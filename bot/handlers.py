"""Telegram command handlers, the live channel listener and the archive importer."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import Chat, Message, MessageOriginChannel

from .config import CATEGORIES, Settings
from .database import (
    FAILED,
    MISSED,
    PENDING,
    POSTED,
    SKIPPED,
    UNKNOWN,
    Database,
)
from .scheduler import RepostScheduler
from .selector import detect_media_type, has_caption, media_type_to_category
from .settings_store import (
    BY_KEY,
    COMMANDS as SETTING_COMMANDS,
    SETTINGS_USAGE,
    SettingError,
    format_settings,
    read_overrides,
)
from .telegram import (
    ChannelError,
    TelegramGateway,
    TransientRepostError,
    classify,
)

log = logging.getLogger(__name__)

STATUS_ICONS = {
    PENDING: "⏳",
    POSTED: "✅",
    SKIPPED: "⏭",
    MISSED: "⚠️",
    FAILED: "❌",
    UNKNOWN: "❓",
    "sending": "📤",
}

CATEGORY_ICONS = {
    "TEXT": "📝",
    "IMAGE": "🖼",
    "VIDEO": "🎬",
    "AUDIO": "🎵",
    "FILE": "📎",
}

#: Upper bound for a single /scan range, to keep a typo from running for days.
MAX_SCAN_RANGE = 20000

#: How long to wait for more forwards before confirming an import batch.
IMPORT_SETTLE_SECONDS = 3.0

#: /post argument -> archive category.  Matching is case-insensitive.
POST_ALIASES = {
    "video": "VIDEO",
    "vid": "VIDEO",
    "image": "IMAGE",
    "photo": "IMAGE",
    "pic": "IMAGE",
    "audio": "AUDIO",
    "music": "AUDIO",
    "text": "TEXT",
    "file": "FILE",
    "document": "FILE",
    "doc": "FILE",
}
POST_RANDOM = "random"

POST_USAGE = "Usage: /post video | image | audio | text | file | random"

#: Confirmation for the category that actually ended up published.
POST_SUCCESS = {
    "VIDEO": "✅ یک ویدیوی تصادفی از آرشیو منتشر شد.",
    "IMAGE": "✅ یک عکس تصادفی از آرشیو منتشر شد.",
    "AUDIO": "✅ یک صوت تصادفی از آرشیو منتشر شد.",
    "TEXT": "✅ یک پست متنی تصادفی از آرشیو منتشر شد.",
    "FILE": "✅ یک فایل تصادفی از آرشیو منتشر شد.",
}
POST_EMPTY = {
    "VIDEO": "❌ هیچ ویدیوی واجد شرایطی در آرشیو پیدا نشد.",
    "IMAGE": "❌ هیچ عکس واجد شرایطی در آرشیو پیدا نشد.",
    "AUDIO": "❌ هیچ صوت واجد شرایطی در آرشیو پیدا نشد.",
    "TEXT": "❌ هیچ پست متنی واجد شرایطی در آرشیو پیدا نشد.",
    "FILE": "❌ هیچ فایل واجد شرایطی در آرشیو پیدا نشد.",
}
POST_EMPTY_ANY = "❌ هیچ پست واجد شرایطی در آرشیو پیدا نشد."


@dataclass
class ImportBatch:
    """Counts the posts imported in one burst of forwarding."""

    count: int = 0
    task: asyncio.Task | None = None


@dataclass
class AppContext:
    base_settings: Settings
    db: Database
    gateway: TelegramGateway
    scheduler: RepostScheduler
    started_at: datetime = field(default_factory=lambda: datetime.now())
    scan_task: asyncio.Task | None = None
    scan_stop: asyncio.Event = field(default_factory=asyncio.Event)
    import_batches: dict[int, ImportBatch] = field(default_factory=dict)

    @property
    def settings(self) -> Settings:
        """The settings currently in force.

        Everything reads scheduling values through here, so a value changed with
        ``/set_*`` is visible to every handler at once and nobody keeps a stale
        copy of what ``.env`` said at startup.
        """
        return self.scheduler.settings


class IsAdmin(BaseFilter):
    """Allow only the configured administrator."""

    async def __call__(self, message: Message, ctx: AppContext) -> bool:
        return bool(message.from_user and message.from_user.id == ctx.settings.admin_user_id)


class IsConfiguredChannel(BaseFilter):
    """Allow channel posts that come from the configured channel."""

    async def __call__(self, message: Message, ctx: AppContext) -> bool:
        resolved = ctx.gateway.channel_numeric_id
        if resolved is not None:
            return message.chat.id == resolved
        configured = str(ctx.settings.channel_id).lstrip("@").lower()
        return (message.chat.username or "").lower() == configured


def humanize(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "now"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "under a minute"


def message_has_content(message: Message) -> bool:
    return detect_media_type(message) not in {"other"}


def snapshot(ctx: AppContext) -> str:
    """The /status text."""
    settings = ctx.settings
    now = ctx.scheduler.now
    today = now.date().isoformat()

    counters = ctx.db.day_counters(today)
    planned = sum(counters.values())
    posted = counters.get(POSTED, 0)
    skipped = counters.get(SKIPPED, 0) + counters.get(MISSED, 0)
    failed = counters.get(FAILED, 0)
    remaining = counters.get(PENDING, 0)

    paused = ctx.scheduler.paused
    state_line = "⏸ Paused" if paused else "▶️ Running"

    lines = [f"🤖 Bot: {state_line}"]
    lines.append(f"📅 Today ({today}, {settings.timezone_name})")
    lines.append(
        f"   planned {planned} · posted {posted} · remaining {remaining} "
        f"· skipped {skipped} · failed {failed}"
    )

    upcoming = ctx.scheduler.next_slot()
    if upcoming is not None:
        countdown = humanize(upcoming.planned_at - now)
        icon = CATEGORY_ICONS.get(upcoming.category, "•")
        lines.append(f"⏭ Next post: {upcoming.planned_at:%H:%M} — {icon} {upcoming.category} (in {countdown})")
    else:
        lines.append("⏭ Next post: nothing scheduled")

    total = ctx.db.archive_size()
    lines.append(f"🗂 Archive: {total} posts")
    breakdown = ctx.db.archive_breakdown()
    if breakdown:
        parts = [
            f"{CATEGORY_ICONS.get(cat, '•')} {cat} {ok}"
            + (f" (+{off} broken)" if off else "")
            for cat, ok, off, _used in breakdown
        ]
        lines.append("   " + " · ".join(parts))
    else:
        lines.append("   empty — use /scan or forward old posts to me")

    lines.append(f"🕒 Posting window: {settings.start_clock}–{settings.end_clock}")
    lines.append(
        f"⚙️ {settings.posts_per_day}/day · min gap {settings.min_interval_minutes}m "
        f"· cooldown {settings.repost_cooldown_days}d"
    )
    return "\n".join(lines)


def today_overview(ctx: AppContext) -> str:
    """The /today text."""
    now = ctx.scheduler.now
    day = now.date().isoformat()
    slots = ctx.db.get_schedule(day)
    if not slots:
        return f"No schedule found for {day}. It is generated automatically on the first tick."

    lines = [f"📅 {day} ({ctx.settings.timezone_name})"]
    for slot in slots:
        icon = STATUS_ICONS.get(slot.status, "•")
        entry = f"{icon} {slot.planned_at:%H:%M} — {slot.category}"
        if slot.post_id is not None:
            post = ctx.db.get_post(slot.post_id)
            if post is not None:
                entry += f" → #{post.label} ({post.media_type})"
        if slot.error and slot.status in {FAILED, MISSED, POSTED, UNKNOWN}:
            entry += f"\n      ↳ {slot.error}"
        lines.append(entry)

    pending_tomorrow = ctx.db.pending_for_day((now.date() + timedelta(days=1)).isoformat())
    if pending_tomorrow:
        preview = ", ".join(f"{s.planned_at:%H:%M} {s.category}" for s in pending_tomorrow)
        lines.append(f"\n➡️ Tomorrow: {preview}")
    return "\n".join(lines)


def archive_overview(ctx: AppContext) -> str:
    total = ctx.db.archive_size()
    lines = [f"🗂 Archive: {total} usable posts"]
    breakdown = ctx.db.archive_breakdown()
    if not breakdown:
        lines.append("The archive is empty.")
    else:
        for category, ok, off, used in breakdown:
            icon = CATEGORY_ICONS.get(category, "•")
            lines.append(f"{icon} {category}: {ok} available · {used} reposted at least once" + (f" · {off} broken" if off else ""))

    history = ctx.db.history(5)
    if history:
        lines.append("\n🕘 Last reposts:")
        for day, slot, category, post_id, posted_at in history:
            stamp = (posted_at or "")[11:16]
            lines.append(f"   {day} {stamp} — {category} #{post_id}")

    lines.append(
        "\nPopulate it with /scan, or by forwarding old posts from the channel "
        "to this chat — every new channel post is archived automatically."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------- router


def build_router(ctx: AppContext) -> tuple[Router, Router]:
    """Return ``(admin_router, public_router)``; register admin first."""
    router = Router(name="admin")
    # Every handler carries its own filters rather than the router carrying a
    # root filter.  A root filter that rejects an update can end the chain, and
    # then a stranger's message would never reach the public router below and
    # would hang unanswered.  Per-handler filters keep the fallback reachable.
    admin_only = (IsAdmin(), F.chat.type == "private")
    # Commands are only ever answered in a private chat; without this a stray
    # channel post could reach a handler and be answered publicly.
    channel_only = (IsConfiguredChannel(),)

    # ------------------------------------------------------------------ basics

    @router.message(CommandStart(), *admin_only)
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "👋 Reposter is running.\n\n"
            "It reposts old posts from your channel on a randomised daily schedule.\n\n"
            "Commands:\n"
            "/status — health, counters and next post\n"
            "/today — today's planned schedule\n"
            "/next — the next scheduled post\n"
            "/skip — drop the next scheduled post\n"
            "/pause — stop posting\n"
            "/resume — start posting again\n"
            "/post video — انتشار فوری یک ویدیوی تصادفی\n"
            "/post image — انتشار فوری یک عکس تصادفی\n"
            "/post audio — انتشار فوری یک صوت تصادفی\n"
            "/post text — انتشار فوری یک پست متنی تصادفی\n"
            "/post file — انتشار فوری یک فایل تصادفی\n"
            "/post random — انتشار فوری یک پست با نوع تصادفی\n"
            "/settings — scheduling settings, and what each one is\n"
            "/set_posts_per_day 6 — reposts per day\n"
            "/set_start_time 08:00 — when the window opens\n"
            "/set_end_time 23:00 — when the window closes\n"
            "/set_min_interval 90 — minutes between scheduled posts\n"
            "/set_randomization 45 — ± minutes of jitter per slot\n"
            "/set_cooldown 7 — days before a post may be reused\n"
            "/scan — build the archive (see /scan for usage)\n"
            "/archive — archive statistics\n"
            "/import — how to import old posts\n"
            "/list [CATEGORY] — sample archive entries\n"
        )

    @router.message(Command("status"), *admin_only)
    async def cmd_status(message: Message) -> None:
        await message.answer(snapshot(ctx))

    @router.message(Command("today"), *admin_only)
    async def cmd_today(message: Message) -> None:
        await message.answer(today_overview(ctx))

    @router.message(Command("next"), *admin_only)
    async def cmd_next(message: Message) -> None:
        now = ctx.scheduler.now
        slot = ctx.scheduler.next_slot()
        if slot is None:
            await message.answer("No pending post scheduled.")
            return
        countdown = humanize(slot.planned_at - now)
        icon = CATEGORY_ICONS.get(slot.category, "•")
        lines = [
            f"⏭ {slot.planned_at:%Y-%m-%d %H:%M} — {icon} {slot.category}",
            f"⏱ in {countdown}",
            f"slot {slot.slot + 1}/{ctx.settings.posts_per_day} of {slot.day}",
        ]
        if ctx.scheduler.paused:
            lines.append("⏸ Posting is paused — /resume to continue.")
        await message.answer("\n".join(lines))

    @router.message(Command("skip"), *admin_only)
    async def cmd_skip(message: Message) -> None:
        slot = ctx.scheduler.skip_next()
        if slot is None:
            await message.answer("Nothing scheduled to skip.")
            return
        await message.answer(
            f"⏭ Skipped {slot.day} {slot.planned_at:%H:%M} ({slot.category})."
        )

    @router.message(Command("pause"), *admin_only)
    async def cmd_pause(message: Message) -> None:
        ctx.scheduler.pause()
        log.info("Posting paused by admin")
        await message.answer("⏸ Paused. The schedule is kept; use /resume to continue.")

    @router.message(Command("resume"), *admin_only)
    async def cmd_resume(message: Message) -> None:
        ctx.scheduler.resume()
        log.info("Posting resumed by admin")
        await message.answer("▶️ Resumed.")

    @router.message(Command("post"), *admin_only)
    async def cmd_post(message: Message, command: CommandObject) -> None:
        """Publish one archive post immediately, outside the schedule."""
        argument = (command.args or "").strip().lower()
        if not argument:
            await message.answer(POST_USAGE)
            return

        category = POST_ALIASES.get(argument)
        if category is None and argument != POST_RANDOM:
            await message.answer(f"❌ Unknown type “{argument}”.\n{POST_USAGE}")
            return

        try:
            if category is None:
                result = await ctx.scheduler.publish_random()
            else:
                result = await ctx.scheduler.publish_now(category)
        except Exception:
            # The scheduler already handles Telegram errors; this is a net for
            # anything unforeseen so the admin always gets an answer.
            log.exception("Manual /post failed")
            await message.answer("❌ The repost failed unexpectedly; check the logs.")
            return

        if result.ok and result.post is not None:
            reply = POST_SUCCESS.get(result.post.category, "✅ یک پست تصادفی منتشر شد.")
            if result.reason:
                reply += f"\n({result.reason})"  # only set when a cooldown was relaxed
            await message.answer(reply)
            return

        if result.post is not None:
            # A post was chosen but Telegram refused to copy it.
            await message.answer(f"❌ Publish failed: {result.reason}")
        elif category is None:
            await message.answer(POST_EMPTY_ANY)
        else:
            await message.answer(POST_EMPTY.get(category, POST_EMPTY_ANY))

    # ---------------------------------------------------------------- settings

    @router.message(Command("settings"), *admin_only)
    async def cmd_settings(message: Message) -> None:
        """Show the scheduling settings and where each value comes from."""
        await message.answer(format_settings(ctx.settings, read_overrides(ctx.db)))

    @router.message(Command(*SETTING_COMMANDS), *admin_only)
    async def cmd_set_setting(message: Message, command: CommandObject) -> None:
        """Change one scheduling setting; /set_<name> default restores .env."""
        key = SETTING_COMMANDS.get((command.command or "").lower())
        if key is None:  # unreachable: the filter only matches known commands
            await message.answer(SETTINGS_USAGE)
            return

        try:
            summary = ctx.scheduler.change_setting(key, command.args or "")
        except SettingError as exc:
            await message.answer(f"❌ {exc}\n{BY_KEY[key].usage}")
            return
        except Exception:
            # A failed write must still leave the admin with an answer.
            log.exception("Could not apply the %s setting", key)
            await message.answer("❌ The setting could not be saved; check the logs.")
            return

        await message.answer(f"✅ {summary}")

    @router.message(Command("archive"), *admin_only)
    async def cmd_archive(message: Message) -> None:
        await message.answer(archive_overview(ctx))

    @router.message(Command("list"), *admin_only)
    async def cmd_list(message: Message, command: CommandObject) -> None:
        argument = (command.args or "").strip().upper()
        if argument and argument not in CATEGORIES:
            await message.answer(f"Unknown category {argument}. Use one of: {', '.join(CATEGORIES)}")
            return
        posts = ctx.db.recent_posts(15, argument or None)
        if not posts:
            await message.answer("No archive entries yet.")
            return
        lines = [f"Latest archive entries{' — ' + argument if argument else ''}:"]
        for post in posts:
            album = f" ({len(post.message_ids)} msgs)" if post.is_album else ""
            last = (post.last_reposted_at or "")[:10] or "never"
            lines.append(
                f"#{post.label} · {post.category}/{post.media_type}{album} "
                f"· reposts {post.repost_count} · last {last}"
            )
        await message.answer("\n".join(lines))

    # ----------------------------------------------------------------- import

    @router.message(Command("import"), *admin_only)
    async def cmd_import(message: Message) -> None:
        await message.answer(
            "📥 Importing old posts\n\n"
            "The Bot API cannot list a channel's history, so there are two ways "
            "to fill the archive:\n\n"
            "1. Forwarding (recommended, groups albums correctly)\n"
            "   Open the channel, select old posts — you can select many at once "
            "— and forward them here. Every album arrives as one archive entry.\n\n"
            "2. /scan (walks the channel by message id)\n"
            "   /scan 1 1500 — probes ids 1..1500 and archives what still exists.\n"
            "   Slower, and albums are stored as separate single messages.\n\n"
            "New posts published in the channel are archived automatically."
        )

    @router.message(F.forward_origin, *admin_only)
    async def on_forwarded(message: Message) -> None:
        if not ctx.settings.import_enabled:
            await message.answer("Importing is disabled (IMPORT_ENABLED=false).")
            return

        origin = message.forward_origin
        if not isinstance(origin, MessageOriginChannel):
            await message.answer(
                "I can only import posts forwarded from a channel. "
                "This one was forwarded from a user or hidden source, so Telegram "
                "does not tell me which message it was."
            )
            return

        if not is_allowed_source(ctx, origin.chat):
            await message.answer(
                f"“{origin.chat.title}” is not a configured source channel, so I "
                f"will not archive it.\n"
                f"Add its id ({origin.chat.id}) to EXTRA_SOURCE_CHAT_IDS in .env "
                f"if you really want it in the archive."
            )
            return

        if not message_has_content(message):
            await message.answer("That message has no content I can repost, so I skipped it.")
            return

        media_type = detect_media_type(message)
        post_id = ctx.db.upsert_post(
            chat_id=origin.chat.id,
            group_key=f"mg:{message.media_group_id}" if message.media_group_id else f"msg:{origin.message_id}",
            message_id=origin.message_id,
            media_type=media_type,
            category=media_type_to_category(media_type),
            original_date=origin.date.isoformat() if origin.date else None,
            has_caption=has_caption(message),
            now=ctx.scheduler.now,
            reactivate=True,  # an explicit import revives a post disabled by an error
        )
        post = ctx.db.get_post(post_id)
        album = f" (album of {len(post.message_ids)})" if post and post.is_album else ""
        log.info("Imported message %s as %s%s", origin.message_id, media_type, album)
        schedule_import_ack(ctx, message.chat.id)

    # ------------------------------------------------------------------- scan

    @router.message(Command("scan"), *admin_only)
    async def cmd_scan(message: Message, command: CommandObject) -> None:
        await handle_scan(ctx, message, command.args or "")

    # ------------------------------------------------------- live channel feed

    @router.channel_post(*channel_only)
    async def on_channel_post(message: Message) -> None:
        chat_id = message.chat.id
        if ctx.gateway.is_own_repost(message.message_id) or ctx.db.is_own_repost(
            chat_id, message.message_id
        ):
            return  # this is one of our own reposts, not original content
        if ctx.gateway.in_send_window():
            # We are (or just were) publishing.  A copy's update can reach us
            # before the copy call returns, so ignore channel traffic here.
            log.debug("Ignoring channel post %s during a send window", message.message_id)
            return
        if not message_has_content(message):
            return

        media_type = detect_media_type(message)
        post_id = ctx.db.upsert_post(
            chat_id=chat_id,
            group_key=f"mg:{message.media_group_id}" if message.media_group_id else f"msg:{message.message_id}",
            message_id=message.message_id,
            media_type=media_type,
            category=media_type_to_category(media_type),
            original_date=message.date.isoformat() if message.date else None,
            has_caption=has_caption(message),
            now=ctx.scheduler.now,
        )
        log.debug("Archived new channel post %s as %s (row %s)", message.message_id, media_type, post_id)

    # ---------------------------------------------------------------- fallback

    @router.message(*admin_only)
    async def admin_fallback(message: Message) -> None:
        await message.answer(
            "Unknown command. Try /status, /today, /next, /skip, /post or /settings."
        )

    public = Router(name="public")

    @public.message(F.chat.type == "private")
    async def unauthorized(message: Message) -> None:
        log.warning(
            "Ignored message from unauthorized user %s",
            message.from_user.id if message.from_user else "?",
        )
        await message.answer("⛔️ This bot is private.")

    return router, public


def is_allowed_source(ctx: AppContext, chat: Chat) -> bool:
    allowed = {str(chat_id) for chat_id in ctx.settings.extra_source_chat_ids}
    if ctx.gateway.channel_numeric_id is not None:
        allowed.add(str(ctx.gateway.channel_numeric_id))
    configured = str(ctx.settings.channel_id)
    allowed.add(configured)
    allowed.add(configured.lstrip("@"))
    allowed.update(value.lstrip("@").lower() for value in list(allowed))
    if chat.username:
        allowed.add(chat.username.lower())
    return str(chat.id) in allowed or (chat.username or "").lower() in allowed


def schedule_import_ack(ctx: AppContext, chat_id: int) -> None:
    """Acknowledge a burst of forwards with one message instead of one per post.

    Forwarding a 30-post selection -- or a 10-photo album, which arrives as ten
    separate updates -- would otherwise produce thirty replies.
    """
    batch = ctx.import_batches.setdefault(chat_id, ImportBatch())
    batch.count += 1
    if batch.task is not None and not batch.task.done():
        batch.task.cancel()
    batch.task = asyncio.create_task(_flush_import_ack(ctx, chat_id))


async def _flush_import_ack(ctx: AppContext, chat_id: int) -> None:
    try:
        await asyncio.sleep(IMPORT_SETTLE_SECONDS)
    except asyncio.CancelledError:
        raise  # another message arrived; a newer task will report instead

    batch = ctx.import_batches.pop(chat_id, None)
    if batch is None:
        return
    await ctx.gateway.send_admin(
        f"✅ Archived {batch.count} message(s).\n"
        f"Archive now holds {ctx.db.archive_size()} posts — /archive for the breakdown."
    )


# ------------------------------------------------------------------------ scan


async def handle_scan(ctx: AppContext, message: Message, args: str) -> None:
    parts = args.split()

    if parts and parts[0].lower() in {"stop", "cancel"}:
        if ctx.scan_task and not ctx.scan_task.done():
            ctx.scan_stop.set()
            await message.answer("🛑 Stopping the scan…")
        else:
            await message.answer("No scan is running.")
        return

    if parts and parts[0].lower() in {"status", "progress"}:
        cursor = ctx.db.get_int("scan_cursor", 0)
        target = ctx.db.get_int("scan_target", 0)
        running = bool(ctx.scan_task and not ctx.scan_task.done())
        await message.answer(
            f"🔍 Scan {'running' if running else 'idle'}\n"
            f"cursor {cursor} / {target or '?'}\n"
            f"found {ctx.db.get_int('scan_found', 0)} posts"
        )
        return

    if ctx.scan_task and not ctx.scan_task.done():
        await message.answer("A scan is already running. Use /scan stop first.")
        return

    if not parts:
        await message.answer(
            "🔍 Building the archive\n\n"
            "The Bot API has no method to list a channel's history, so /scan walks "
            "the channel one message id at a time and keeps whatever still exists.\n\n"
            "Usage:\n"
            "   /scan <from_id> <to_id>   e.g. /scan 1 1500\n"
            "   /scan stop\n"
            "   /scan status\n\n"
            "Notes:\n"
            "• Each probe briefly copies a post into this chat (or SCAN_CHAT_ID) and "
            "deletes it again, so expect some notification noise.\n"
            "• Albums are archived as individual messages this way; forwarding them "
            "here instead keeps each album together.\n"
            "• You can stop and rerun it — it resumes from the last id it checked."
        )
        return

    try:
        start = int(parts[0])
        end = int(parts[1]) if len(parts) > 1 else ctx.db.get_int("scan_target", 0)
    except ValueError:
        await message.answer("Usage: /scan <from_id> <to_id>")
        return

    if end < start:
        await message.answer("The end id must be greater than the start id.")
        return
    if end - start > MAX_SCAN_RANGE:
        await message.answer(
            f"That range is too large ({end - start} ids). Split it into chunks of "
            f"{MAX_SCAN_RANGE} or fewer."
        )
        return

    ctx.scan_stop = asyncio.Event()
    ctx.db.set_state("scan_cursor", start)
    ctx.db.set_state("scan_target", end)
    ctx.db.set_state("scan_found", 0)
    ctx.scan_task = asyncio.create_task(_scan_worker(ctx, start, end))
    await message.answer(
        f"🔍 Scanning ids {start}…{end}. I will report when it finishes. "
        f"/scan stop to cancel, /scan status for progress."
    )


async def _scan_worker(ctx: AppContext, start: int, end: int) -> None:
    settings = ctx.settings
    probe_chat = settings.scan_chat_id or settings.admin_user_id
    interval = settings.scan_interval_seconds
    found = 0
    consecutive_errors = 0
    cursor = start
    log.info("Scan started: ids %s..%s", start, end)

    try:
        if ctx.gateway.channel_numeric_id is None:
            await ctx.gateway.resolve_channel()
        source_chat_id = ctx.gateway.channel_numeric_id
        if source_chat_id is None:
            await ctx.gateway.send_admin("❌ Scan aborted: the channel could not be resolved.")
            return

        while cursor <= end and not ctx.scan_stop.is_set():
            try:
                sent = await ctx.gateway.probe_message(source_chat_id, cursor, probe_chat)
            except Exception as exc:
                error = classify(exc)
                if isinstance(error, TransientRepostError):
                    consecutive_errors += 1
                    if consecutive_errors >= 10:
                        log.error("Scan aborted after repeated transient errors: %s", error)
                        await ctx.gateway.send_admin(f"❌ Scan aborted: {error}")
                        return
                    await asyncio.sleep(min(30.0, 2.0 * consecutive_errors))
                    continue
                if isinstance(error, ChannelError):
                    log.error("Scan aborted, channel unreachable: %s", error)
                    await ctx.gateway.send_admin(f"❌ Scan aborted: {error}")
                    return
                # Permanent: this id has no message (deleted, or never existed).
            else:
                consecutive_errors = 0
                media_type = detect_media_type(sent)
                if media_type != "other":
                    ctx.db.upsert_post(
                        chat_id=int(source_chat_id),
                        group_key=f"msg:{cursor}",
                        message_id=cursor,
                        media_type=media_type,
                        category=media_type_to_category(media_type),
                        # copyMessage does not preserve the original post date
                        original_date=None,
                        has_caption=has_caption(sent),
                        now=ctx.scheduler.now,
                    )
                    found += 1
                    ctx.db.set_state("scan_found", ctx.db.get_int("scan_found", 0) + 1)

            cursor += 1
            ctx.db.set_state("scan_cursor", cursor)
            if cursor % 100 == 0:
                log.info("Scan progress: %s/%s, found %s", cursor, end, found)
            if interval:
                await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Scan crashed")
        await ctx.gateway.send_admin("❌ The scan crashed; check the logs.")
        return

    stopped = ctx.scan_stop.is_set()
    log.info("Scan finished: %s posts found (stopped=%s)", found, stopped)
    await ctx.gateway.send_admin(
        f"{'🛑 Scan stopped' if stopped else '✅ Scan finished'}\n"
        f"ids checked up to {cursor - 1}, {found} posts archived\n"
        f"archive total: {ctx.db.archive_size()}"
    )
