"""End-to-end upload, limit, retry and crash-recovery tests (mock uploader only - no YouTube)."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import needs_ffmpeg
from database import from_iso, to_iso, utc_now
from youtube_uploader import SimulatedCrash, UploadRequest

pytestmark = needs_ffmpeg


def queue_videos(stack, video_factory, count, start_variant=0):
    for i in range(count):
        stack.add_video(video_factory(variant=start_variant + i), f"clip_{start_variant + i}.mp4")
    return stack.scan()


def test_successful_upload_schedules_private_with_future_publish_at(stack, video_factory):
    (vid,) = queue_videos(stack, video_factory, 1)
    outcome = stack.uploads.upload_video(vid, trigger="manual")
    assert outcome.status == "scheduled", outcome.message
    video = stack.db.get_video(vid)
    assert video["youtube_video_id"].startswith("MOCK")
    assert video["youtube_url"].endswith(video["youtube_video_id"])
    assert from_iso(video["scheduled_publish_at"]) > utc_now() + timedelta(minutes=59)
    assert Path(video["file_path"]).parent == stack.cfg.uploaded_dir
    remote = stack.uploader.all_videos()[video["youtube_video_id"]]
    assert remote["status"]["privacyStatus"] == "private"
    assert remote["status"]["publishAt"].endswith("Z")
    assert "#Shorts" in remote["snippet"]["description"]


def test_daily_limit_enforced_even_after_restart(stack, make_stack, video_factory):
    queue_videos(stack, video_factory, 3)
    outcomes = stack.uploads.run_daily_batch("scheduled")
    assert [o.status for o in outcomes] == ["scheduled"]
    assert len(stack.db.list_videos(["queued"])) == 2
    restarted = make_stack()  # new process, same database
    assert restarted.uploads.run_daily_batch("catch_up") == []
    assert len(restarted.uploader.all_videos()) == 1
    # Explicit override from the dashboard is allowed
    vid = restarted.db.list_videos(["queued"])[0]["id"]
    assert restarted.uploads.upload_video(vid, enforce_limit=True).status == "limit"
    assert restarted.uploads.upload_video(vid, enforce_limit=False).status == "scheduled"


def test_daily_limit_two_uses_two_distinct_slots(stack, video_factory):
    stack.db.update_settings(daily_limit=2)
    queue_videos(stack, video_factory, 3)
    outcomes = stack.uploads.run_daily_batch("scheduled")
    assert [o.status for o in outcomes] == ["scheduled", "scheduled"]
    slots = [v["scheduled_publish_at"] for v in stack.db.list_videos(["scheduled"])]
    assert len(set(slots)) == 2


def test_no_eligible_videos_logs_and_waits(stack):
    assert stack.uploads.run_daily_batch("scheduled") == []
    assert any("No eligible videos" in e["message"] for e in stack.db.list_events())


def test_already_uploaded_video_is_never_reuploaded(stack, video_factory):
    (vid,) = queue_videos(stack, video_factory, 1)
    stack.uploads.upload_video(vid)
    stack.db.update_video(vid, upload_status="queued")  # even if status were corrupted
    assert stack.uploads.upload_video(vid, enforce_limit=False).status == "skipped"
    assert len(stack.uploader.all_videos()) == 1


def test_concurrent_upload_prevented(stack, video_factory):
    a, b = queue_videos(stack, video_factory, 2)
    job = stack.db.start_job(a, "manual", 1, 1)
    assert stack.uploads.upload_video(b, enforce_limit=False).status == "busy"
    stack.db.finish_job(job, "failed")


def test_transient_failure_retries_with_backoff(make_stack, video_factory):
    stack = make_stack("transient_once")
    stack.db.update_settings(upload_time="00:00")
    (vid,) = queue_videos(stack, video_factory, 1)
    assert stack.uploads.upload_video(vid).status == "retry_wait"
    video = stack.db.get_video(vid)
    assert video["retry_count"] == 1 and from_iso(video["next_retry_at"]) > utc_now()
    assert Path(video["file_path"]).parent == stack.cfg.incoming_dir
    assert stack.uploads.process_due_retries() == []  # not due yet
    stack.db.update_video(vid, next_retry_at=to_iso(utc_now() - timedelta(seconds=1)))
    outcomes = stack.uploads.process_due_retries()
    assert [o.status for o in outcomes] == ["scheduled"]


def test_retries_are_bounded(make_stack, video_factory):
    stack = make_stack("transient_always")
    stack.db.update_settings(max_retries=2, upload_time="00:00")
    (vid,) = queue_videos(stack, video_factory, 1)
    statuses = []
    for _ in range(4):
        stack.db.update_video(vid, next_retry_at=to_iso(utc_now() - timedelta(seconds=1)))
        statuses.append(stack.uploads.upload_video(vid, enforce_limit=False).status)
    assert statuses[:3] == ["retry_wait", "retry_wait", "failed"]
    assert statuses[3] == "skipped"  # failed videos are not retried automatically
    assert Path(stack.db.get_video(vid)["file_path"]).parent == stack.cfg.failed_dir


def test_permanent_error_not_retried(make_stack, video_factory):
    stack = make_stack("permanent")
    (vid,) = queue_videos(stack, video_factory, 1)
    assert stack.uploads.upload_video(vid).status == "failed"
    video = stack.db.get_video(vid)
    assert video["retry_count"] == 0 and Path(video["file_path"]).exists()
    assert Path(video["file_path"]).parent == stack.cfg.failed_dir


def test_auth_error_requeues_without_burning_retries(make_stack, video_factory):
    stack = make_stack("auth")
    queue_videos(stack, video_factory, 2)
    outcomes = stack.uploads.run_daily_batch("scheduled")
    assert [o.status for o in outcomes] == ["auth_error"]   # batch stops at first auth error
    assert len(stack.db.list_videos(["queued"])) == 2
    assert stack.db.get_state("auth_error")


def test_quota_error_waits_for_pacific_midnight(make_stack, video_factory):
    stack = make_stack("quota")
    (vid,) = queue_videos(stack, video_factory, 1)
    assert stack.uploads.upload_video(vid).status == "quota"
    video = stack.db.get_video(vid)
    assert video["upload_status"] == "retry_wait" and video["retry_count"] == 0
    assert from_iso(video["next_retry_at"]) - utc_now() <= timedelta(hours=25)


def test_unconfirmed_schedule_is_repaired(make_stack, video_factory):
    stack = make_stack("drop_publish_at")
    (vid,) = queue_videos(stack, video_factory, 1)
    assert stack.uploads.upload_video(vid).status == "scheduled"
    video = stack.db.get_video(vid)
    remote = stack.uploader.all_videos()[video["youtube_video_id"]]
    assert remote["status"]["publishAt"]


def test_crash_after_upload_is_reconciled_not_reuploaded(make_stack, video_factory):
    crashing = make_stack("crash_after_upload")
    (vid,) = queue_videos(crashing, video_factory, 1)
    with pytest.raises(SimulatedCrash):
        crashing.uploads.upload_video(vid)
    # Simulated process death: SQLite still says "uploading", YouTube already has the video.
    assert crashing.db.get_video(vid)["upload_status"] == "uploading"
    assert len(crashing.uploader.all_videos()) == 1

    restarted = make_stack()
    report = restarted.recovery.run(include_running=True)
    assert report.reconciled == [vid]
    video = restarted.db.get_video(vid)
    assert video["upload_status"] == "scheduled"
    assert video["youtube_video_id"] in restarted.uploader.all_videos()
    assert len(restarted.uploader.all_videos()) == 1           # no duplicate upload
    assert Path(video["file_path"]).parent == restarted.cfg.uploaded_dir
    assert restarted.db.open_jobs() == []
    assert restarted.uploads.run_daily_batch("scheduled") == []  # limit already used today


def test_interrupted_upload_resumes_same_session(make_stack, video_factory):
    crashing = make_stack("interrupt_midway")
    (vid,) = queue_videos(crashing, video_factory, 1)
    with pytest.raises(SimulatedCrash):
        crashing.uploads.upload_video(vid)
    job = crashing.db.open_jobs()[0]
    assert job["resumable_uri"] and 0 < job["bytes_uploaded"] < job["total_bytes"]

    restarted = make_stack()
    report = restarted.recovery.run(include_running=True)
    assert report.resumed == [vid]
    assert restarted.db.get_video(vid)["upload_status"] == "scheduled"
    store = json.loads(restarted.cfg.mock_store_path.read_text())
    assert len(store["videos"]) == 1 and len(store["sessions"]) == 1  # same session continued


def test_expired_session_falls_back_to_channel_search(make_stack, video_factory):
    crashing = make_stack("crash_after_upload")
    (vid,) = queue_videos(crashing, video_factory, 1)
    with pytest.raises(SimulatedCrash):
        crashing.uploads.upload_video(vid)
    store = json.loads(crashing.cfg.mock_store_path.read_text())
    store["sessions"] = {}  # YouTube forgot the session
    crashing.cfg.mock_store_path.write_text(json.dumps(store))

    restarted = make_stack()
    assert restarted.recovery.run(include_running=True).reconciled == [vid]
    assert len(restarted.uploader.all_videos()) == 1


def test_crash_before_upload_started_requeues(stack, video_factory):
    (vid,) = queue_videos(stack, video_factory, 1)
    # Simulate: job opened, slot reserved, file moved to processing, then power loss before any bytes.
    job = stack.db.start_job(vid, "scheduled", 1, 100)
    video = stack.db.get_video(vid)
    stack.db.update_video(vid, upload_status="uploading", scheduled_publish_at=to_iso(utc_now() + timedelta(hours=5)))
    stack.db.update_job(job, title_snapshot=video["title"])
    stack.queue.relocate(stack.db.get_video(vid), "uploading")

    report = stack.recovery.run(include_running=True)
    assert report.requeued == [vid]
    video = stack.db.get_video(vid)
    assert video["upload_status"] == "queued" and video["scheduled_publish_at"] is None
    assert Path(video["file_path"]).parent == stack.cfg.incoming_dir
    assert stack.uploads.upload_video(vid).status == "scheduled"
    assert len(stack.uploader.all_videos()) == 1


def test_stray_processing_file_restored(stack, video_factory):
    stray = stack.cfg.processing_dir / "left_behind.mp4"
    stray.write_bytes(video_factory(variant=9).read_bytes())
    report = stack.recovery.run(include_running=True)
    assert "left_behind.mp4" in report.files_restored
    assert (stack.cfg.incoming_dir / "left_behind.mp4").exists()


def test_sync_marks_published_after_publish_time(stack, video_factory):
    (vid,) = queue_videos(stack, video_factory, 1)
    stack.uploads.upload_video(vid)
    video = stack.db.get_video(vid)
    past = utc_now() - timedelta(minutes=1)
    stack.db.update_video(vid, scheduled_publish_at=to_iso(past))
    stack.uploader.set_publish_at(video["youtube_video_id"], past)
    assert stack.uploads.sync_published() == 1
    assert stack.db.get_video(vid)["upload_status"] == "published"


def test_upload_request_body_uses_official_scheduling_fields(tmp_path):
    body = UploadRequest(file_path=tmp_path / "a.mp4", title="T", description="D", tags=["a"], hashtags=["#Shorts"],
                         category_id="22", language="en", publish_at=utc_now() + timedelta(days=1),
                         contains_synthetic_media=True).body()
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"].endswith(".000Z")
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["snippet"]["description"].endswith("#Shorts")
