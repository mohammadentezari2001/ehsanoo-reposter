"""Daily schedule generation and the posting loop."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import Settings
from .database import (
    FAILED,
    MISSED,
    PENDING,
    POSTED,
    SENDING,
    UNKNOWN,
    Database,
    Post,
    Slot,
)
from .selector import plan_categories, select_post
from .settings_store import (
    BY_KEY,
    RESET_WORDS,
    applied_line,
    clear_override,
    effective_settings,
    parse_value,
    read_overrides,
    save_override,
    settings_signature,
    settings_summary,
)
from .telegram import (
    ChannelError,
    PermanentRepostError,
    TelegramGateway,
    classify,
    describe,
)

log = logging.getLogger(__name__)

#: How often the posting loop wakes up.
TICK_SECONDS = 20

#: Transient failures tolerated per slot before the slot is abandoned.
MAX_TRANSIENT_FAILURES = 5

#: Days of schedule history kept in SQLite.
SCHEDULE_RETENTION_DAYS = 30


@dataclass
class PublishResult:
    """Outcome of one publish attempt.

    Shared by the scheduled slots and by the manual ``/post`` command, so both
    go through exactly the same selection and copy code.
    """

    ok: bool
    post: Post | None = None
    #: Why the fallback/relaxation happened, or why publishing failed.
    reason: str = ""


def generate_times(settings: Settings, day: date, rng: random.Random) -> list[datetime]:
    """Pick ``POSTS_PER_DAY`` natural-looking times inside the daily window.

    The window is split into equal slots; one post is drawn from each slot.  A
    guard band of ``RANDOMIZATION_MINUTES`` (capped at 15% of a slot) is kept
    away from the slot edges so posts never bunch up or fall outside the
    window.  ``MIN_INTERVAL_MINUTES`` is then enforced on the sorted result.
    """
    count = settings.posts_per_day
    midnight = datetime.combine(day, time.min, tzinfo=settings.timezone)
    window_start = midnight + timedelta(minutes=settings.start_minutes)
    window_end = midnight + timedelta(minutes=settings.end_minutes)
    step = timedelta(minutes=settings.slot_minutes)

    guard = timedelta(minutes=min(settings.randomization_minutes, settings.slot_minutes * 0.15))
    guard = max(guard, timedelta(0))

    times: list[datetime] = []
    for index in range(count):
        slot_start = window_start + step * index
        low = slot_start + guard
        high = slot_start + step - guard
        if high < low:
            low = high = slot_start + step / 2
        span = (high - low).total_seconds()
        offset = rng.uniform(0, span) if span > 0 else 0.0
        moment = low + timedelta(seconds=offset)
        # Nudge off the exact minute so times look human rather than generated.
        moment += timedelta(seconds=rng.randint(0, 59))
        times.append(min(max(moment, window_start), window_end))

    times.sort()
    minimum = timedelta(minutes=settings.min_interval_minutes)
    if minimum > timedelta(0):
        for index in range(1, count):
            if times[index] - times[index - 1] < minimum:
                times[index] = times[index - 1] + minimum

        if times[-1] > window_end:
            times[-1] = window_end
            for index in range(count - 2, -1, -1):
                if times[index + 1] - times[index] < minimum:
                    times[index] = times[index + 1] - minimum

        if times[0] < window_start:
            # The constraints cannot all be met; fall back to even spacing.
            log.warning(
                "MIN_INTERVAL_MINUTES=%s does not fit %s posts in the window; "
                "using even spacing",
                settings.min_interval_minutes,
                count,
            )
            if count == 1:
                times = [window_start + (window_end - window_start) / 2]
            else:
                span = window_end - window_start
                times = [window_start + span * index / (count - 1) for index in range(count)]

    return [moment.replace(microsecond=0) for moment in times]


def build_day_plan(settings: Settings, day: date, rng: random.Random) -> list[tuple[int, datetime, str]]:
    """(slot index, planned time, category) triples for one day."""
    day_iso = day.isoformat()
    times = generate_times(settings, day, rng)
    categories = plan_categories(settings, day_iso)
    return [
        (index, moment, categories[index % len(categories)])
        for index, moment in enumerate(times)
    ]


class RepostScheduler:
    """Owns the daily plan and publishes the due posts."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        gateway: TelegramGateway,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        #: The .env values, kept as read-only defaults.
        self.base_settings = settings
        #: The values actually in force: the defaults above plus whatever the
        #: admin has saved from Telegram.  Refreshed on every tick.
        self.settings = effective_settings(settings, read_overrides(db))
        if settings_signature(self.settings) != settings_signature(settings):
            log.info("Using saved settings: %s", settings_summary(self.settings))
        self.gateway = gateway
        self.rng = rng or random.Random()
        self._stopping = False
        self._busy = asyncio.Lock()
        self._transient_failures: dict[tuple[str, int], int] = {}
        self._last_maintenance: date | None = None
        self._last_sweep: date | None = None

    # ------------------------------------------------------------------ clock

    @property
    def now(self) -> datetime:
        return datetime.now(self.settings.timezone)

    # --------------------------------------------------------------- settings

    def reload_settings(self) -> Settings:
        """Adopt the currently saved settings.

        Runs on every tick, so a value changed from Telegram takes effect at
        once and nothing read from .env at startup is cached forever.  Costs one
        small SELECT per tick.
        """
        current = effective_settings(self.base_settings, read_overrides(self.db))
        if settings_signature(current) != settings_signature(self.settings):
            log.info("Scheduling settings are now %s", settings_summary(current))
        self.settings = current
        return current

    def change_setting(self, key: str, raw: str) -> str:
        """Validate, save and apply one setting; returns a confirmation line.

        Raises :class:`~bot.settings_store.SettingError` when the value -- or the
        combination it would produce -- is unusable.  Nothing is written until it
        validates, so a rejected command leaves the bot exactly as it was.
        """
        spec = BY_KEY[key]
        text = raw.strip()
        reset = text.lower() in RESET_WORDS  # "default" / "reset" -> back to .env

        overrides = dict(read_overrides(self.db))
        value: int | None = None
        if reset:
            overrides.pop(key, None)
        else:
            value = parse_value(spec, text)
            overrides[key] = value

        candidate = effective_settings(self.base_settings, overrides, strict=True)
        if reset:
            clear_override(self.db, key)
        else:
            assert value is not None  # parse_value ran, so it is set
            save_override(self.db, key, value)

        self.settings = candidate
        rebuilt = self.regenerate_future_days()
        log.info("%s %s: %s", "Reset" if reset else "Set", key, settings_summary(candidate))
        return applied_line(key, candidate) + self._change_note(rebuilt)

    def _change_note(self, rebuilt: int) -> str:
        today = self.now.date().isoformat()
        if rebuilt:
            return (
                f"\n♻️ Rebuilt the plan for {rebuilt} future day(s). Today ({today}) is "
                f"untouched, so no scheduled post is moved or lost."
            )
        return (
            f"\nℹ️ Today ({today}) is untouched. The new values apply the next time a "
            f"day is planned."
        )

    def regenerate_future_days(self) -> int:
        """Re-plan future days that nothing has happened to yet.

        Today is deliberately never rebuilt: its published slots are history and
        its pending slots are what the admin is currently expecting, so moving
        them could fire a post early or drop one.  A future day is rebuilt only
        when *every* slot is still pending -- a day with any non-pending slot
        (one dropped with /skip, for instance) is left exactly as it is.  That is
        what makes this safe: it can neither lose a post nor duplicate one.
        """
        today = self.now.date().isoformat()
        rebuilt = 0
        for day_iso in self.db.days_with_schedule():
            if day_iso <= today:
                continue
            try:
                day = date.fromisoformat(day_iso)
            except ValueError:
                continue
            if any(slot.status != PENDING for slot in self.db.get_schedule(day_iso)):
                continue
            self.db.delete_schedule(day_iso)
            self.db.insert_schedule(
                day_iso,
                build_day_plan(self.settings, day, random.Random(f"plan:{day_iso}")),
            )
            rebuilt += 1
        if rebuilt:
            log.info("Rebuilt the plan for %s future day(s) after a settings change", rebuilt)
        return rebuilt

    # --------------------------------------------------------------- planning

    def ensure_day(self, day: date) -> bool:
        """Create the plan for ``day`` if it does not exist yet."""
        day_iso = day.isoformat()
        if self.db.has_schedule(day_iso):
            return False
        plan = build_day_plan(self.settings, day, random.Random(f"plan:{day_iso}"))
        self.db.insert_schedule(day_iso, plan)
        log.info(
            "Generated schedule for %s: %s",
            day_iso,
            ", ".join(f"{moment:%H:%M} {category}" for _, moment, category in plan),
        )
        self._expire_elapsed_slots(day, plan)
        return True

    def _expire_elapsed_slots(
        self, day: date, plan: list[tuple[int, datetime, str]]
    ) -> None:
        """A plan generated mid-day should not fire the slots already gone.

        This is what keeps the very first start (or a start after a long
        outage) from dumping three posts into the channel at once.  It only
        applies at generation time -- a plan that already existed keeps its
        pending slots, so a short restart still catches up.
        """
        now = self.now
        if day != now.date():
            return
        expired = [(index, moment) for index, moment, _ in plan if moment < now]
        if not expired:
            return
        for index, moment in expired:
            self.db.update_slot(
                day.isoformat(),
                index,
                status=MISSED,
                error="plan was generated after this slot's time",
            )
        log.info(
            "Skipped %s slot(s) already past when the plan was created (up to %s)",
            len(expired),
            f"{expired[-1][1]:%H:%M}",
        )

    def _housekeeping(self, now: datetime) -> None:
        if self._last_maintenance == now.date():
            return
        self._last_maintenance = now.date()
        try:
            self.db.purge_schedule_before(now.date() - timedelta(days=SCHEDULE_RETENTION_DAYS))
            self.db.purge_reposts_before(now - timedelta(days=SCHEDULE_RETENTION_DAYS))
        except Exception:
            log.exception("Housekeeping failed")

    def recover_stale_slots(self) -> list[Slot]:
        """Slots interrupted by a crash.  Marked UNKNOWN, never re-sent."""
        stale = self.db.stale_sending()
        for slot in stale:
            self.db.update_slot(
                slot.day,
                slot.slot,
                status=UNKNOWN,
                error="interrupted by restart; not re-sent to avoid a duplicate",
            )
            log.warning(
                "Slot %s#%s was mid-send at shutdown; marked unknown", slot.day, slot.slot
            )
        return stale

    def sweep_abandoned_days(self, today: date) -> int:
        """Pending slots belonging to earlier days can never run: mark them missed."""
        if self._last_sweep == today:
            return 0
        self._last_sweep = today
        count = 0
        for day_iso in self.db.days_with_schedule():
            if day_iso >= today.isoformat():
                continue
            for slot in self.db.pending_for_day(day_iso):
                self.db.update_slot(day_iso, slot.slot, status=MISSED, error="day ended")
                count += 1
        if count:
            log.info("Marked %s abandoned slot(s) from previous days as missed", count)
        return count

    # ------------------------------------------------------------------- loop

    async def run(self) -> None:
        log.info("Scheduler started")
        try:
            while not self._stopping:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Scheduler tick failed; continuing")
                await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            log.info("Scheduler stopped")
            raise

    def stop(self) -> None:
        self._stopping = True

    async def _tick(self) -> None:
        try:
            # Anything the admin changed from Telegram (or a value saved by an
            # earlier run) is adopted before the day is planned or a slot fires.
            self.reload_settings()
        except Exception:
            log.exception("Could not reload the scheduling settings; keeping the current ones")

        now = self.now
        today = now.date()

        try:
            if self.ensure_day(today):
                await self._announce_day(today)
            # Kept ready so /next and /today can preview the next day.
            self.ensure_day(today + timedelta(days=1))
        except Exception:
            log.exception("Could not prepare the schedule")

        self._housekeeping(now)
        self.sweep_abandoned_days(today)

        if self.db.get_bool("paused", False):
            return

        due = self.db.pending_due(today.isoformat(), now)
        if not due:
            return

        minimum = timedelta(minutes=self.settings.min_interval_minutes)
        async with self._busy:
            for slot in due:
                if self._stopping:
                    return
                lateness = now - slot.planned_at
                if lateness > timedelta(minutes=self.settings.catchup_max_late_minutes):
                    self.db.update_slot(
                        slot.day,
                        slot.slot,
                        status=MISSED,
                        error=f"more than {self.settings.catchup_max_late_minutes} min late",
                    )
                    log.warning(
                        "Skipped slot %s#%s: %.0f minutes late",
                        slot.day, slot.slot, lateness.total_seconds() / 60,
                    )
                    continue

                last = self.db.last_posted_at(slot.day)
                if last is not None and minimum > timedelta(0) and (now - last) < minimum:
                    # Too soon after the previous post; retry on a later tick.
                    continue

                await self._execute(slot)

    async def _announce_day(self, day: date) -> None:
        slots = [s for s in self.db.get_schedule(day.isoformat()) if s.status == PENDING]
        if not slots:
            return
        lines = [f"🗓 New schedule for {day.isoformat()}:"]
        lines += [f"{s.planned_at:%H:%M} — {s.category}" for s in slots]
        await self.gateway.send_admin("\n".join(lines))

    # ------------------------------------------------------------------ posting

    async def _execute(self, slot: Slot) -> bool:
        """Publish one slot, retrying with a different archive item on failure."""
        settings = self.settings
        tried: list[int] = []

        for attempt in range(1, settings.max_send_attempts + 1):
            now = self.now
            used = self.db.used_post_ids(slot.day) + tried
            post, reason = select_post(
                self.db,
                settings,
                slot.category,
                now=now,
                exclude_post_ids=used,
                rng=self.rng,
            )
            if post is None:
                self.db.update_slot(slot.day, slot.slot, status=FAILED, error=reason)
                log.error("Slot %s#%s has no content: %s", slot.day, slot.slot, reason)
                await self.gateway.send_admin(
                    f"⚠️ {slot.planned_at:%H:%M} ({slot.category}) could not be posted: {reason}\n"
                    f"Populate the archive with /scan or by forwarding old posts to me."
                )
                return False

            self.db.update_slot(slot.day, slot.slot, status=SENDING, post_id=post.id, error=None)
            try:
                created = await self.gateway.copy_post(post)
            except Exception as exc:
                error = classify(exc)
                tried.append(post.id)
                if isinstance(error, PermanentRepostError):
                    self.db.disable_post(post.id, describe(exc))
                    self.db.update_slot(slot.day, slot.slot, status=PENDING, post_id=None)
                    log.warning(
                        "Archive item %s is unusable (%s); trying another",
                        post.label, error,
                    )
                    continue
                if isinstance(error, ChannelError):
                    self.db.update_slot(
                        slot.day, slot.slot, status=PENDING, post_id=None, error=str(error)
                    )
                    log.error("Cannot post to the channel: %s", error)
                    await self.gateway.send_admin(
                        f"🚫 Cannot publish to the channel: {error}\n"
                        f"Check that the bot is still an administrator with "
                        f"“Post Messages” permission."
                    )
                    return False
                # Transient: leave the slot pending so the next tick retries it.
                key = (slot.day, slot.slot)
                self._transient_failures[key] = self._transient_failures.get(key, 0) + 1
                failures = self._transient_failures[key]
                self.db.update_slot(
                    slot.day, slot.slot, status=PENDING, post_id=None, error=str(error)
                )
                log.warning(
                    "Transient failure posting %s (%s/%s): %s",
                    post.label, failures, MAX_TRANSIENT_FAILURES, error,
                )
                if failures >= MAX_TRANSIENT_FAILURES:
                    self.db.update_slot(
                        slot.day, slot.slot, status=FAILED, error=str(error)
                    )
                    await self.gateway.send_admin(
                        f"❌ Gave up on {slot.planned_at:%H:%M} ({slot.category}) "
                        f"after {failures} attempts: {error}"
                    )
                return False
            else:
                # The post is live.  Record it before anything else can fail.
                finished = self._record_success(post, created)
                self._transient_failures.pop((slot.day, slot.slot), None)
                note = reason or None
                self.db.update_slot(
                    slot.day, slot.slot,
                    status=POSTED, post_id=post.id, posted_at=finished, error=note,
                )
                log.info(
                    "Reposted message %s as %s (%s%s)",
                    post.label,
                    post.media_type,
                    slot.category,
                    f", {note}" if note else "",
                )
                return True

        self.db.update_slot(
            slot.day, slot.slot, status=FAILED, error="all attempts used"
        )
        log.error("Slot %s#%s failed after %s attempts", slot.day, slot.slot, settings.max_send_attempts)
        return False

    def _record_success(self, post: Post, created: list[int]) -> datetime:
        """Book-keeping once a copy has landed in the channel.

        Marks the archive item as used -- which is what starts its cooldown --
        and makes sure the channel listener cannot mistake our own copy for new
        original content.  Called for scheduled slots and manual posts alike.
        """
        finished = self.now
        try:
            self.db.mark_reposted(post.id, finished)
            chat_id = self.gateway.channel_numeric_id
            if chat_id is not None:
                self.db.remember_repost(chat_id, created, post.id, finished)
                # Belt and braces: if the channel listener managed to archive
                # our own copy, drop it again.
                self.db.drop_posts_containing(
                    chat_id, created, since=finished - timedelta(minutes=10)
                )
        except Exception:
            log.exception("Could not record the repost in the archive")
        return finished

    # ------------------------------------------------------------ manual post

    async def publish_now(self, category: str) -> PublishResult:
        """Pick one post of ``category`` and publish it straight away.

        This is the same selection, copy and book-keeping the scheduled slots
        use, minus the schedule: no slot is created or modified, so a manual
        ``/post`` never disturbs the day's plan.  The archive item is still
        marked as reposted, so the normal cooldown keeps it out of the next
        selection.
        """
        settings = self.settings
        tried: list[int] = []

        for _ in range(settings.max_send_attempts):
            post, reason = select_post(
                self.db,
                settings,
                category,
                now=self.now,
                exclude_post_ids=tried,
                rng=self.rng,
                # /post video means a video, not a photo that happened to be
                # in the fallback chain.
                allow_fallback=False,
            )
            if post is None:
                return PublishResult(False, None, reason)

            async with self._busy:  # never interleave with a scheduled send
                try:
                    created = await self.gateway.copy_post(post)
                except Exception as exc:
                    error = classify(exc)
                    tried.append(post.id)
                    if isinstance(error, PermanentRepostError):
                        self.db.disable_post(post.id, describe(exc))
                        log.warning(
                            "Archive item %s is unusable (%s); trying another",
                            post.label, error,
                        )
                        continue
                    log.error("Manual repost of %s failed: %s", post.label, error)
                    return PublishResult(False, post, str(error))

            self._record_success(post, created)
            log.info(
                "Manually reposted message %s as %s (%s%s)",
                post.label,
                post.media_type,
                post.category,
                f", {reason}" if reason else "",
            )
            return PublishResult(True, post, reason)

        return PublishResult(False, None, "no usable content in the archive")

    async def publish_random(self) -> PublishResult:
        """Publish one post of a randomly chosen type that still has content."""
        available = sorted(
            category
            for category, count in self.db.count_by_category().items()
            if count
        )
        if not available:
            return PublishResult(False, None, "archive is empty")
        self.rng.shuffle(available)
        result = PublishResult(False, None, "archive is empty")
        for category in available:
            result = await self.publish_now(category)
            if result.ok or result.post is not None:
                # Published, or the copy itself was refused -- the latter is not
                # specific to this type, so do not hammer the API five times.
                return result
        return result

    # ------------------------------------------------------------- admin verbs

    def pause(self) -> None:
        self.db.set_bool("paused", True)

    def resume(self) -> None:
        self.db.set_bool("paused", False)

    @property
    def paused(self) -> bool:
        return self.db.get_bool("paused", False)

    def skip_next(self) -> Slot | None:
        """Drop the next pending slot and return it."""
        now = self.now
        today = now.date().isoformat()
        slot = self.db.next_pending(today, now)
        if slot is None:
            tomorrow = (now.date() + timedelta(days=1)).isoformat()
            pending = self.db.pending_for_day(tomorrow)
            slot = pending[0] if pending else None
        if slot is None:
            return None
        self.db.update_slot(
            slot.day, slot.slot, status=MISSED, error="skipped by admin"
        )
        log.info("Skipped slot %s#%s (%s)", slot.day, slot.slot, slot.category)
        return slot

    def next_slot(self) -> Slot | None:
        now = self.now
        today = now.date()
        slot = self.db.next_pending(today.isoformat(), now)
        if slot is not None:
            return slot
        return self.db.next_pending((today + timedelta(days=1)).isoformat(), now)
