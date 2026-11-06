"""Glue between the metadata generator, sidecar overrides and the database."""
from __future__ import annotations

import logging
from pathlib import Path

from config import AppConfig
from database import Database
from metadata_generator import (CATEGORIES, MetadataGenerator, SidecarError, VideoMetadata, find_sidecar,
                                load_sidecar, normalize_hashtag, sanitize)

log = logging.getLogger("ysa.metadata")

USER_OWNED = {"edited", "approved"}  # never overwritten automatically


class MetadataService:
    def __init__(self, cfg: AppConfig, db: Database, generator: MetadataGenerator | None = None):
        self.cfg = cfg
        self.db = db
        self._generator = generator

    def generator(self) -> MetadataGenerator:
        if self._generator is not None:
            return self._generator
        settings = self.db.get_settings()
        return MetadataGenerator(self.cfg.anthropic_api_key, self.cfg.anthropic_model,
                                 settings["default_language"], settings["default_category_id"])

    def sidecar_for(self, video: dict) -> tuple[dict, str | None]:
        path = find_sidecar(Path(video["file_path"]), self.cfg.metadata_dir, self.cfg.incoming_dir)
        if path is None:
            return {}, None
        try:
            return load_sidecar(path), None
        except SidecarError as exc:
            return {}, str(exc)

    def ensure_metadata(self, video_id: int, force: bool = False, topic: str | None = None) -> dict:
        """Generate metadata unless the user already edited/approved it (or ``force``)."""
        video = self.db.get_video(video_id)
        if video is None:
            raise ValueError(f"Video {video_id} not found")
        if not force and video["title"] and video["metadata_status"] in USER_OWNED:
            return video
        if not force and video["title"] and video["metadata_status"] != "none":
            return video

        sidecar, sidecar_error = self.sidecar_for(video)
        if sidecar_error:
            self.db.log_event("metadata", f"Sidecar ignored: {sidecar_error}", "WARNING", video_id)
        effective_topic = topic if topic is not None else video.get("topic")
        meta = self.generator().generate(video["filename"], effective_topic, sidecar)
        self._store(video_id, meta, status=meta.source if meta.source != "anthropic" else "generated",
                    topic=effective_topic)
        for warning in meta.warnings:
            self.db.log_event("metadata", warning, "WARNING", video_id)
        self.db.log_event("metadata", f"Metadata ready ({meta.source}): {meta.title}", "INFO", video_id)
        return self.db.get_video(video_id)

    def refresh_override_before_upload(self, video_id: int) -> dict:
        """Apply a sidecar added after registration, unless the user edited metadata in the dashboard."""
        video = self.db.get_video(video_id)
        if video["metadata_status"] in USER_OWNED and video["title"]:
            return video
        sidecar, _ = self.sidecar_for(video)
        if sidecar or not video["title"]:
            return self.ensure_metadata(video_id, force=True)
        return video

    def save_edit(self, video_id: int, title: str, description: str, tags: list[str], hashtags: list[str],
                  category_id: str, language: str, contains_synthetic_media: bool, approve: bool) -> dict:
        meta = sanitize(VideoMetadata(
            title=title, description=description, tags=tags,
            hashtags=[normalize_hashtag(h) for h in hashtags if h.strip()],
            category_id=category_id if category_id in CATEGORIES else "22", language=language or "en",
            source="edited", contains_synthetic_media=contains_synthetic_media))
        self._store(video_id, meta, status="approved" if approve else "edited")
        video = self.db.get_video(video_id)
        if approve and video["upload_status"] == "pending_approval":
            self.db.update_video(video_id, upload_status="queued")
        self.db.log_event("metadata", f"Metadata {'approved' if approve else 'edited'}: {meta.title}", "INFO", video_id)
        return self.db.get_video(video_id)

    def approve(self, video_id: int) -> None:
        video = self.db.get_video(video_id)
        if not video["title"]:
            raise ValueError("Generate metadata before approving")
        fields = {"metadata_status": "approved"}
        if video["upload_status"] == "pending_approval":
            fields["upload_status"] = "queued"
        self.db.update_video(video_id, **fields)
        self.db.log_event("metadata", f"Metadata approved: {video['title']}", "INFO", video_id)

    def _store(self, video_id: int, meta: VideoMetadata, status: str, topic: str | None = None) -> None:
        fields = dict(title=meta.title, description=meta.description, tags=meta.tags, hashtags=meta.hashtags,
                      category_id=meta.category_id, language=meta.language, metadata_status=status,
                      metadata_source=meta.source)
        if meta.contains_synthetic_media is not None:
            fields["contains_synthetic_media"] = bool(meta.contains_synthetic_media)
        if topic is not None:
            fields["topic"] = topic or None
        self.db.update_video(video_id, **fields)
