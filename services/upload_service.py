"""Upload orchestration: validation -> metadata -> slot -> resumable upload -> verify -> persist.

Safety rules implemented here:
* Only one upload runs at a time (``upload_jobs`` unique ``running`` row).
* A video that already has a ``youtube_video_id`` is never uploaded again.
* The YouTube video id is written to SQLite immediately after YouTube accepts
  the file, before any further API calls.
* The publication slot is always computed fresh and is always in the future.
* Daily upload limits are enforced from the database, so restarts can't bypass them.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config import AppConfig
from database import Database, from_iso, to_iso, utc_now
from queue_manager import QueueManager
from services.metadata_service import MetadataService
from services.scheduling_service import (compute_publish_slot, format_local, is_upload_day, local_today,
                                         next_quota_reset, retry_delay, todays_upload_time)
from video_processor import file_sha256, validate_video
from youtube_uploader import (AuthUploadError, BaseUploader, PermanentUploadError, QuotaExceededError,
                              TransientUploadError, UploadError, UploadInterrupted, UploadRequest, UploadResult)

log = logging.getLogger("ysa.upload")

WAITING_STATUSES = ("queued", "pending_approval", "retry_wait")


@dataclass
class UploadOutcome:
    video_id: int | None
    status: str          # scheduled | needs_review | retry_wait | failed | invalid | auth_error | quota | busy | limit | skipped | interrupted
    message: str = ""
    youtube_video_id: str | None = None


class UploadService:
    def __init__(self, cfg: AppConfig, db: Database, uploader: BaseUploader, queue: QueueManager,
                 metadata: MetadataService, stop_event: threading.Event | None = None, clock=utc_now):
        self.cfg = cfg
        self.db = db
        self.uploader = uploader
        self.queue = queue
        self.metadata = metadata
        self.stop_event = stop_event or threading.Event()
        self.clock = clock

    # --- limits ----------------------------------------------------------------------
    def uploads_today(self, now: datetime | None = None) -> int:
        now = now or self.clock()
        return self.db.count_uploads_on_day(local_today(now, self.db.get_settings()).isoformat())

    def limit_reached(self, now: datetime | None = None) -> bool:
        return self.uploads_today(now) >= int(self.db.get_settings()["daily_limit"])

    # --- batch entry points (called by the worker) -------------------------------------
    def run_daily_batch(self, trigger: str = "scheduled") -> list[UploadOutcome]:
        now = self.clock()
        settings = self.db.get_settings()
        today = local_today(now, settings)
        self.db.set_state("last_daily_run_day", today.isoformat())
        self.db.set_state("last_daily_run_at", to_iso(now))
        if not is_upload_day(today, settings):
            self.db.log_event("schedule", f"{today:%A} is not an upload day (weekend uploads disabled)")
            return []
        outcomes: list[UploadOutcome] = []
        attempted: set[int] = set()
        while not self.stop_event.is_set():
            done = self.uploads_today()
            if done >= settings["daily_limit"]:
                self.db.log_event("schedule", f"Daily limit reached ({done}/{settings['daily_limit']}); "
                                              "no more uploads today")
                break
            candidates = [v for v in self.queue.eligible_videos(self.clock()) if v["id"] not in attempted]
            if not candidates:
                if not outcomes:
                    self.db.log_event("schedule", "No eligible videos in the queue; waiting for the next run",
                                      "WARNING")
                break
            video = candidates[0]
            attempted.add(video["id"])
            outcome = self.upload_video(video["id"], trigger=trigger)
            outcomes.append(outcome)
            if outcome.status in ("auth_error", "quota", "busy", "interrupted"):
                break
        return outcomes

    def process_manual_requests(self) -> list[UploadOutcome]:
        outcomes = []
        for video in self.db.list_videos(WAITING_STATUSES):
            if self.stop_event.is_set() or not video["manual_request"]:
                continue
            override = video["manual_request"] == 2
            outcome = self.upload_video(video["id"], trigger="manual", enforce_limit=not override)
            if outcome.status in ("limit", "skipped"):
                self.db.update_video(video["id"], manual_request=0)
            outcomes.append(outcome)
            if outcome.status in ("auth_error", "quota", "busy", "interrupted"):
                break
        return outcomes

    def process_due_retries(self) -> list[UploadOutcome]:
        now = self.clock()
        settings = self.db.get_settings()
        if not settings["scheduler_enabled"] or now < todays_upload_time(now, settings):
            return []
        outcomes = []
        for video in self.db.list_videos(["retry_wait"]):
            if self.stop_event.is_set() or self.limit_reached():
                break
            due = from_iso(video["next_retry_at"])
            if due and due > now:
                continue
            outcome = self.upload_video(video["id"], trigger="retry")
            outcomes.append(outcome)
            if outcome.status in ("auth_error", "quota", "busy", "interrupted"):
                break
        return outcomes

    # --- single upload -------------------------------------------------------------------
    def upload_video(self, video_id: int, trigger: str = "manual", enforce_limit: bool = True) -> UploadOutcome:
        settings = self.db.get_settings()
        now = self.clock()
        video = self.db.get_video(video_id)
        if video is None:
            return UploadOutcome(video_id, "skipped", "Video not found")
        if video["youtube_video_id"]:
            return UploadOutcome(video_id, "skipped", "Already on YouTube", video["youtube_video_id"])
        if video["upload_status"] not in WAITING_STATUSES:
            return UploadOutcome(video_id, "skipped", f"Status is {video['upload_status']}")
        if video["upload_status"] == "pending_approval" and settings["manual_approval"]:
            return UploadOutcome(video_id, "skipped", "Waiting for metadata approval")
        if enforce_limit and self.uploads_today(now) >= settings["daily_limit"]:
            msg = f"Daily limit of {settings['daily_limit']} reached; {video['filename']} not uploaded"
            self.db.log_event("schedule", msg, "WARNING", video_id)
            return UploadOutcome(video_id, "limit", msg)

        path = Path(video["file_path"])
        if not path.exists():
            self.db.update_video(video_id, upload_status="failed", error_message=f"File missing: {path}",
                                 manual_request=0)
            self.db.log_event("upload", f"File for {video['filename']} is missing", "ERROR", video_id)
            return UploadOutcome(video_id, "failed", "File missing")

        job_id = self.db.start_job(video_id, trigger, os.getpid(), path.stat().st_size)
        if job_id is None:
            return UploadOutcome(video_id, "busy", "Another upload is already running")

        try:
            # Re-check the file: it may have been replaced since it was queued.
            size = path.stat().st_size
            if size != video["file_size"]:
                digest = file_sha256(path)
                other = self.db.find_video_by_hash(digest)
                if other and other["id"] != video_id:
                    raise PermanentUploadError(f"File content now duplicates video #{other['id']}")
            result = validate_video(path, self.cfg.shorts_max_duration, self.cfg.allowed_extensions,
                                    self.cfg.ffprobe_path, self.cfg.ffmpeg_path)
            if not result.ok:
                self.db.update_video(video_id, upload_status="invalid", error_message="; ".join(result.errors),
                                     manual_request=0)
                self.queue.relocate(self.db.get_video(video_id), "invalid")
                self.db.finish_job(job_id, "failed", "; ".join(result.errors))
                self.db.log_event("upload", f"{video['filename']} failed validation: {result.summary}",
                                  "ERROR", video_id)
                return UploadOutcome(video_id, "invalid", result.summary)

            video = self.metadata.refresh_override_before_upload(video_id)
            slot = self._reserve_slot(video_id, now, settings)
            video = self.db.get_video(video_id)
            self.db.update_job(job_id, title_snapshot=video["title"], publish_at_snapshot=to_iso(slot))
            path = self.queue.relocate(video, "uploading")
            self.db.log_event("upload", f"Uploading {video['filename']} as '{video['title']}' "
                                        f"(publish {format_local(slot, settings['timezone'])})", "INFO", video_id)
        except UploadError as exc:
            return self._handle_failure(video_id, job_id, exc)
        except Exception as exc:
            log.exception("Preparing upload failed")
            return self._handle_failure(video_id, job_id, TransientUploadError(f"Preparation failed: {exc}"))

        return self._run_upload(video, job_id, slot, path, settings)

    def resume_upload(self, video_id: int, resumable_uri: str, offset: int) -> UploadOutcome:
        """Continue an interrupted resumable session (used by recovery)."""
        settings = self.db.get_settings()
        video = self.db.get_video(video_id)
        path = Path(video["file_path"])
        slot = from_iso(video["scheduled_publish_at"])
        job_id = self.db.start_job(video_id, "recovery", os.getpid(), path.stat().st_size)
        if job_id is None:
            return UploadOutcome(video_id, "busy", "Another upload is already running")
        self.db.update_job(job_id, resumable_uri=resumable_uri, bytes_uploaded=offset,
                           title_snapshot=video["title"], publish_at_snapshot=to_iso(slot))
        self.db.log_event("recovery", f"Resuming interrupted upload of {video['filename']} at byte {offset:,}",
                          "INFO", video_id)
        return self._run_upload(video, job_id, slot, path, settings, resume=(resumable_uri, offset))

    # --- internals ------------------------------------------------------------------------
    def _reserve_slot(self, video_id: int, now: datetime, settings: dict) -> datetime:
        with self.db.transaction():
            slot = compute_publish_slot(now, settings, self.db.taken_publish_slots())
            self.db.update_video(video_id, upload_status="uploading", scheduled_publish_at=to_iso(slot),
                                 upload_day=local_today(now, settings).isoformat(), error_message=None)
        return slot

    def build_request(self, video: dict, path: Path, slot: datetime, settings: dict) -> UploadRequest:
        return UploadRequest(
            file_path=path,
            title=video["title"],
            description=video["description"] or "",
            tags=video["tags"],
            hashtags=video["hashtags"],
            category_id=video["category_id"] or settings["default_category_id"],
            language=video["language"] or settings["default_language"],
            publish_at=slot,
            contains_synthetic_media=bool(video["contains_synthetic_media"]),
            notify_subscribers=bool(settings["notify_subscribers"]),
        )

    def _run_upload(self, video: dict, job_id: int, slot: datetime, path: Path, settings: dict,
                    resume: tuple[str, int] | None = None) -> UploadOutcome:
        video_id = video["id"]
        request = self.build_request(video, path, slot, settings)

        def on_session(uri: str, offset: int) -> None:
            self.db.update_job(job_id, resumable_uri=uri, bytes_uploaded=offset)

        try:
            result = self.uploader.upload(request, on_session=on_session, stop_event=self.stop_event, resume=resume)
        except UploadInterrupted as exc:
            self.db.update_job(job_id, job_status="interrupted", error_message=str(exc))
            self.db.log_event("upload", f"Upload of {video['filename']} paused for shutdown; it will resume "
                                        "on next start", "WARNING", video_id)
            return UploadOutcome(video_id, "interrupted", str(exc))
        except UploadError as exc:
            return self._handle_failure(video_id, job_id, exc)
        except Exception as exc:
            job = self.db.get_job(job_id)
            if job and job["resumable_uri"]:
                # We can't tell whether YouTube completed the upload: leave it to recovery.
                self.db.update_job(job_id, job_status="interrupted", error_message=f"Unexpected: {exc}")
                self.db.log_event("upload", f"Unexpected error during upload of {video['filename']}: {exc}. "
                                            "Recovery will reconcile with YouTube.", "ERROR", video_id)
                return UploadOutcome(video_id, "interrupted", str(exc))
            log.exception("Upload failed before a session was created")
            return self._handle_failure(video_id, job_id, TransientUploadError(f"Unexpected error: {exc}"))
        return self.finalize_success(video_id, job_id, result, slot)

    def finalize_success(self, video_id: int, job_id: int | None, result: UploadResult, slot: datetime,
                         via: str = "upload") -> UploadOutcome:
        settings = self.db.get_settings()
        # 1) Persist the YouTube id immediately so a crash can never cause a re-upload.
        with self.db.transaction():
            self.db.update_video(video_id, youtube_video_id=result.video_id, youtube_url=result.url,
                                 upload_status="needs_review", uploaded_at=to_iso(self.clock()),
                                 error_message="Verifying scheduled publication...", manual_request=0,
                                 next_retry_at=None)
            if job_id is not None:
                self.db.finish_job(job_id, "succeeded" if via == "upload" else "reconciled")
        self.db.set_state("auth_error", None)

        # 2) Verify YouTube accepted privacy=private + publishAt; repair once if needed.
        confirmed = result.schedule_confirmed(slot)
        note = None
        if not confirmed:
            try:
                fresh = self.uploader.get_videos([result.video_id]).get(result.video_id)
                if fresh is not None and fresh.schedule_confirmed(slot):
                    confirmed = True
                elif fresh is not None and fresh.privacy_status == "public":
                    note = "Video is already public on YouTube"
                elif slot > self.clock():
                    repaired = self.uploader.set_publish_at(result.video_id, slot)
                    confirmed = repaired.schedule_confirmed(slot)
            except UploadError as exc:
                note = f"Could not verify schedule: {exc}"
        status = "scheduled" if confirmed else ("published" if note == "Video is already public on YouTube"
                                                 else "needs_review")
        self.db.update_video(
            video_id, upload_status=status,
            error_message=None if confirmed else (note or "YouTube did not confirm the scheduled publish time; "
                                                          "check the video in YouTube Studio"),
            published_at=to_iso(self.clock()) if status == "published" else None,
        )
        video = self.db.get_video(video_id)
        self.queue.relocate(video, status)
        when = format_local(slot, settings["timezone"])
        if confirmed:
            self.db.log_event("upload", f"Scheduled '{video['title']}' for {when} ({result.url})", "INFO", video_id,
                              {"youtube_video_id": result.video_id, "via": via})
        else:
            self.db.log_event("upload", f"Uploaded '{video['title']}' ({result.url}) but scheduling needs review: "
                                        f"{video['error_message']}", "WARNING", video_id)
        return UploadOutcome(video_id, status, f"Publishes {when}", result.video_id)

    def _handle_failure(self, video_id: int, job_id: int, error: UploadError) -> UploadOutcome:
        settings = self.db.get_settings()
        now = self.clock()
        video = self.db.get_video(video_id)
        message = str(error)
        self.db.finish_job(job_id, "failed", message)
        reset = dict(scheduled_publish_at=None, upload_day=None, manual_request=0)

        if isinstance(error, QuotaExceededError):
            retry_at = next_quota_reset(now)
            self.db.update_video(video_id, upload_status="retry_wait", next_retry_at=to_iso(retry_at),
                                 error_message=message, **reset)
            self.queue.relocate(self.db.get_video(video_id), "retry_wait")
            self.db.log_event("upload", f"YouTube quota/upload limit reached; {video['filename']} will retry after "
                                        f"{format_local(retry_at, settings['timezone'])}", "WARNING", video_id)
            return UploadOutcome(video_id, "quota", message)

        if isinstance(error, AuthUploadError):
            self.db.update_video(video_id, upload_status="queued", error_message=message, **reset)
            self.queue.relocate(self.db.get_video(video_id), "queued")
            self.db.set_state("auth_error", message)
            self.db.log_event("auth", f"Authorization problem: {message}. Reconnect YouTube in the dashboard.",
                              "ERROR", video_id)
            return UploadOutcome(video_id, "auth_error", message)

        if isinstance(error, PermanentUploadError):
            self.db.update_video(video_id, upload_status="failed", error_message=message, **reset)
            self.queue.relocate(self.db.get_video(video_id), "failed")
            self.db.log_event("upload", f"Upload of {video['filename']} failed permanently: {message}",
                              "ERROR", video_id)
            return UploadOutcome(video_id, "failed", message)

        retries = int(video["retry_count"] or 0) + 1
        if retries > int(settings["max_retries"]):
            self.db.update_video(video_id, upload_status="failed", retry_count=retries,
                                 error_message=f"Gave up after {retries - 1} retries: {message}", **reset)
            self.queue.relocate(self.db.get_video(video_id), "failed")
            self.db.log_event("retry", f"Giving up on {video['filename']} after {retries - 1} retries: {message}",
                              "ERROR", video_id)
            return UploadOutcome(video_id, "failed", message)
        retry_at = now + retry_delay(retries)
        self.db.update_video(video_id, upload_status="retry_wait", retry_count=retries,
                             next_retry_at=to_iso(retry_at), error_message=message, **reset)
        self.queue.relocate(self.db.get_video(video_id), "retry_wait")
        self.db.log_event("retry", f"Temporary failure for {video['filename']} ({message}); retry {retries}/"
                                   f"{settings['max_retries']} at {format_local(retry_at, settings['timezone'])}",
                          "WARNING", video_id)
        return UploadOutcome(video_id, "retry_wait", message)

    # --- post-upload sync ------------------------------------------------------------------
    def sync_published(self) -> int:
        """Mark scheduled videos as published once YouTube reports them public."""
        now = self.clock()
        due = [v for v in self.db.list_videos(["scheduled"])
               if v["youtube_video_id"] and from_iso(v["scheduled_publish_at"]) and
               from_iso(v["scheduled_publish_at"]) <= now]
        if not due:
            return 0
        try:
            remote = self.uploader.get_videos([v["youtube_video_id"] for v in due])
        except UploadError as exc:
            log.warning("Could not sync publication status: %s", exc)
            return 0
        changed = 0
        for video in due:
            info = remote.get(video["youtube_video_id"])
            if info is None:
                self.db.update_video(video["id"], upload_status="needs_review",
                                     error_message="Video no longer found on YouTube (deleted or not visible)")
                self.db.log_event("upload", f"'{video['title']}' was not found on YouTube", "WARNING", video["id"])
            elif info.privacy_status == "public":
                self.db.update_video(video["id"], upload_status="published", published_at=video["scheduled_publish_at"])
                self.db.log_event("schedule", f"'{video['title']}' is now public", "INFO", video["id"])
                changed += 1
        return changed
