"""Content classification and the repost selection strategy."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

from aiogram.types import Message

from .database import Database, Post
from .config import Settings

log = logging.getLogger(__name__)

#: Telegram media type -> daily category.
TYPE_TO_CATEGORY = {
    "photo": "IMAGE",
    "video": "VIDEO",
    "animation": "VIDEO",
    "video_note": "VIDEO",
    "audio": "AUDIO",
    "voice": "AUDIO",
    "document": "FILE",
    "text": "TEXT",
    "sticker": "FILE",
    "poll": "TEXT",
    "other": "FILE",
}

#: Higher rank wins when several media types share one album.
CATEGORY_RANK = {
    "TEXT": 0,
    "FILE": 1,
    "AUDIO": 2,
    "IMAGE": 3,
    "VIDEO": 4,
}

#: What to fall back to when a category has no usable content.
FALLBACK_CHAIN: dict[str, tuple[str, ...]] = {
    "TEXT": ("TEXT", "IMAGE", "FILE", "VIDEO", "AUDIO"),
    "IMAGE": ("IMAGE", "VIDEO", "FILE", "TEXT", "AUDIO"),
    "VIDEO": ("VIDEO", "IMAGE", "FILE", "AUDIO", "TEXT"),
    "AUDIO": ("AUDIO", "FILE", "VIDEO", "TEXT", "IMAGE"),
    "FILE": ("FILE", "TEXT", "IMAGE", "VIDEO", "AUDIO"),
}

#: A never-reposted item outranks everything else.
NEVER_REPOSTED_WEIGHT = 8.0


def category_rank(category: str) -> int:
    return CATEGORY_RANK.get(category, 0)


def detect_media_type(message: Message) -> str:
    """Best-effort media type of a single Telegram message."""
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.animation:
        return "animation"
    if message.video_note:
        return "video_note"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.sticker:
        return "sticker"
    if message.document:
        return "document"
    if message.poll:
        return "poll"
    if message.text:
        return "text"
    return "other"


def media_type_to_category(media_type: str) -> str:
    return TYPE_TO_CATEGORY.get(media_type, "FILE")


def has_caption(message: Message) -> bool:
    return bool(message.caption or message.text)


def _age_days(last_reposted_at: str | None, now: datetime) -> float | None:
    """Days since the post was last reposted, or ``None`` if never."""
    if not last_reposted_at:
        return None
    try:
        last = datetime.fromisoformat(last_reposted_at)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return (now - last).total_seconds() / 86400.0


def _weight(post: Post, now: datetime) -> float:
    """Older / less-used posts get picked more often, but nothing is excluded."""
    age = _age_days(post.last_reposted_at, now)
    if age is None:
        base = NEVER_REPOSTED_WEIGHT
    else:
        # 1.0 right after a repost, growing slowly with time.
        base = 1.0 + min(age, 180.0) / 30.0
    # Slight preference for content that has been reused less often.
    return base / (1.0 + 0.35 * post.repost_count)


def _pick_weighted(pool: list[Post], now: datetime, rng: random.Random) -> Post:
    weights = [_weight(post, now) for post in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def select_post(
    db: Database,
    settings: Settings,
    category: str,
    *,
    now: datetime,
    exclude_post_ids: list[int],
    rng: random.Random | None = None,
    allow_fallback: bool = True,
) -> tuple[Post | None, str]:
    """Choose an archive post for ``category``.

    Returns ``(post, reason)``.  ``reason`` is a short human-readable note used
    in logs and in admin replies (e.g. the fallback that was applied).

    With ``allow_fallback=False`` only ``category`` itself is considered -- no
    fallback chain and no catch-all.  An explicit ``/post video`` needs that:
    handing back a photo because the archive had no video left would silently
    publish something the admin did not ask for.  The scheduler keeps the
    default, so a scheduled slot still degrades instead of failing.
    """
    rng = rng or random.Random()
    cooldown = timedelta(days=settings.repost_cooldown_days)
    chain = FALLBACK_CHAIN.get(category, (category,))
    if not allow_fallback:
        chain = chain[:1]
    notes: list[str] = []

    # Pass 1: the requested category, cooled-down content only.
    # Pass 2: the requested category, cooldown relaxed.
    # Pass 3+: the fallback chain under the same two rules.
    for index, candidate_category in enumerate(chain):
        try:
            pool = db.candidates(candidate_category, exclude_post_ids)
        except Exception:  # pragma: no cover - defensive
            log.exception("Candidate query failed for %s", candidate_category)
            pool = []
        if not pool:
            continue

        if cooldown > timedelta(0):
            fresh = [
                post
                for post in pool
                if (age := _age_days(post.last_reposted_at, now)) is None
                or timedelta(days=age) >= cooldown
            ]
        else:
            fresh = pool

        if fresh:
            chosen = _pick_weighted(fresh, now, rng)
        else:
            # Everything in this category is still cooling down; reuse the
            # stalest item rather than skipping the slot.
            chosen = max(
                pool,
                key=lambda p: _age_days(p.last_reposted_at, now) or 0.0,
            )
            notes.append(f"{candidate_category} cooldown relaxed")

        if index > 0:
            notes.append(f"fell back from {category} to {candidate_category}")
        return chosen, "; ".join(notes)

    # Nothing matched any category: try absolutely anything that is enabled.
    # Strict callers asked for one specific type, so stop here instead.
    if not allow_fallback:
        return None, f"no eligible {category} content in the archive"
    try:
        everything = db.any_candidates(exclude_post_ids)
    except Exception:  # pragma: no cover - defensive
        everything = []
    if everything:
        notes.append("fell back to any available content")
        return _pick_weighted(everything, now, rng), "; ".join(notes)

    return None, "archive is empty or every candidate is already used today"


def plan_categories(settings: Settings, day_iso: str) -> list[str]:
    """Category order for one day, deterministically shuffled per date.

    Using the date as the RNG seed means a regenerated plan for the same day is
    identical, which keeps restarts from reshuffling the daily mix.
    """
    rng = random.Random(f"mix:{day_iso}:{settings.posts_per_day}")
    plan = list(settings.daily_mix)
    rng.shuffle(plan)
    return plan
