"""Entry point: wires configuration, storage, scheduler and Telegram together."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError

from .config import ConfigError, Settings, load_settings
from .database import Database
from .handlers import AppContext, build_router
from .scheduler import RepostScheduler
from .telegram import TelegramGateway, describe

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

#: Retries for the startup check, for a network that is not up yet after a reboot.
STARTUP_ATTEMPTS = 3
STARTUP_RETRY_SECONDS = 10


def setup_logging(settings: Settings) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if settings.log_file is not None:
        try:
            settings.log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                settings.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("Could not open the log file %s: %s", settings.log_file, exc)

    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def check_channel(bot: Bot, settings: Settings, gateway: TelegramGateway) -> list[str]:
    """Validate the token, the channel and the bot's rights.  Returns warnings."""
    warnings: list[str] = []

    me = await bot.get_me()
    log.info("Authorised as @%s (id %s)", me.username, me.id)

    await bot.delete_webhook(drop_pending_updates=False)

    try:
        chat = await gateway.resolve_channel()
    except Exception as exc:
        raise ConfigError(
            f"CHANNEL_ID={settings.channel_id!r} is not reachable: {describe(exc)}. "
            f"Double-check the id/username and make sure the bot is a member of the channel."
        ) from exc

    log.info("Channel resolved: %s (id %s)", chat.title, chat.id)

    try:
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception as exc:
        warnings.append(f"could not read my own permissions: {describe(exc)}")
        return warnings

    status = getattr(member, "status", "unknown")
    if status not in {"administrator", "creator"}:
        warnings.append(
            f"the bot is only “{status}” in the channel — it needs to be an "
            f"administrator with the “Post Messages” permission"
        )
    elif getattr(member, "can_post_messages", False) is False:
        warnings.append("the bot is an administrator but “Post Messages” is disabled")

    if getattr(member, "can_delete_messages", False) is False:
        warnings.append("“Delete Messages” is off — /scan will leave probe copies behind")

    return warnings


def banner(settings: Settings, channel_title: str, archive_size: int, warnings: list[str]) -> None:
    lines = [
        "",
        "  Telegram Reposter",
        "  ─────────────────────────────────────────────",
        f"  channel        {channel_title} ({settings.channel_id})",
        f"  timezone       {settings.timezone_name}",
        f"  window         {settings.start_clock} → {settings.end_clock}",
        f"  posts per day  {settings.posts_per_day}  ({', '.join(settings.daily_mix)})",
        f"  min interval   {settings.min_interval_minutes} min "
        f"(±{settings.randomization_minutes} min jitter)",
        f"  cooldown       {settings.repost_cooldown_days} days",
        f"  archive        {archive_size} posts",
        f"  database       {settings.database_path}",
        "  ─────────────────────────────────────────────",
    ]
    for warning in warnings:
        lines.append(f"  ⚠️  {warning}")
    if warnings:
        lines.append("  ─────────────────────────────────────────────")
    print("\n".join(lines), flush=True)


async def run() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2

    setup_logging(settings)
    log.info("Starting up (timezone %s)", settings.timezone_name)

    db = Database(settings.database_path)
    db.connect()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=None))
    gateway = TelegramGateway(bot, settings.channel_id, settings.admin_user_id)
    scheduler = RepostScheduler(db, settings, gateway)
    ctx = AppContext(
        base_settings=settings, db=db, gateway=gateway, scheduler=scheduler,
        started_at=datetime.now(settings.timezone),
    )

    warnings: list[str] = []
    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        try:
            warnings = await check_channel(bot, settings, gateway)
            break
        except TelegramUnauthorizedError:
            log.error("BOT_TOKEN was rejected by Telegram")
            print("BOT_TOKEN was rejected by Telegram. Check .env.", file=sys.stderr)
            await bot.session.close()
            db.close()
            return 2
        except ConfigError as exc:
            log.error("%s", exc)
            print(f"Configuration error: {exc}", file=sys.stderr)
            await bot.session.close()
            db.close()
            return 2
        except Exception as exc:
            # Usually a network that is not up yet right after a reboot.
            log.warning(
                "Startup check failed (%s/%s): %s", attempt, STARTUP_ATTEMPTS, describe(exc)
            )
            if attempt == STARTUP_ATTEMPTS:
                print(f"Startup failed: {describe(exc)}", file=sys.stderr)
                await bot.session.close()
                db.close()
                return 2
            await asyncio.sleep(STARTUP_RETRY_SECONDS)

    # A slot interrupted by a crash must never be sent twice.
    stale = scheduler.recover_stale_slots()
    if stale:
        await gateway.send_admin(
            f"♻️ Restarted. {len(stale)} post(s) were interrupted mid-send and were "
            f"NOT re-sent, to avoid duplicates. Check the channel."
        )

    today = scheduler.now.date()
    scheduler.ensure_day(today)
    # The banner shows what is actually in force, which is the .env defaults
    # with anything saved from Telegram layered on top.
    banner(scheduler.settings, await gateway.channel_title(), db.archive_size(), warnings)
    log.info("Archive holds %s repostable posts", db.archive_size())

    if db.archive_size() == 0:
        log.warning("Archive is empty — nothing can be reposted yet")
        await gateway.send_admin(
            "👋 I am online, but the archive is empty.\n"
            "Forward old posts from the channel to me, or run /scan <from> <to>."
        )

    dispatcher = Dispatcher()
    admin_router, public_router = build_router(ctx)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(public_router)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler")
    polling_task = asyncio.create_task(
        dispatcher.start_polling(
            bot,
            ctx=ctx,  # injected into every handler and filter as `ctx`
            allowed_updates=["message", "channel_post"],
            polling_timeout=settings.poll_timeout,
        ),
        name="polling",
    )

    tasks = [scheduler_task, polling_task]
    stop_task = asyncio.create_task(stop.wait(), name="shutdown")
    tasks.append(stop_task)

    exit_code = 0
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for finished in done:
            if finished is stop_task:
                continue
            exc = finished.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                log.error("Task %s stopped unexpectedly", finished.get_name(), exc_info=exc)
                exit_code = 1
            else:
                log.warning("Task %s stopped", finished.get_name())
    finally:
        log.info("Shutting down")
        scheduler.stop()
        ctx.scan_stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await bot.session.close()
        except Exception:
            log.debug("Bot session close failed", exc_info=True)
        db.close()
        log.info("Stopped")

    return exit_code


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:
        raise SystemExit(0)


if __name__ == "__main__":
    main()
