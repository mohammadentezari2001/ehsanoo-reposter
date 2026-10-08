"""Admin-editable scheduling settings.

``.env`` supplies the *defaults*.  Anything the admin changes from Telegram is
stored in the ``state`` table and layered on top of those defaults, producing an
ordinary frozen :class:`~bot.config.Settings`.  Every consumer -- the scheduler,
the selector, the handlers -- therefore keeps reading plain attributes and never
needs to know where a value came from.

Keeping the values in SQLite rather than in memory is what makes them survive a
restart: the bot re-reads the stored overrides at startup and on every scheduler
tick, so `.env` can never win over a value the admin set.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace

from .config import (
    ConfigError,
    Settings,
    format_clock,
    parse_clock,
    validate_window,
)
from .database import Database

log = logging.getLogger(__name__)

#: Marks the rows of the ``state`` table that hold scheduling overrides.
STATE_PREFIX = "setting."

#: ``/set_cooldown default`` drops the override and returns to the .env value.
RESET_WORDS = frozenset({"default", "reset"})


class SettingError(RuntimeError):
    """A setting could not be applied; the message is shown to the admin."""


@dataclass(frozen=True)
class SettingSpec:
    """One editable setting: how to parse it, bound it, and describe it."""

    key: str
    """Command suffix and state key suffix, e.g. ``posts_per_day``."""

    field: str
    """The :class:`Settings` attribute it maps to."""

    kind: str
    """``"int"`` or ``"time"``."""

    minimum: int
    maximum: int
    example: str
    label: str
    #: Only an end-of-window bound may be 24:00.
    allow_end_of_day: bool = False

    @property
    def usage(self) -> str:
        return f"Usage: /set_{self.key} {self.example}"

    def accepts(self, value: int) -> bool:
        return self.minimum <= value <= self.maximum

    def range_message(self, value: int) -> str:
        if self.kind == "time":
            return (
                f"{self.label} must be between {format_clock(self.minimum)} and "
                f"{format_clock(self.maximum)} (got {format_clock(value)})."
            )
        return (
            f"{self.label} must be between {self.minimum} and {self.maximum} "
            f"(got {value})."
        )


#: Every setting the admin can change from Telegram.  Adding one here is enough
#: to get a ``/set_<key>`` command, a validated parser and a /settings line.
SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("posts_per_day", "posts_per_day", "int", 1, 48, "6", "Posts per day"),
    SettingSpec("start_time", "start_minutes", "time", 0, 1439, "08:00", "Start time"),
    SettingSpec(
        "end_time", "end_minutes", "time", 1, 1440, "23:00", "End time",
        allow_end_of_day=True,
    ),
    SettingSpec(
        "min_interval", "min_interval_minutes", "int", 0, 1440, "90", "Minimum interval"
    ),
    SettingSpec(
        "randomization", "randomization_minutes", "int", 0, 1440, "45", "Randomization"
    ),
    SettingSpec(
        "cooldown", "repost_cooldown_days", "int", 0, 3650, "7", "Repost cooldown"
    ),
)

BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SPECS}

#: ``/set_posts_per_day`` -> ``posts_per_day``, for the command filter.
COMMANDS: dict[str, str] = {f"set_{spec.key}": spec.key for spec in SPECS}

SETTINGS_USAGE = "Usage: " + " | ".join(f"/set_{spec.key} {spec.example}" for spec in SPECS)


# ------------------------------------------------------------------ persistence


def read_overrides(db: Database) -> dict[str, int]:
    """The saved overrides, keyed by spec key.  Unreadable rows are ignored."""
    overrides: dict[str, int] = {}
    for key, raw in db.get_prefixed_state(STATE_PREFIX).items():
        try:
            overrides[key] = int(raw)
        except ValueError:
            log.warning("Ignoring unreadable stored setting %s=%r", key, raw)
    return overrides


def save_override(db: Database, key: str, value: int) -> None:
    db.set_state(STATE_PREFIX + key, int(value))


def clear_override(db: Database, key: str) -> None:
    db.delete_state(STATE_PREFIX + key)


# -------------------------------------------------------------------- building


def effective_settings(
    base: Settings,
    overrides: Mapping[str, object],
    *,
    strict: bool = False,
) -> Settings:
    """Layer stored overrides over the ``.env`` defaults.

    ``strict`` is for the command path, where a bad value must be reported to the
    admin.  Without it a value that no longer fits (say ``.env`` shrank the
    window after ``/set_posts_per_day 48``) is skipped and the bot keeps running
    on the defaults instead of refusing to start.
    """
    changes: dict[str, int] = {}
    for key, raw in overrides.items():
        spec = BY_KEY.get(key)
        if spec is None:
            continue  # a setting that no longer exists
        try:
            value = int(raw)  # every state row is text
        except (TypeError, ValueError):
            continue
        if not spec.accepts(value):
            if strict:
                raise SettingError(spec.range_message(value))
            continue
        changes[spec.field] = value

    if not changes:
        return base

    candidate = replace(base, **changes)
    try:
        validate_window(candidate.start_minutes, candidate.end_minutes, candidate.posts_per_day)
    except ConfigError as exc:
        if strict:
            raise SettingError(str(exc)) from exc
        log.error("Saved scheduling settings are unusable (%s); using .env defaults", exc)
        return base
    return candidate


def parse_value(spec: SettingSpec, raw: str) -> int:
    """Validate one raw argument, or raise :class:`SettingError`."""
    text = raw.strip()
    if not text:
        raise SettingError("A value is required.")
    if spec.kind == "time":
        try:
            value = parse_clock(text, allow_end_of_day=spec.allow_end_of_day)
        except ConfigError as exc:
            raise SettingError(str(exc)) from exc
    else:
        try:
            value = int(text)
        except ValueError:
            raise SettingError(f"{text!r} is not a whole number.") from None
    if not spec.accepts(value):
        raise SettingError(spec.range_message(value))
    return value


# -------------------------------------------------------------------- display


def settings_signature(settings: Settings) -> tuple[int, ...]:
    """Everything a saved override can change, for cheap change detection."""
    return (
        settings.posts_per_day,
        settings.start_minutes,
        settings.end_minutes,
        settings.min_interval_minutes,
        settings.randomization_minutes,
        settings.repost_cooldown_days,
    )


def settings_summary(settings: Settings) -> str:
    """One dense line, for the log and for /status."""
    return (
        f"{settings.posts_per_day}/day, {settings.start_clock}→{settings.end_clock}, "
        f"gap {settings.min_interval_minutes}m, "
        f"jitter ±{settings.randomization_minutes}m, "
        f"cooldown {settings.repost_cooldown_days}d"
    )


def format_settings(settings: Settings, overridden: Collection[str] = ()) -> str:
    """The /settings text."""
    lines = [
        "⚙️ Scheduling Settings",
        "",
        f"📌 Posts per day: {settings.posts_per_day}",
        f"🕐 Time window: {settings.start_clock} → {settings.end_clock}",
        f"⏱ Minimum interval: {settings.min_interval_minutes} minutes",
        f"🎲 Randomization: ±{settings.randomization_minutes} minutes",
        f"🔄 Repost cooldown: {settings.repost_cooldown_days} days",
    ]
    if overridden:
        lines.append("")
        lines.append(
            f"✏️ {len(overridden)} value(s) set from Telegram; the rest come from .env."
        )
    lines.append("")
    lines.append(
        "Change one with /set_posts_per_day, /set_start_time, /set_end_time, "
        "/set_min_interval, /set_randomization or /set_cooldown.\n"
        "Use “default” as the value (e.g. /set_cooldown default) to go back to .env."
    )
    return "\n".join(lines)


def applied_line(key: str, settings: Settings) -> str:
    """The one-line confirmation for a change, in the /settings vocabulary."""
    if key in {"start_time", "end_time"}:
        return f"🕐 Time window: {settings.start_clock} → {settings.end_clock}"
    if key == "posts_per_day":
        return f"📌 Posts per day: {settings.posts_per_day}"
    if key == "min_interval":
        return f"⏱ Minimum interval: {settings.min_interval_minutes} minutes"
    if key == "randomization":
        return f"🎲 Randomization: ±{settings.randomization_minutes} minutes"
    if key == "cooldown":
        return f"🔄 Repost cooldown: {settings.repost_cooldown_days} days"
    return settings_summary(settings)
