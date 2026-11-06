import sqlite3

import pytest

from database import Database, from_iso, to_iso, utc_now


def test_schema_and_default_settings(db):
    s = db.get_settings()
    assert s["upload_time"] == "08:00"
    assert s["publish_time"] == "18:00"
    assert s["timezone"] == "Asia/Kolkata"
    assert s["daily_limit"] == 1
    assert s["scheduler_enabled"] is True
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"channels", "videos", "settings", "upload_jobs", "events", "app_state"} <= tables


def test_settings_persist_across_connections(cfg, db):
    db.update_settings(upload_time="09:30", daily_limit=2, upload_on_weekends=False)
    other = Database(cfg.db_path)
    s = other.get_settings()
    assert s["upload_time"] == "09:30" and s["daily_limit"] == 2 and s["upload_on_weekends"] is False
    with pytest.raises(ValueError):
        db.update_settings(nonsense=1)


def test_unique_hash_prevents_duplicate_rows(db):
    db.insert_video(filename="a.mp4", file_path="/x/a.mp4", file_hash="abc")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_video(filename="b.mp4", file_path="/x/b.mp4", file_hash="abc")
    # A row explicitly marked duplicate may share the hash (for history display).
    db.insert_video(filename="b.mp4", file_path="/x/b.mp4", file_hash="abc", upload_status="duplicate")


def test_only_one_running_job(db):
    v1 = db.insert_video(filename="a.mp4", file_path="/a", file_hash="1")
    v2 = db.insert_video(filename="b.mp4", file_path="/b", file_hash="2")
    j1 = db.start_job(v1, "scheduled", 1, 10)
    assert j1 is not None
    assert db.start_job(v2, "scheduled", 1, 10) is None  # concurrent upload blocked
    db.finish_job(j1, "succeeded")
    assert db.start_job(v2, "scheduled", 1, 10) is not None


def test_youtube_id_unique(db):
    v1 = db.insert_video(filename="a.mp4", file_path="/a", file_hash="1")
    v2 = db.insert_video(filename="b.mp4", file_path="/b", file_hash="2")
    db.update_video(v1, youtube_video_id="XYZ")
    with pytest.raises(sqlite3.IntegrityError):
        db.update_video(v2, youtube_video_id="XYZ")


def test_publish_slot_unique_for_active_uploads(db):
    slot = to_iso(utc_now())
    v1 = db.insert_video(filename="a.mp4", file_path="/a", file_hash="1")
    v2 = db.insert_video(filename="b.mp4", file_path="/b", file_hash="2")
    db.update_video(v1, upload_status="scheduled", scheduled_publish_at=slot)
    with pytest.raises(sqlite3.IntegrityError):
        db.update_video(v2, upload_status="scheduled", scheduled_publish_at=slot)
    assert from_iso(slot) in db.taken_publish_slots()


def test_transaction_rolls_back(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.insert_video(filename="a.mp4", file_path="/a", file_hash="1")
            raise RuntimeError("boom")
    assert db.list_videos() == []


def test_tags_round_trip_and_daily_count(db):
    vid = db.insert_video(filename="a.mp4", file_path="/a", file_hash="1", tags=["x", "y"], hashtags=["#x"])
    db.update_video(vid, upload_status="scheduled", upload_day="2026-01-05")
    video = db.get_video(vid)
    assert video["tags"] == ["x", "y"] and video["hashtags"] == ["#x"]
    assert db.count_uploads_on_day("2026-01-05") == 1
    assert db.count_uploads_on_day("2026-01-06") == 0


def test_events_redact_secrets(db):
    db.log_event("auth", "token ya29.a0AfB_secretvalue leaked? refresh_token=1//0abcdefghijklmnopqrstuvwxyz")
    message = db.list_events(limit=1)[0]["message"]
    assert "ya29" not in message and "1//0abc" not in message
    assert "[REDACTED]" in message


def test_state_round_trip(db):
    db.set_state("k", 5)
    assert db.get_state("k") == "5"
    db.set_state("k", None)
    assert db.get_state("k", "default") == "default"
