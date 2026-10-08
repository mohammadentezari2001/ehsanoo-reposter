"""Configuration loading and validation.

Every setting comes from an environment variable (optionally supplied through a
``.env`` file).  Nothing is hardcoded and the bot token is never logged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Content categories understood by the selector.
CATEGORIES = ("TEXT", "IMAGE", "VIDEO", "AUDIO", "FILE")

#: Guarantees one of each per day when POSTS_PER_DAY == 4.
DEFAULT_DAILY_MIX = ("TEXT", "IMAGE", "VIDEO", "AUDIO")
#: Used to fill the plan when POSTS_PER_DAY > len(daily mix).
EXTRA_MIX = ("FILE", "IMAGE", "VIDEO", "AUDIO", "TEXT")

#: Minutes in a day.  A window may end at 1440 (= 24:00, midnight), which is
#: what END_HOUR=24 always meant.
MAX_WINDOW_MINUTES = 1440

_CLOCK_RE = re.compile(r"(\d{1,2}):([0-5]\d)")


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or invalid."""


def format_clock(minutes: int) -> str:
    """``510`` -> ``"08:30"``; 1440 renders as ``"24:00"``."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_clock(raw: str, *, allow_end_of_day: bool = False) -> int:
    """Parse ``HH:MM`` into minutes after midnight.

    ``24:00`` is only accepted where it makes sense -- the end of a window, not
    its start -- because that is the one way to say "until midnight".
    """
    text = raw.strip()
    match = _CLOCK_RE.fullmatch(text)
    if match is None:
        raise ConfigError(f"{text!r} is not a valid time — use HH:MM, e.g. 08:00")
    minutes = int(match.group(1)) * 60 + int(match.group(2))
    if minutes == MAX_WINDOW_MINUTES:
        if allow_end_of_day:
            return minutes
        raise ConfigError("a window cannot start at 24:00")
    if minutes > MAX_WINDOW_MINUTES:
        raise ConfigError(f"{text!r} is not a valid time — a day has 24:00 hours")
    return minutes


def validate_window(start_minutes: int, end_minutes: int, posts_per_day: int) -> None:
    """Reject a posting window that cannot hold the requested number of posts.

    Shared by the ``.env`` loader and by the Telegram settings commands so both
    refuse the same combinations with the same wording.
    """
    if end_minutes <= start_minutes:
        raise ConfigError(
            f"End time ({format_clock(end_minutes)}) must be after "
            f"start time ({format_clock(start_minutes)})"
        )
    window = end_minutes - start_minutes
    if window < posts_per_day:
        raise ConfigError(
            f"{posts_per_day} posts per day do not fit in a {window}-minute "
            f"window ({format_clock(start_minutes)} → {format_clock(end_minutes)})"
        )


def _raw(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _str(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = _raw(name)
    if value is None:
        if required:
            raise ConfigError(f"Missing required environment variable: {name}")
        return default
    return value


def _int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = _raw(name)
    if value is None:
        result = default
    else:
        try:
            result = int(value)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {value!r}") from exc

    if minimum is not None and result < minimum:
        raise ConfigError(f"{name} must be >= {minimum} (got {result})")
    if maximum is not None and result > maximum:
        raise ConfigError(f"{name} must be <= {maximum} (got {result})")
    return result


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    value = _raw(name)
    if value is None:
        result = default
    else:
        try:
            result = float(value)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if minimum is not None and result < minimum:
        raise ConfigError(f"{name} must be >= {minimum} (got {result})")
    return result


def _bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> tuple[str, ...]:
    value = _raw(name)
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _optional_int(name: str) -> int | None:
    value = _raw(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _mix_base(override: str | None) -> tuple[str, ...]:
    """The daily mix *pattern* from ``.env``, before it is padded per day.

    Stored unpadded so ``POSTS_PER_DAY`` can be changed at runtime (from
    Telegram) without re-reading ``.env``; :attr:`Settings.daily_mix` pads the
    pattern on demand.
    """
    if not override:
        return DEFAULT_DAILY_MIX
    base = [part.strip().upper() for part in override.replace(" ", "").split(",") if part.strip()]
    unknown = [c for c in base if c not in CATEGORIES]
    if unknown:
        raise ConfigError(
            f"DAILY_MIX contains unknown categories: {', '.join(unknown)}. "
            f"Allowed: {', '.join(CATEGORIES)}"
        )
    if not base:
        raise ConfigError("DAILY_MIX must not be empty")
    return tuple(base)


def pad_mix(base: tuple[str, ...], count: int) -> tuple[str, ...]:
    """Exactly ``count`` category names, cycling ``EXTRA_MIX`` once base runs out."""
    mix: list[str] = []
    index = 0
    while len(mix) < count:
        if index < len(base):
            mix.append(base[index])
        else:
            mix.append(EXTRA_MIX[(index - len(base)) % len(EXTRA_MIX)])
        index += 1
    return tuple(mix)


@dataclass(frozen=True)
class Settings:
    """Immutable, validated runtime configuration.

    The scheduling fields (:attr:`posts_per_day`, the window, the intervals,
    the cooldown) are the ones the admin can override from Telegram; see
    :mod:`bot.settings_store`.  Overriding builds a *new* ``Settings`` with
    ``dataclasses.replace``, which is why :attr:`daily_mix` is derived rather
    than stored -- a new ``posts_per_day`` cannot leave a stale mix behind.
    """

    bot_token: str = field(repr=False)
    admin_user_id: int
    channel_id: str
    extra_source_chat_ids: tuple[str, ...]
    timezone: ZoneInfo
    timezone_name: str

    posts_per_day: int
    #: Window bounds as minutes after local midnight, so a window can start or
    #: end at half past the hour (and "end of day" is simply 1440).
    start_minutes: int
    end_minutes: int
    min_interval_minutes: int
    randomization_minutes: int
    #: The unpadded DAILY_MIX pattern; ``daily_mix`` pads it to posts_per_day.
    mix_base: tuple[str, ...]

    repost_cooldown_days: int
    catchup_max_late_minutes: int
    max_send_attempts: int

    import_enabled: bool
    scan_chat_id: int | None
    scan_interval_seconds: float

    database_path: Path
    log_level: str
    log_file: Path | None
    poll_timeout: int

    @property
    def window_minutes(self) -> int:
        return self.end_minutes - self.start_minutes

    @property
    def slot_minutes(self) -> float:
        return self.window_minutes / self.posts_per_day

    @property
    def daily_mix(self) -> tuple[str, ...]:
        """Exactly :attr:`posts_per_day` category names for one day."""
        return pad_mix(self.mix_base, self.posts_per_day)

    # Whole-hour views, kept because they read better in logs and because the
    # window is still configured in whole hours from .env by default.

    @property
    def start_hour(self) -> int:
        return self.start_minutes // 60

    @property
    def end_hour(self) -> int:
        return self.end_minutes // 60

    @property
    def start_clock(self) -> str:
        return format_clock(self.start_minutes)

    @property
    def end_clock(self) -> str:
        return format_clock(self.end_minutes)


def _window_bound(
    time_var: str, hour_var: str, default_hour: int, *, allow_end_of_day: bool
) -> int:
    """Minutes after local midnight for one edge of the posting window.

    ``START_TIME=08:30`` takes precedence over ``START_HOUR=8``; the whole-hour
    spelling is still honoured so an existing ``.env`` keeps working untouched.
    ``END_HOUR=24`` means midnight, exactly as it always did.
    """
    explicit = _raw(time_var)
    if explicit is not None:
        try:
            return parse_clock(explicit, allow_end_of_day=allow_end_of_day)
        except ConfigError as exc:
            raise ConfigError(f"{time_var}: {exc}") from exc

    hour = _int(hour_var, default_hour, minimum=0, maximum=24)
    if hour == MAX_WINDOW_MINUTES // 60:
        if allow_end_of_day:
            return MAX_WINDOW_MINUTES
        raise ConfigError(f"{hour_var} must be <= 23 — a window cannot start at 24:00")
    return hour * 60


def load_settings(env_file: Path | None = None) -> Settings:
    """Load ``.env`` (if present) and build a validated :class:`Settings`."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    bot_token = _str("BOT_TOKEN", required=True)
    assert bot_token is not None  # for type checkers

    admin_user_id = _int("ADMIN_USER_ID", 0)
    if admin_user_id <= 0:
        raise ConfigError(
            "ADMIN_USER_ID is required and must be a positive integer "
            "(your personal Telegram user id, get it from @userinfobot)"
        )

    channel_id = _str("CHANNEL_ID", required=True)
    assert channel_id is not None

    timezone_name = _str("TIMEZONE", "Asia/Tehran") or "Asia/Tehran"
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Unknown TIMEZONE {timezone_name!r}: {exc}") from exc

    posts_per_day = _int("POSTS_PER_DAY", 4, minimum=1, maximum=48)

    start_minutes = _window_bound("START_TIME", "START_HOUR", 8, allow_end_of_day=False)
    end_minutes = _window_bound("END_TIME", "END_HOUR", 22, allow_end_of_day=True)
    try:
        validate_window(start_minutes, end_minutes, posts_per_day)
    except ConfigError as exc:
        raise ConfigError(
            f"{exc} — check START_TIME/END_TIME and POSTS_PER_DAY in .env"
        ) from exc

    min_interval = _int("MIN_INTERVAL_MINUTES", 120, minimum=0)
    randomization = _int("RANDOMIZATION_MINUTES", 60, minimum=0)
    cooldown = _int("REPOST_COOLDOWN_DAYS", 7, minimum=0)
    catchup = _int("CATCHUP_MAX_LATE_MINUTES", 90, minimum=0, maximum=1440)
    attempts = _int("MAX_SEND_ATTEMPTS", 3, minimum=1, maximum=10)

    database_path = PROJECT_ROOT / "data" / "reposter.sqlite3"
    db_value = _str("DATABASE_PATH")
    if db_value:
        path = Path(db_value).expanduser()
        database_path = path if path.is_absolute() else PROJECT_ROOT / path

    log_file: Path | None = None
    log_value = _str("LOG_FILE", "data/bot.log")
    if log_value and log_value.lower() not in {"none", "off", "-"}:
        candidate = Path(log_value).expanduser()
        log_file = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate

    return Settings(
        bot_token=bot_token,
        admin_user_id=admin_user_id,
        channel_id=channel_id,
        extra_source_chat_ids=_csv("EXTRA_SOURCE_CHAT_IDS"),
        timezone=timezone,
        timezone_name=timezone_name,
        posts_per_day=posts_per_day,
        start_minutes=start_minutes,
        end_minutes=end_minutes,
        min_interval_minutes=min_interval,
        randomization_minutes=randomization,
        mix_base=_mix_base(_str("DAILY_MIX")),
        repost_cooldown_days=cooldown,
        catchup_max_late_minutes=catchup,
        max_send_attempts=attempts,
        import_enabled=_bool("IMPORT_ENABLED", True),
        scan_chat_id=_optional_int("SCAN_CHAT_ID"),
        scan_interval_seconds=_float("SCAN_INTERVAL_SECONDS", 0.7, minimum=0.0),
        database_path=database_path,
        log_level=(_str("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_file=log_file,
        poll_timeout=_int("POLL_TIMEOUT", 30, minimum=1, maximum=90),
    )
