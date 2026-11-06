"""Pure time calculations for upload runs and publication slots.

All functions take ``now`` explicitly (an aware datetime) so they are easy to test.
Times are stored and sent to YouTube in UTC; the user's settings are in their
local timezone (default Asia/Kolkata).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SchedulingError(ValueError):
    pass


def parse_hhmm(value: str) -> time:
    try:
        hours, minutes = value.strip().split(":")[:2]
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError) as exc:
        raise SchedulingError(f"Invalid time '{value}', expected HH:MM") from exc


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SchedulingError(f"Unknown timezone '{name}'") from exc


def local_datetime(day: date, at: time, tz: ZoneInfo) -> datetime:
    """Combine a local date and wall-clock time, normalising DST gaps."""
    naive = datetime.combine(day, at)
    aware = naive.replace(tzinfo=tz)
    # Round-trip through UTC so a non-existent wall time (DST gap) maps to a real instant.
    return aware.astimezone(timezone.utc).astimezone(tz)


def is_upload_day(day: date, settings: dict) -> bool:
    return bool(settings.get("upload_on_weekends", True)) or day.weekday() < 5


def local_today(now: datetime, settings: dict) -> date:
    return now.astimezone(get_zone(settings["timezone"])).date()


def todays_upload_time(now: datetime, settings: dict) -> datetime:
    tz = get_zone(settings["timezone"])
    return local_datetime(now.astimezone(tz).date(), parse_hhmm(settings["upload_time"]), tz)


def next_upload_run(now: datetime, settings: dict) -> datetime | None:
    """Next scheduled daily run strictly after ``now`` (local tz-aware), or None if disabled."""
    if not settings.get("scheduler_enabled", True):
        return None
    tz = get_zone(settings["timezone"])
    at = parse_hhmm(settings["upload_time"])
    day = now.astimezone(tz).date()
    for offset in range(0, 15):
        candidate_day = day + timedelta(days=offset)
        if not is_upload_day(candidate_day, settings):
            continue
        candidate = local_datetime(candidate_day, at, tz)
        if candidate > now:
            return candidate
    return None


def is_daily_run_due(now: datetime, settings: dict, last_run_day: str | None) -> bool:
    """True if today's run time has passed and today's run has not happened yet.

    Used for catching up runs missed while the PC was off/asleep or the worker
    was stopped. Only today's run is caught up; older missed days are skipped.
    """
    if not settings.get("scheduler_enabled", True):
        return False
    today = local_today(now, settings)
    if not is_upload_day(today, settings):
        return False
    if last_run_day == today.isoformat():
        return False
    return now >= todays_upload_time(now, settings)


def compute_publish_slot(now: datetime, settings: dict, taken: set[datetime] | None = None,
                         horizon_days: int = 400) -> datetime:
    """First free publication instant (UTC) that is safely in the future.

    Each eligible day offers ``daily_limit`` slots starting at ``publish_time`` and
    spaced ``publish_spacing_minutes`` apart. A slot must be at least
    ``min_publish_lead_minutes`` after ``now``; if today's slot has passed, the
    next eligible day is used. Weekend days are skipped when weekend uploads are off.
    """
    tz = get_zone(settings["timezone"])
    publish_at = parse_hhmm(settings["publish_time"])
    daily_limit = max(1, int(settings.get("daily_limit", 1)))
    spacing = timedelta(minutes=max(1, int(settings.get("publish_spacing_minutes", 60))))
    lead = timedelta(minutes=max(15, int(settings.get("min_publish_lead_minutes", 60))))
    taken_utc = {t.astimezone(timezone.utc).replace(microsecond=0) for t in (taken or set())}
    earliest = now.astimezone(timezone.utc) + lead

    start_day = now.astimezone(tz).date()
    for offset in range(horizon_days):
        day = start_day + timedelta(days=offset)
        if not is_upload_day(day, settings):
            continue
        first = local_datetime(day, publish_at, tz)
        for index in range(daily_limit):
            slot = (first + spacing * index).astimezone(timezone.utc).replace(second=0, microsecond=0)
            if slot < earliest or slot in taken_utc:
                continue
            return slot
    raise SchedulingError("No free publication slot found")


def next_quota_reset(now: datetime) -> datetime:
    """YouTube API quotas reset at midnight Pacific Time."""
    pacific = ZoneInfo("America/Los_Angeles")
    local = now.astimezone(pacific)
    midnight = datetime.combine(local.date() + timedelta(days=1), time(0, 5), tzinfo=pacific)
    return midnight.astimezone(timezone.utc)


def retry_delay(retry_count: int, base_minutes: int = 5) -> timedelta:
    """Backoff between whole-upload retries: 5, 15, 45 ... minutes (capped at 6 h)."""
    return timedelta(minutes=min(360, base_minutes * (3 ** max(0, retry_count - 1))))


def format_local(dt: datetime | None, tz_name: str, fmt: str = "%a %d %b %Y, %I:%M %p") -> str:
    if dt is None:
        return "-"
    return dt.astimezone(get_zone(tz_name)).strftime(fmt)
