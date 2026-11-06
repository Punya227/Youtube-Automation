from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from services.scheduling_service import (SchedulingError, compute_publish_slot, is_daily_run_due,
                                         next_quota_reset, next_upload_run, parse_hhmm, retry_delay)

IST = ZoneInfo("Asia/Kolkata")
BASE = {
    "upload_time": "08:00", "publish_time": "18:00", "timezone": "Asia/Kolkata", "daily_limit": 1,
    "scheduler_enabled": True, "upload_on_weekends": True, "publish_spacing_minutes": 60,
    "min_publish_lead_minutes": 60,
}


def ist(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_publish_same_day_converted_to_utc():
    # Wed 2026-01-07 08:00 IST -> publish 18:00 IST = 12:30 UTC
    slot = compute_publish_slot(ist(2026, 1, 7, 8), BASE)
    assert slot == datetime(2026, 1, 7, 12, 30, tzinfo=timezone.utc)
    assert slot.astimezone(IST).hour == 18


def test_publish_moves_to_next_day_when_time_passed():
    slot = compute_publish_slot(ist(2026, 1, 7, 19), BASE)
    assert slot.astimezone(IST) == ist(2026, 1, 8, 18)


def test_publish_respects_minimum_lead_time():
    # 17:30 IST: 18:00 is only 30 minutes away (< 60 lead) -> tomorrow
    slot = compute_publish_slot(ist(2026, 1, 7, 17, 30), BASE)
    assert slot.astimezone(IST) == ist(2026, 1, 8, 18)


def test_never_in_the_past():
    now = ist(2026, 3, 1, 23, 59)
    for hour in range(24):
        settings = {**BASE, "publish_time": f"{hour:02d}:00"}
        assert compute_publish_slot(now, settings) > now + timedelta(minutes=59)


def test_taken_slot_is_skipped():
    first = compute_publish_slot(ist(2026, 1, 7, 8), BASE)
    second = compute_publish_slot(ist(2026, 1, 7, 8), BASE, {first})
    assert second.astimezone(IST) == ist(2026, 1, 8, 18)


def test_multiple_daily_slots_are_spaced():
    settings = {**BASE, "daily_limit": 2, "publish_spacing_minutes": 90}
    first = compute_publish_slot(ist(2026, 1, 7, 8), settings)
    second = compute_publish_slot(ist(2026, 1, 7, 8), settings, {first})
    assert second - first == timedelta(minutes=90)


def test_weekend_skipped_when_disabled():
    settings = {**BASE, "upload_on_weekends": False}
    # Friday 2026-01-09 19:00 -> Saturday/Sunday skipped -> Monday 2026-01-12 18:00
    slot = compute_publish_slot(ist(2026, 1, 9, 19), settings)
    assert slot.astimezone(IST) == ist(2026, 1, 12, 18)
    run = next_upload_run(ist(2026, 1, 9, 9), settings)
    assert run == ist(2026, 1, 12, 8)


def test_next_upload_run_today_or_tomorrow():
    assert next_upload_run(ist(2026, 1, 7, 7, 59), BASE) == ist(2026, 1, 7, 8)
    assert next_upload_run(ist(2026, 1, 7, 8, 0), BASE) == ist(2026, 1, 8, 8)
    assert next_upload_run(ist(2026, 1, 7, 7), {**BASE, "scheduler_enabled": False}) is None


def test_utc_input_is_handled_in_local_timezone():
    # 2026-01-07 20:00 UTC is already 01:30 IST on Jan 8 -> today's 18:00 IST (Jan 8) is fine
    now = datetime(2026, 1, 7, 20, 0, tzinfo=timezone.utc)
    assert compute_publish_slot(now, BASE).astimezone(IST) == ist(2026, 1, 8, 18)


def test_dst_timezone_conversion():
    ny = ZoneInfo("America/New_York")
    settings = {**BASE, "timezone": "America/New_York", "publish_time": "02:30"}
    # 2026-03-08: 02:30 does not exist in New York (spring forward); slot must be a real future instant.
    now = datetime(2026, 3, 7, 23, 0, tzinfo=ny)
    slot = compute_publish_slot(now, settings)
    assert slot > now
    local = slot.astimezone(ny)
    assert local.date().isoformat() == "2026-03-08" and local.hour == 3
    # Summer vs winter offsets differ by one hour in UTC
    winter = compute_publish_slot(datetime(2026, 1, 5, 0, 0, tzinfo=ny), {**settings, "publish_time": "18:00"})
    summer = compute_publish_slot(datetime(2026, 7, 5, 0, 0, tzinfo=ny), {**settings, "publish_time": "18:00"})
    assert winter.hour == 23 and summer.hour == 22


def test_daily_run_due_logic():
    now = ist(2026, 1, 7, 10)
    assert is_daily_run_due(now, BASE, None) is True
    assert is_daily_run_due(now, BASE, "2026-01-07") is False      # already ran today
    assert is_daily_run_due(ist(2026, 1, 7, 7), BASE, None) is False  # before upload time
    saturday = ist(2026, 1, 10, 10)
    assert is_daily_run_due(saturday, {**BASE, "upload_on_weekends": False}, None) is False


def test_quota_reset_is_midnight_pacific():
    reset = next_quota_reset(datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc))
    local = reset.astimezone(ZoneInfo("America/Los_Angeles"))
    assert (local.hour, local.minute) == (0, 5) and reset > datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)


def test_retry_backoff_grows():
    assert retry_delay(1) == timedelta(minutes=5)
    assert retry_delay(2) == timedelta(minutes=15)
    assert retry_delay(3) == timedelta(minutes=45)
    assert retry_delay(10) == timedelta(hours=6)


def test_invalid_inputs():
    with pytest.raises(SchedulingError):
        parse_hhmm("25:99")
    with pytest.raises(SchedulingError):
        compute_publish_slot(ist(2026, 1, 1, 1), {**BASE, "timezone": "Mars/Base"})
