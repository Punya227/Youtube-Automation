"""Restart recovery: reconcile interrupted uploads with YouTube instead of re-uploading blindly.

For every upload job left ``running``/``interrupted`` by a crash, power cut or
shutdown, and every video stuck in ``uploading``:

1. If SQLite already has a YouTube id -> just close the job and move the file.
2. If a resumable session URI was saved -> ask YouTube for the session state:
   * complete   -> YouTube created the video: record its id (no re-upload).
   * incomplete -> resume the same session from the confirmed byte offset
                   (only if the publish slot is still safely in the future).
   * expired    -> continue with step 3.
3. Search the channel's most recent uploads for the exact title uploaded after the
   job started. Found -> record it. Not found -> it is safe to re-queue.
4. If YouTube can't be asked (offline, missing scope), mark ``needs_review``
   so a human decides; never upload a possible duplicate automatically.

Files left in ``processing/`` without an active upload are moved back to where
their database status says they belong.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from database import Database, from_iso, utc_now
from queue_manager import QueueManager, move_file
from services.upload_service import UploadService
from youtube_uploader import BaseUploader, UploadError

log = logging.getLogger("ysa.recovery")


@dataclass
class RecoveryReport:
    reconciled: list[int] = field(default_factory=list)
    resumed: list[int] = field(default_factory=list)
    requeued: list[int] = field(default_factory=list)
    needs_review: list[int] = field(default_factory=list)
    files_restored: list[str] = field(default_factory=list)

    @property
    def anything(self) -> bool:
        return any((self.reconciled, self.resumed, self.requeued, self.needs_review, self.files_restored))


class RecoveryManager:
    def __init__(self, db: Database, uploader: BaseUploader, queue: QueueManager, upload_service: UploadService):
        self.db = db
        self.uploader = uploader
        self.queue = queue
        self.uploads = upload_service

    def run(self, include_running: bool = True) -> RecoveryReport:
        """``include_running`` must only be True when no upload can be in progress (worker startup)."""
        report = RecoveryReport()
        statuses = ("running", "interrupted") if include_running else ("interrupted",)
        handled_videos: set[int] = set()
        for job in self.db.open_jobs():
            if job["job_status"] not in statuses:
                continue
            handled_videos.add(job["video_id"])
            try:
                self._recover_job(job, report)
            except Exception as exc:  # keep going with other jobs
                log.exception("Recovery of job %s failed", job["id"])
                self.db.log_event("recovery", f"Recovery of job #{job['id']} failed: {exc}", "ERROR", job["video_id"])

        if include_running:
            for video in self.db.list_videos(["uploading"]):
                if video["id"] in handled_videos:
                    continue
                self._recover_orphan_video(video, report)
            self._restore_stray_files(report)

        if report.anything:
            self.db.log_event("recovery", "Startup recovery finished", "INFO", details={
                "reconciled": report.reconciled, "resumed": report.resumed, "requeued": report.requeued,
                "needs_review": report.needs_review, "files_restored": report.files_restored})
        return report

    # --- jobs ---------------------------------------------------------------------------
    def _recover_job(self, job: dict, report: RecoveryReport) -> None:
        video = self.db.get_video(job["video_id"])
        if video is None:
            self.db.finish_job(job["id"], "failed", "Video row missing")
            return

        if video["youtube_video_id"]:
            self.db.finish_job(job["id"], "reconciled", "Upload had already been recorded")
            if video["upload_status"] == "uploading":
                self.db.update_video(video["id"], upload_status="needs_review",
                                     error_message="Recovered after restart; verify schedule in YouTube Studio")
            self.queue.relocate(self.db.get_video(video["id"]), self.db.get_video(video["id"])["upload_status"])
            report.reconciled.append(video["id"])
            return

        slot = from_iso(video["scheduled_publish_at"]) or from_iso(job["publish_at_snapshot"])
        total = job["total_bytes"] or (Path(video["file_path"]).stat().st_size if Path(video["file_path"]).exists() else 0)

        if job["resumable_uri"]:
            try:
                status = self.uploader.query_session(job["resumable_uri"], total)
            except UploadError as exc:
                self._mark_needs_review(video, job, f"Could not query the interrupted upload session ({exc})", report)
                return
            if status.state == "complete" and status.result:
                self.db.finish_job(job["id"], "reconciled", "YouTube had completed the upload before the crash")
                self.uploads.finalize_success(video["id"], None, status.result, slot, via="recovery")
                self.db.log_event("recovery", f"Reconciled {video['filename']} with existing YouTube video "
                                              f"{status.result.video_id} (no re-upload)", "INFO", video["id"])
                report.reconciled.append(video["id"])
                return
            if status.state == "incomplete":
                safe_slot = slot is not None and slot > utc_now() + timedelta(minutes=30)
                if safe_slot and Path(video["file_path"]).exists():
                    self.db.finish_job(job["id"], "failed", "Superseded by resumed session")
                    self.uploads.resume_upload(video["id"], job["resumable_uri"], status.offset)
                    report.resumed.append(video["id"])
                    return
                # Session incomplete -> YouTube has not created a video. Start fresh later.
                self._requeue(video, job, "Interrupted upload was incomplete; re-queued with a new publish slot",
                              report)
                return
            # expired: fall through to the channel search

        self._search_or_requeue(video, job, slot, report)

    def _recover_orphan_video(self, video: dict, report: RecoveryReport) -> None:
        job = self.db.latest_job_for_video(video["id"])
        if job and job["job_status"] in ("running", "interrupted"):
            return  # handled above
        slot = from_iso(video["scheduled_publish_at"])
        self._search_or_requeue(video, job, slot, report)

    def _search_or_requeue(self, video: dict, job: dict | None, slot, report: RecoveryReport) -> None:
        since = from_iso(job["started_at"]) if job else from_iso(video["updated_at"])
        title = (job or {}).get("title_snapshot") or video["title"]
        if not title:
            self._requeue(video, job, "Interrupted before upload started; re-queued", report)
            return
        try:
            match = self.uploader.find_recent_upload(title, since or utc_now())
        except UploadError as exc:
            self._mark_needs_review(video, job, f"Could not check YouTube for an interrupted upload ({exc}). "
                                                "Check YouTube Studio, then use Re-queue or leave as is.", report)
            return
        if match:
            if job:
                self.db.finish_job(job["id"], "reconciled", f"Matched existing YouTube video {match.video_id}")
            self.uploads.finalize_success(video["id"], None, match, slot or match.publish_at or utc_now(),
                                          via="recovery")
            self.db.log_event("recovery", f"Found {video['filename']} already on YouTube as {match.video_id}; "
                                          "recorded it instead of uploading again", "INFO", video["id"])
            report.reconciled.append(video["id"])
            return
        self._requeue(video, job, "Interrupted upload never reached YouTube; re-queued", report)

    def _requeue(self, video: dict, job: dict | None, reason: str, report: RecoveryReport) -> None:
        if job and job["job_status"] in ("running", "interrupted"):
            self.db.finish_job(job["id"], "failed", reason)
        self.db.update_video(video["id"], upload_status="queued", scheduled_publish_at=None, upload_day=None,
                             error_message=reason)
        self.queue.relocate(self.db.get_video(video["id"]), "queued")
        self.db.log_event("recovery", f"{video['filename']}: {reason}", "WARNING", video["id"])
        report.requeued.append(video["id"])

    def _mark_needs_review(self, video: dict, job: dict | None, reason: str, report: RecoveryReport) -> None:
        if job and job["job_status"] in ("running", "interrupted"):
            self.db.finish_job(job["id"], "failed", reason)
        self.db.update_video(video["id"], upload_status="needs_review", error_message=reason)
        self.db.log_event("recovery", f"{video['filename']}: {reason}", "ERROR", video["id"])
        report.needs_review.append(video["id"])

    # --- files ------------------------------------------------------------------------------
    def _restore_stray_files(self, report: RecoveryReport) -> None:
        processing = self.queue.cfg.processing_dir
        if not processing.is_dir():
            return
        by_path = {os.path.normcase(v["file_path"]): v for v in self.db.list_videos()}
        for path in processing.iterdir():
            if not path.is_file():
                continue
            video = by_path.get(os.path.normcase(str(path)))
            if video is None:
                moved = move_file(path, self.queue.cfg.incoming_dir)
                report.files_restored.append(moved.name)
                self.db.log_event("recovery", f"Moved unknown file {path.name} from processing/ back to incoming/")
            elif video["upload_status"] != "uploading":
                self.queue.relocate(video, video["upload_status"])
                report.files_restored.append(path.name)
