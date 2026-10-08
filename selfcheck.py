#!/usr/bin/env python3
"""Offline sanity checks -- no Telegram connection required.

    python selfcheck.py

Verifies that the package imports, the database initialises, the daily plan is
generated inside the configured window with the required gaps, and that the
content selector behaves.  Run it after changing the code or the .env.
"""

from __future__ import annotations

import asyncio
import random
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.config import Settings, load_settings  # noqa: E402
from bot.database import MISSED, PENDING, POSTED, Database  # noqa: E402
from bot.scheduler import RepostScheduler, build_day_plan, generate_times  # noqa: E402
from bot.selector import select_post  # noqa: E402
from bot.settings_store import (  # noqa: E402
    BY_KEY,
    SPECS,
    SettingError,
    clear_override,
    effective_settings,
    format_settings,
    parse_value,
    read_overrides,
    save_override,
)

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def synthetic_settings() -> Settings:
    """Defaults that do not require a real .env."""
    import os

    backup = dict(os.environ)
    os.environ.update(
        BOT_TOKEN="123:TEST",
        CHANNEL_ID="-1001234567890",
        ADMIN_USER_ID="1",
        TIMEZONE="Asia/Tehran",
    )
    for key in (
        "POSTS_PER_DAY", "START_HOUR", "END_HOUR", "START_TIME", "END_TIME",
        "MIN_INTERVAL_MINUTES", "RANDOMIZATION_MINUTES", "REPOST_COOLDOWN_DAYS",
        "DAILY_MIX", "DATABASE_PATH", "LOG_FILE",
    ):
        os.environ.pop(key, None)
    try:
        return load_settings(env_file=Path("/nonexistent.env"))
    finally:
        os.environ.clear()
        os.environ.update(backup)


class FakeGateway:
    """The slice of TelegramGateway the publishing code actually touches."""

    def __init__(self, chat_id: int = -1001234567890) -> None:
        self.channel_numeric_id = chat_id
        self.copied: list[int] = []
        self.created: list[list[int]] = []
        self.admin_messages: list[str] = []

    async def copy_post(self, post) -> list[int]:
        self.copied.append(post.id)
        first = 9000 + len(self.copied)
        created = [first, first + 1] if post.is_album else [first]
        self.created.append(created)
        return created

    async def send_admin(self, text: str) -> None:
        self.admin_messages.append(text)


def schedule_fingerprint(db: Database, day: str) -> list[tuple]:
    """Everything about one day's plan, in a form that is easy to compare."""
    return [
        (
            slot.slot,
            slot.planned_at.isoformat(),
            slot.category,
            slot.status,
            slot.post_id,
            slot.posted_at.isoformat() if slot.posted_at else None,
            slot.error,
        )
        for slot in db.get_schedule(day)
    ]


def rejects(key: str, value: str) -> bool:
    """True when ``value`` is refused for ``key``."""
    try:
        parse_value(BY_KEY[key], value)
    except SettingError:
        return True
    return False


def test_time_generation(settings: Settings) -> None:
    print("\n[1] Schedule generation")
    day = date(2026, 10, 7)
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=settings.timezone)
    start = midnight + timedelta(minutes=settings.start_minutes)
    end = midnight + timedelta(minutes=settings.end_minutes)

    problems: list[str] = []
    for seed in range(200):
        times = generate_times(settings, day, random.Random(seed))
        if len(times) != settings.posts_per_day:
            problems.append(f"seed {seed}: {len(times)} slots")
        elif times != sorted(times):
            problems.append(f"seed {seed}: not sorted")
        elif not all(start <= t <= end for t in times):
            problems.append(f"seed {seed}: outside the window")
        else:
            gaps = [
                (times[i + 1] - times[i]).total_seconds() / 60 for i in range(len(times) - 1)
            ]
            if any(gap < settings.min_interval_minutes for gap in gaps):
                problems.append(f"seed {seed}: gap {min(gaps):.0f} < minimum")
    check(
        not problems,
        f"200 random days: {settings.posts_per_day} slots, in-window, "
        f">= {settings.min_interval_minutes} min apart"
        + (f" — {problems[:3]}" if problems else ""),
    )

    times = generate_times(settings, day, random.Random(42))
    check(
        any(t.minute or t.second for t in times),
        "times carry a minute/second offset instead of landing on the hour",
    )

    # A window with minute precision -- only reachable from /set_start_time and
    # /set_end_time -- must bound every slot just as tightly.
    off_hour = synthetic_settings()
    object.__setattr__(off_hour, "start_minutes", 6 * 60 + 30)
    object.__setattr__(off_hour, "end_minutes", 22 * 60 + 45)
    low = midnight.replace(hour=6, minute=30)
    high = midnight.replace(hour=22, minute=45)
    times = generate_times(off_hour, day, random.Random(7))
    check(
        off_hour.window_minutes == 975 and off_hour.slot_minutes == 975 / 4,
        f"a 06:30 → 22:45 window measures 975 minutes (got {off_hour.window_minutes})",
    )
    check(
        len(times) == off_hour.posts_per_day and all(low <= t <= high for t in times),
        "every slot of a minute-precision window lands inside it",
    )

    if settings.posts_per_day > 1:
        tight = synthetic_settings()
        # (count - 1) gaps of this size cannot fit in the window.
        impossible = settings.window_minutes // (settings.posts_per_day - 1) + 1
        object.__setattr__(tight, "min_interval_minutes", impossible)
        times = generate_times(tight, day, random.Random(1))
        check(
            len(times) == tight.posts_per_day and times[0] >= start and times[-1] <= end,
            f"an impossible MIN_INTERVAL ({impossible}m) falls back to even spacing",
        )


def test_plan(settings: Settings, db: Database) -> None:
    print("\n[2] Daily plan")
    day = date(2026, 10, 7)
    plan = build_day_plan(settings, day, random.Random("plan"))
    categories = [category for _, _, category in plan]
    check(len(plan) == settings.posts_per_day, "plan has one entry per post")
    check(
        sorted(categories) == sorted(settings.daily_mix),
        f"plan covers the daily mix ({', '.join(categories)})",
    )
    check(
        categories != list(settings.daily_mix) or settings.posts_per_day == 1,
        "categories are shuffled rather than in .env order",
    )

    db.insert_schedule(day.isoformat(), plan)
    check(db.has_schedule(day.isoformat()), "plan persisted to SQLite")
    check(
        db.insert_schedule(day.isoformat(), plan) == 0,
        "re-inserting the same day is a no-op (restart-safe)",
    )
    slots = db.get_schedule(day.isoformat())
    check(len(slots) == settings.posts_per_day, "plan reads back complete")
    check(all(s.status == "pending" for s in slots), "new slots start pending")
    check(
        build_day_plan(settings, day, random.Random("plan")) == plan,
        "regenerating the same day is deterministic",
    )


def seed_archive(db: Database, settings: Settings) -> None:
    now = datetime.now(settings.timezone)
    counts = {"TEXT": 6, "IMAGE": 5, "VIDEO": 5, "AUDIO": 4, "FILE": 3}
    message_id = 100
    for category, number in counts.items():
        for index in range(number):
            message_id += 1
            db.upsert_post(
                chat_id=-1001234567890,
                group_key=f"msg:{message_id}",
                message_id=message_id,
                media_type=category.lower(),
                category=category,
                original_date=(now - timedelta(days=30)).isoformat(),
                has_caption=True,
                now=now,
            )


def test_selection(settings: Settings, db: Database) -> None:
    print("\n[3] Content selection")
    now = datetime.now(settings.timezone)

    check(db.archive_size() == 23, f"archive holds 23 posts (got {db.archive_size()})")
    breakdown = dict((c, ok) for c, ok, _off, _used in db.archive_breakdown())
    check(breakdown.get("VIDEO") == 5, "categories counted correctly")

    # Regression: an empty exclusion list must not filter anything out.  It
    # used to render as `id NOT IN (NULL)`, which evaluates to NULL -- never
    # TRUE -- so the query matched no rows at all and the whole archive looked
    # empty to both the scheduler and /post.
    video_ids = [p.id for p in db.candidates("VIDEO", [])]
    check(
        len(video_ids) == 5,
        f"an empty exclusion list returns every VIDEO candidate (got {len(video_ids)})",
    )
    check(
        len(db.any_candidates([])) == 23,
        f"an empty exclusion list returns the whole archive (got {len(db.any_candidates([]))})",
    )
    check(
        len(db.candidates("VIDEO", video_ids[:2])) == 3,
        "a non-empty exclusion list still excludes",
    )

    # Every category /post can ask for must resolve on a fresh archive, and
    # must resolve to that exact category.
    for category in ("VIDEO", "IMAGE", "AUDIO", "TEXT", "FILE"):
        chosen, why = select_post(
            db, settings, category, now=now, exclude_post_ids=[], allow_fallback=False
        )
        check(
            chosen is not None and chosen.category == category,
            f"/post {category.lower()} picks a {category} post ({why or 'ok'})",
        )

    post, _reason = select_post(db, settings, "VIDEO", now=now, exclude_post_ids=[])
    check(post is not None and post.category == "VIDEO", "picks the requested category")
    check(post.media_type == "video", "carries the media type through")

    # Nothing left in FILE -> must fall back rather than fail.
    file_ids = [p.id for p in db.candidates("FILE", [])]
    post, reason = select_post(db, settings, "FILE", now=now, exclude_post_ids=file_ids)
    check(post is not None and post.id not in file_ids, f"falls back when a category is used up ({reason})")

    # /post asks for one exact type, so it must not fall back to another one.
    post, reason = select_post(
        db, settings, "FILE", now=now, exclude_post_ids=file_ids, allow_fallback=False
    )
    check(post is None and "FILE" in reason, f"strict selection refuses to fall back ({reason})")

    # Excluding everything must yield None, not an exception.
    post, reason = select_post(
        db, settings, "TEXT", now=now, exclude_post_ids=[p.id for p in db.any_candidates([])]
    )
    check(post is None, "returns None when the archive is exhausted")

    # Cooldown: everything reposted just now -> the cooldown is relaxed, not skipped.
    for item in db.any_candidates([]):
        db.mark_reposted(item.id, now)
    post, reason = select_post(db, settings, "VIDEO", now=now, exclude_post_ids=[])
    check(post is not None and "cooldown relaxed" in reason, "relaxes the cooldown instead of failing")

    # Content past the cooldown should dominate a single draw over content that
    # is still inside it -- but the cooldown must not hard-block anything.
    long_ago = now - timedelta(days=settings.repost_cooldown_days + 20)
    for item in db.candidates("VIDEO", []):
        db.mark_reposted(item.id, long_ago)

    for seed in range(60):
        chosen, reason = select_post(
            db, settings, "VIDEO", now=now, exclude_post_ids=[], rng=random.Random(seed)
        )
        if chosen is not None:
            break
    check(
        chosen is not None and not reason,
        "items past the cooldown are eligible again (no fallback needed)",
    )

    # A brand new post (never reposted) should be favoured over the five that
    # were reposted 7+ days ago: weight 8.0 against 1.0 + age/30.
    new_id = db.upsert_post(
        chat_id=-1001234567890,
        group_key="msg:99999",
        message_id=99999,
        media_type="video",
        category="VIDEO",
        original_date=None,
        has_caption=False,
        now=now,
    )
    hits = 0
    for seed in range(200):
        chosen, _ = select_post(
            db, settings, "VIDEO", now=now, exclude_post_ids=[], rng=random.Random(seed)
        )
        hits += chosen is not None and chosen.id == new_id
    uniform = 200 / (len(db.candidates("VIDEO", [])) or 1)
    check(
        hits > uniform * 1.8,
        f"weights favour never-reposted content: {hits}/200 draws "
        f"(uniform would be ~{uniform:.0f})",
    )
    db.disable_post(new_id, "self-check cleanup")


def test_restart_safety(settings: Settings, db: Database) -> None:
    print("\n[4] Restart safety")
    day = date(2026, 10, 8).isoformat()
    plan = build_day_plan(settings, date(2026, 10, 8), random.Random("plan"))
    db.insert_schedule(day, plan)
    db.update_slot(day, 0, status=POSTED, posted_at=datetime.now(settings.timezone))
    db.update_slot(day, 1, status="sending")

    stale = db.stale_sending()
    check(len(stale) == 1, "mid-send slots are detected after a crash")
    for slot in stale:
        db.update_slot(slot.day, slot.slot, status="unknown")
    check(
        not db.stale_sending() and len(db.pending_for_day(day)) == len(plan) - 2,
        "an interrupted slot is never left pending (no duplicate on restart)",
    )
    used = db.used_post_ids(day)
    check(len(used) == 0, "slots without a post id do not reserve archive content")


def test_settings_store(settings: Settings, db: Database) -> None:
    print("\n[5] Runtime settings")
    check(
        effective_settings(settings, {}) is settings,
        "with nothing saved, the effective settings are the .env ones",
    )
    check(read_overrides(db) == {}, "a fresh database has no saved settings")

    check(parse_value(BY_KEY["posts_per_day"], "6") == 6, "/set_posts_per_day 6 parses")
    check(
        all(rejects("posts_per_day", value) for value in ("0", "-3", "49", "abc", "")),
        "posts per day refuses 0, negatives, >48, text and a blank argument",
    )
    check(
        parse_value(BY_KEY["start_time"], "08:30") == 510,
        "/set_start_time 08:30 parses to 510 minutes past midnight",
    )
    check(
        parse_value(BY_KEY["end_time"], "24:00") == 1440,
        "/set_end_time 24:00 means midnight",
    )
    check(
        all(rejects("start_time", value) for value in ("8", "25:00", "08:70", "noon", "")),
        "start time refuses 8, 25:00, 08:70, noon and a blank argument",
    )
    check(
        all(rejects("min_interval", value) for value in ("-1", "x", "")),
        "the minimum interval cannot be negative",
    )
    check(
        all(rejects("randomization", value) for value in ("-1", "x", "")),
        "the randomization cannot be negative",
    )
    check(
        all(rejects("cooldown", value) for value in ("-1", "x", "")),
        "the cooldown cannot be negative",
    )

    # Save one of each, then read them back as an "effective" set.
    save_override(db, "posts_per_day", 6)
    save_override(db, "start_time", 6 * 60 + 30)
    save_override(db, "end_time", 23 * 60)
    save_override(db, "min_interval", 90)
    save_override(db, "randomization", 45)
    save_override(db, "cooldown", 10)
    effective = effective_settings(settings, read_overrides(db))

    check(effective.posts_per_day == 6, "the saved posts per day is applied")
    check(
        effective.start_clock == "06:30" and effective.end_clock == "23:00",
        f"the saved window is applied ({effective.start_clock} → {effective.end_clock})",
    )
    check(
        len(effective.daily_mix) == 6,
        f"the daily mix is padded to the new posts per day ({len(effective.daily_mix)})",
    )
    check(
        effective.min_interval_minutes == 90 and effective.randomization_minutes == 45,
        "the saved interval and randomization are applied",
    )
    check(effective.repost_cooldown_days == 10, "the saved cooldown is applied")
    check(
        settings.posts_per_day == 4 and settings.start_minutes == 8 * 60,
        "the .env defaults themselves are left untouched",
    )

    # Values must survive a restart, which is a new process reading the same file.
    restarted = RepostScheduler(db, settings, FakeGateway())
    check(
        restarted.settings.posts_per_day == 6
        and restarted.settings.repost_cooldown_days == 10,
        "a restart picks the saved values up from SQLite",
    )

    text = format_settings(effective, read_overrides(db))
    for expected in (
        "⚙️ Scheduling Settings",
        "📌 Posts per day: 6",
        "🕐 Time window: 06:30 → 23:00",
        "⏱ Minimum interval: 90 minutes",
        "🎲 Randomization: ±45 minutes",
        "🔄 Repost cooldown: 10 days",
        "value(s) set from Telegram",
    ):
        check(expected in text, f"/settings prints {expected!r}")

    # A window too small for the requested count, and one that does not move
    # forward, must both be refused rather than producing a broken plan.
    try:
        effective_settings(
            settings, {"posts_per_day": 48, "start_time": 1380, "end_time": 1410},
            strict=True,
        )
    except SettingError as exc:
        check("do not fit" in str(exc), f"a window too small for the count is refused ({exc})")
    else:
        check(False, "a window too small for the count is refused")

    try:
        effective_settings(settings, {"start_time": 1380, "end_time": 1380}, strict=True)
    except SettingError as exc:
        check("must be after" in str(exc), f"an end time equal to the start is refused ({exc})")
    else:
        check(False, "an end time equal to the start is refused")

    check(
        effective_settings(settings, {"posts_per_day": 999}).posts_per_day == 4,
        "an impossible saved value is skipped instead of stopping the bot",
    )

    clear_override(db, "posts_per_day")
    check(
        effective_settings(settings, read_overrides(db)).posts_per_day == 4,
        "/set_posts_per_day default falls back to the .env value",
    )

    for spec in SPECS:  # leave no overrides behind for the later tests
        clear_override(db, spec.key)
    check(read_overrides(db) == {}, "the settings tests cleaned up after themselves")


def test_settings_change_schedule(settings: Settings, db: Database) -> None:
    print("\n[6] Changing settings vs. the schedule")
    today = datetime.now(settings.timezone).date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    today_iso, tomorrow_iso, day_after_iso = (
        today.isoformat(), tomorrow.isoformat(), day_after.isoformat()
    )

    # Today, tomorrow and the day after, exactly as the scheduler leaves them:
    # today has one published slot, the future days are untouched.
    for day in (today, tomorrow, day_after):
        db.delete_schedule(day.isoformat())
        db.insert_schedule(
            day.isoformat(), build_day_plan(settings, day, random.Random(f"plan:{day.isoformat()}"))
        )
    db.update_slot(today_iso, 0, status=POSTED, posted_at=datetime.now(settings.timezone), post_id=1)

    today_before = schedule_fingerprint(db, today_iso)
    scheduler = RepostScheduler(db, settings, FakeGateway())

    summary = scheduler.change_setting("posts_per_day", "6")
    check("📌 Posts per day: 6" in summary, f"the change is confirmed ({summary.splitlines()[0]})")
    check(scheduler.settings.posts_per_day == 6, "the scheduler adopts the new value at once")
    check(
        schedule_fingerprint(db, today_iso) == today_before,
        "today's schedule is untouched by a settings change",
    )
    rebuilt = db.get_schedule(tomorrow_iso)
    check(len(rebuilt) == 6, f"tomorrow's all-pending plan is rebuilt (got {len(rebuilt)} slots)")
    check(all(s.status == PENDING for s in rebuilt), "the rebuilt plan is entirely pending")
    check(
        sorted(s.category for s in rebuilt) == sorted(scheduler.settings.daily_mix),
        "the rebuilt plan covers the new daily mix",
    )

    # A future day that has already been touched -- say one dropped with /skip --
    # must be left exactly as it is, so nothing can be lost or duplicated.
    db.update_slot(day_after_iso, 0, status=MISSED, error="skipped by admin")
    day_after_before = schedule_fingerprint(db, day_after_iso)
    scheduler.change_setting("posts_per_day", "3")
    check(
        schedule_fingerprint(db, day_after_iso) == day_after_before,
        "a future day with a non-pending slot is left exactly as it is",
    )
    check(
        len(db.get_schedule(tomorrow_iso)) == 3,
        "the future day with no history is rebuilt again",
    )

    # A refused value must change nothing at all.
    before = schedule_fingerprint(db, tomorrow_iso)
    try:
        scheduler.change_setting("posts_per_day", "0")
    except SettingError as exc:
        check("Posts per day" in str(exc), f"an invalid value is refused ({exc})")
    else:
        check(False, "an invalid value is refused")
    check(
        scheduler.settings.posts_per_day == 3 and schedule_fingerprint(db, tomorrow_iso) == before,
        "a refused value changes neither the settings nor the schedule",
    )

    try:
        scheduler.change_setting("end_time", "07:00")
    except SettingError as exc:
        check("must be after" in str(exc), f"an end time before the start is refused ({exc})")
    else:
        check(False, "an end time before the start is refused")

    # Narrow the window, then ask for more posts than it can hold.
    scheduler.change_setting("end_time", "08:30")
    try:
        scheduler.change_setting("posts_per_day", "48")
    except SettingError as exc:
        check("do not fit" in str(exc), f"a window too small for the count is refused ({exc})")
    else:
        check(False, "a window too small for the count is refused")

    scheduler.change_setting("posts_per_day", "default")
    check(
        scheduler.settings.posts_per_day == settings.posts_per_day,
        "/set_posts_per_day default returns to the .env value",
    )
    check("posts_per_day" not in read_overrides(db), "the removed override is gone from SQLite")

    for spec in SPECS:
        clear_override(db, spec.key)
    scheduler.reload_settings()
    check(
        scheduler.settings.posts_per_day == settings.posts_per_day,
        "the scheduler falls back to the .env values once the overrides are gone",
    )


async def _manual_scenario(db: Database, settings: Settings, gateway: FakeGateway) -> None:
    """Every manual post for one day, in a single event loop.

    One loop for all of them on purpose: an ``asyncio.Lock`` binds to the loop
    that first uses it, so a second ``asyncio.run`` on the same scheduler would
    raise rather than test anything.
    """
    today = datetime.now(settings.timezone).date()
    tomorrow = today + timedelta(days=1)
    today_iso, tomorrow_iso = today.isoformat(), tomorrow.isoformat()

    posted = datetime.now(settings.timezone)
    db.delete_schedule(today_iso)
    db.delete_schedule(tomorrow_iso)
    db.insert_schedule(today_iso, build_day_plan(settings, today, random.Random(f"plan:{today_iso}")))
    db.insert_schedule(
        tomorrow_iso, build_day_plan(settings, tomorrow, random.Random(f"plan:{tomorrow_iso}"))
    )
    # Two of today's slots already published, two still waiting.
    db.update_slot(today_iso, 0, status=POSTED, posted_at=posted, post_id=1)
    db.update_slot(today_iso, 1, status=POSTED, posted_at=posted, post_id=2)

    today_before = schedule_fingerprint(db, today_iso)
    tomorrow_before = schedule_fingerprint(db, tomorrow_iso)
    pending_before = len(db.pending_for_day(today_iso))
    used_before = db.used_post_ids(today_iso)
    last_posted_before = db.last_posted_at(today_iso)

    scheduler = RepostScheduler(db, settings, gateway)

    first = await scheduler.publish_now("VIDEO")
    check(first.ok and first.post is not None, f"/post video publishes ({first.reason or 'ok'})")
    check(
        first.post is not None and first.post.category == "VIDEO",
        "/post video published a VIDEO",
    )
    check(
        schedule_fingerprint(db, today_iso) == today_before,
        "a manual post leaves today's schedule byte-for-byte identical",
    )
    check(
        schedule_fingerprint(db, tomorrow_iso) == tomorrow_before,
        "a manual post does not regenerate tomorrow either",
    )
    check(
        len(db.pending_for_day(today_iso)) == pending_before,
        f"a manual post consumes no scheduled slot ({pending_before} still pending)",
    )
    check(
        db.used_post_ids(today_iso) == used_before,
        "a manual post does not reserve archive content for the scheduler",
    )
    check(
        db.last_posted_at(today_iso) == last_posted_before,
        "a manual post cannot delay the next scheduled post",
    )

    refreshed = db.get_post(first.post.id) if first.post else None
    check(
        refreshed is not None and refreshed.repost_count == 1,
        "the manual post recorded a repost, so its cooldown starts",
    )
    check(
        refreshed is not None and refreshed.last_reposted_at is not None,
        "the manual post stamped last_reposted_at",
    )
    check(
        bool(gateway.created)
        and db.is_own_repost(gateway.channel_numeric_id, gateway.created[0][0]),
        "the copy is remembered so the channel listener ignores it",
    )

    # The cooldown and duplicate protection are shared with the scheduler.
    second = await scheduler.publish_now("VIDEO")
    check(
        second.ok
        and second.post is not None
        and first.post is not None
        and second.post.id != first.post.id,
        "/post video does not repeat the item it just used",
    )

    others = [
        await scheduler.publish_now("IMAGE"),
        await scheduler.publish_now("TEXT"),
        await scheduler.publish_random(),
    ]
    check(
        all(result.ok for result in others),
        "further manual posts (/post image, /post text, /post random) publish",
    )
    check(
        schedule_fingerprint(db, today_iso) == today_before
        and schedule_fingerprint(db, tomorrow_iso) == tomorrow_before,
        "five manual posts still leave the whole schedule untouched",
    )

    # Nothing left of a category: a clear refusal, and still no schedule change.
    file_ids = [post.id for post in db.candidates("FILE", [])]
    for post_id in file_ids:
        db.mark_reposted(post_id, datetime.now(settings.timezone))
    chosen, _reason = select_post(
        db, settings, "FILE", now=datetime.now(settings.timezone),
        exclude_post_ids=file_ids, allow_fallback=False,
    )
    check(chosen is None, "an exhausted category yields nothing for a strict /post")
    check(
        schedule_fingerprint(db, today_iso) == today_before,
        "a failed selection changes nothing in the schedule either",
    )


def test_manual_post_independence(settings: Settings, db: Database) -> None:
    print("\n[7] Manual /post is independent of the schedule (the important one)")
    gateway = FakeGateway()
    asyncio.run(_manual_scenario(db, settings, gateway))


def main() -> int:
    print("Reposter self-check")
    settings = synthetic_settings()
    print(
        f"  config: {settings.posts_per_day}/day, "
        f"{settings.start_clock}-{settings.end_clock} "
        f"{settings.timezone_name}, min gap {settings.min_interval_minutes}m"
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "check.sqlite3")
        db.connect()
        try:
            test_time_generation(settings)
            test_plan(settings, db)
            db.purge_schedule_before(date(2030, 1, 1))  # clear the plan fixture
            seed_archive(db, settings)
            test_selection(settings, db)
            test_restart_safety(settings, db)
            test_settings_store(settings, db)
            test_settings_change_schedule(settings, db)
            test_manual_post_independence(settings, db)
        finally:
            db.close()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"   - {failure}")
        return 1
    print("✅ All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
