"""Incoming-folder monitoring and video queue state.

Folder ownership per status:
  incoming/   queued, pending_approval, retry_wait, skipped
  processing/ uploading
  uploaded/   scheduled, published, needs_review (once a YouTube id exists)
  failed/     failed, invalid, duplicate

Original files are only ever *moved* between these folders, never deleted.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from config import AppConfig
from database import Database, from_iso, to_iso, utc_now
from video_processor import FileStabilityTracker, file_sha256, validate_video

log = logging.getLogger("ysa.queue")

_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PARTIAL_SUFFIXES = (".partial", ".part", ".crdownload", ".tmp", ".download")


class UnsafePathError(ValueError):
    pass


def safe_filename(name: str, max_length: int = 180) -> str:
    """Return a filesystem-safe version of a user supplied filename (no directories allowed)."""
    if not name or name.strip() in {"", ".", ".."}:
        raise UnsafePathError("Empty filename")
    base = name.replace("\\", "/").split("/")[-1]
    if base != name.replace("\\", "/"):
        raise UnsafePathError("Filename must not contain directory components")
    cleaned = _INVALID_CHARS.sub("_", base).strip().rstrip(".")
    stem, ext = os.path.splitext(cleaned)
    if not stem or stem.lower() in _RESERVED_NAMES:
        raise UnsafePathError(f"'{name}' is not an allowed filename")
    if len(cleaned) > max_length:
        cleaned = stem[: max_length - len(ext)] + ext
    return cleaned


def ensure_within(base: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and make sure it lives inside ``base``."""
    base_resolved = base.resolve()
    resolved = candidate.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise UnsafePathError(f"Path {candidate} is outside {base}")
    return resolved


def move_file(src: Path, dest_dir: Path) -> Path:
    """Move without ever overwriting: name collisions get a numeric suffix."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src.name
    counter = 1
    while target.exists():
        if target.resolve() == src.resolve():
            return target
        target = dest_dir / f"{src.stem}_{counter}{src.suffix}"
        counter += 1
    try:
        os.rename(src, target)
    except OSError:
        shutil.move(str(src), str(target))  # cross-device fallback (copy + remove source)
    return target


class QueueManager:
    def __init__(self, cfg: AppConfig, db: Database, metadata_service=None):
        self.cfg = cfg
        self.db = db
        self.metadata_service = metadata_service
        self.tracker = FileStabilityTracker(cfg.file_stable_seconds)

    # --- folder helpers -----------------------------------------------------------
    def folder_for_status(self, status: str) -> Path:
        if status == "uploading":
            return self.cfg.processing_dir
        if status in ("scheduled", "published", "needs_review"):
            return self.cfg.uploaded_dir
        if status in ("failed", "invalid", "duplicate"):
            return self.cfg.failed_dir
        return self.cfg.incoming_dir

    def relocate(self, video: dict, status: str) -> Path:
        """Move a video's file into the folder that matches ``status`` and record the new path."""
        current = Path(video["file_path"])
        target_dir = self.folder_for_status(status)
        if not current.exists():
            log.warning("File for video %s is missing at %s", video["id"], current)
            return current
        ensure_within(self.cfg.data_dir, current)
        if current.parent.resolve() == target_dir.resolve():
            return current
        new_path = move_file(current, target_dir)
        self.db.update_video(video["id"], file_path=str(new_path), filename=new_path.name)
        video["file_path"], video["filename"] = str(new_path), new_path.name
        return new_path

    def incoming_candidates(self) -> list[Path]:
        if not self.cfg.incoming_dir.is_dir():
            return []
        files = []
        for entry in self.cfg.incoming_dir.iterdir():
            if not entry.is_file() or entry.name.startswith((".", "~$")):
                continue
            if entry.name.lower().endswith(PARTIAL_SUFFIXES):
                continue
            if entry.suffix.lower() in self.cfg.allowed_extensions:
                files.append(entry)
        return sorted(files, key=lambda p: p.stat().st_mtime)

    def unregistered_files(self) -> list[Path]:
        known = {os.path.normcase(p) for p in self.db.known_file_paths()}
        return [p for p in self.incoming_candidates() if os.path.normcase(str(p)) not in known]

    # --- scanning -------------------------------------------------------------------
    def scan_incoming(self) -> list[int]:
        """Register every fully-copied new file. Returns the ids of newly registered videos."""
        registered = []
        for path in self.unregistered_files():
            if not self.tracker.is_ready(path):
                continue
            try:
                video_id = self.register_file(path)
            except Exception as exc:  # never let one bad file stop the scan
                log.exception("Failed to register %s", path.name)
                self.db.log_event("queue", f"Could not register {path.name}: {exc}", "ERROR")
                continue
            self.tracker.forget(path)
            if video_id is not None:
                registered.append(video_id)
        return registered

    def register_file(self, path: Path) -> int | None:
        ensure_within(self.cfg.incoming_dir, path)
        if path.name != safe_filename(path.name):
            renamed = path.with_name(safe_filename(path.name))
            if not renamed.exists():
                os.rename(path, renamed)
                path = renamed
        size = path.stat().st_size
        digest = file_sha256(path)
        existing = self.db.find_video_by_hash(digest)
        if existing:
            moved = move_file(path, self.cfg.failed_dir)
            video_id = self.db.insert_video(
                filename=moved.name, file_path=str(moved), file_hash=digest, file_size=size,
                upload_status="duplicate", duplicate_of=existing["id"],
                error_message=f"Duplicate of '{existing['filename']}' (video #{existing['id']}, "
                              f"status {existing['upload_status']})",
            )
            self.db.log_event("queue", f"Duplicate file {path.name} moved to failed/ "
                                       f"(same content as #{existing['id']})", "WARNING", video_id)
            return video_id

        result = validate_video(path, self.cfg.shorts_max_duration, self.cfg.allowed_extensions,
                                self.cfg.ffprobe_path, self.cfg.ffmpeg_path)
        info = result.info
        common = dict(
            file_hash=digest, file_size=size,
            duration_seconds=info.duration if info else None,
            width=info.display_width if info else None,
            height=info.display_height if info else None,
            validation_warnings="; ".join(result.warnings) or None,
        )
        if not result.ok:
            moved = move_file(path, self.cfg.failed_dir)
            video_id = self.db.insert_video(filename=moved.name, file_path=str(moved), upload_status="invalid",
                                            error_message="; ".join(result.errors), **common)
            self.db.log_event("queue", f"Rejected {path.name}: {'; '.join(result.errors)}", "ERROR", video_id)
            return video_id

        settings = self.db.get_settings()
        status = "pending_approval" if settings["manual_approval"] else "queued"
        video_id = self.db.insert_video(filename=path.name, file_path=str(path), upload_status=status,
                                        topic=settings["default_topic"] or None,
                                        contains_synthetic_media=settings["contains_synthetic_media"], **common)
        self.db.log_event("queue", f"Queued {path.name} ({info.duration:.1f}s, {info.display_width}x"
                                   f"{info.display_height})", "INFO", video_id,
                          {"warnings": result.warnings} if result.warnings else None)
        if self.metadata_service is not None:
            try:
                self.metadata_service.ensure_metadata(video_id)
            except Exception as exc:
                log.exception("Metadata generation failed for %s", path.name)
                self.db.log_event("metadata", f"Metadata generation failed for {path.name}: {exc}", "ERROR", video_id)
        return video_id

    # --- selection -------------------------------------------------------------------
    def eligible_videos(self, now: datetime | None = None) -> list[dict]:
        now = now or utc_now()
        settings = self.db.get_settings()
        statuses = ["queued", "retry_wait"] + ([] if settings["manual_approval"] else ["pending_approval"])
        out = []
        for video in self.db.list_videos(statuses):
            if video["youtube_video_id"]:
                continue  # already on YouTube - never upload twice
            if video["upload_status"] == "retry_wait":
                due = from_iso(video["next_retry_at"])
                if due and due > now:
                    continue
            if not Path(video["file_path"]).exists():
                continue
            out.append(video)
        return out

    def next_eligible(self, now: datetime | None = None) -> dict | None:
        videos = self.eligible_videos(now)
        return videos[0] if videos else None

    # --- user actions ------------------------------------------------------------
    def remove_from_queue(self, video_id: int) -> None:
        video = self.db.get_video(video_id)
        if not video or video["upload_status"] not in ("queued", "pending_approval", "retry_wait"):
            raise ValueError("Only waiting videos can be removed from the queue")
        self.db.update_video(video_id, upload_status="skipped", manual_request=0)
        self.db.log_event("queue", f"Removed {video['filename']} from the queue (file kept in incoming/)",
                          "INFO", video_id)

    def restore_to_queue(self, video_id: int) -> None:
        video = self.db.get_video(video_id)
        if not video or video["upload_status"] not in ("skipped", "failed", "needs_review"):
            raise ValueError("This video cannot be re-queued")
        if video["youtube_video_id"]:
            raise ValueError("This video already exists on YouTube; re-queuing would create a duplicate")
        self.relocate(video, "queued")
        settings = self.db.get_settings()
        status = "pending_approval" if settings["manual_approval"] and video["metadata_status"] != "approved" else "queued"
        self.db.update_video(video_id, upload_status=status, retry_count=0, next_retry_at=None,
                             error_message=None, scheduled_publish_at=None, upload_day=None)
        self.db.log_event("queue", f"Re-queued {video['filename']}", "INFO", video_id)

    def request_upload_now(self, video_id: int, override_limit: bool = False) -> None:
        video = self.db.get_video(video_id)
        if not video or video["upload_status"] not in ("queued", "pending_approval", "retry_wait"):
            raise ValueError("Only waiting videos can be uploaded")
        if video["upload_status"] == "pending_approval" and self.db.get_settings()["manual_approval"]:
            raise ValueError("Approve the metadata before uploading")
        self.db.update_video(video_id, manual_request=2 if override_limit else 1)
        self.db.log_event("queue", f"Manual upload requested for {video['filename']}", "INFO", video_id)

    def save_uploaded_file(self, name: str, data: bytes) -> Path:
        """Store a file uploaded through the dashboard into incoming/ atomically."""
        filename = safe_filename(name)
        if Path(filename).suffix.lower() not in self.cfg.allowed_extensions:
            raise UnsafePathError(f"Only {', '.join(self.cfg.allowed_extensions)} files are accepted")
        target = ensure_within(self.cfg.incoming_dir, self.cfg.incoming_dir / filename)
        counter = 1
        while target.exists():
            target = self.cfg.incoming_dir / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
            counter += 1
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(data)
        os.replace(partial, target)
        return target
