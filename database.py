"""SQLite persistence layer.

Every write goes through ``Database.transaction()`` which opens a
``BEGIN IMMEDIATE`` transaction, so the worker and the dashboard never
interleave partial updates. Unique indexes enforce the invariants that
prevent duplicate processing:

* one non-duplicate ``videos`` row per file hash,
* one ``youtube_video_id`` per row,
* at most one ``upload_jobs`` row in ``running`` state (no concurrent uploads),
* at most one video per scheduled publication instant.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from logging_setup import redact

VIDEO_STATUSES = (
    "queued",            # waiting in incoming/, eligible for upload
    "pending_approval",  # manual approval mode: metadata must be approved first
    "uploading",         # upload in progress (file in processing/)
    "retry_wait",        # temporary failure, will retry after next_retry_at
    "scheduled",         # uploaded as private with publishAt in the future
    "published",         # publishAt passed and YouTube reports the video public
    "needs_review",      # uploaded/possibly uploaded but needs a human check
    "failed",            # permanent failure (file in failed/)
    "invalid",           # failed validation (file in failed/)
    "duplicate",         # same content as an earlier video (file in failed/)
    "skipped",           # removed from the queue by the user (file left in incoming/)
)
# Statuses that occupy a slot / count as "uploaded today".
ACTIVE_UPLOAD_STATUSES = ("uploading", "scheduled", "published", "needs_review")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS channels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id    TEXT NOT NULL UNIQUE,
    channel_name  TEXT,
    uploads_playlist_id TEXT,
    connected_at  TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS videos (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    filename              TEXT NOT NULL,
    file_path             TEXT NOT NULL,
    file_hash             TEXT NOT NULL,
    file_size             INTEGER,
    duration_seconds      REAL,
    width                 INTEGER,
    height                INTEGER,
    title                 TEXT,
    description           TEXT,
    tags                  TEXT NOT NULL DEFAULT '[]',
    hashtags              TEXT NOT NULL DEFAULT '[]',
    category_id           TEXT,
    language              TEXT,
    topic                 TEXT,
    contains_synthetic_media INTEGER NOT NULL DEFAULT 0,
    metadata_status       TEXT NOT NULL DEFAULT 'none',
    metadata_source       TEXT,
    youtube_video_id      TEXT UNIQUE,
    youtube_url           TEXT,
    upload_status         TEXT NOT NULL DEFAULT 'queued'
                          CHECK (upload_status IN ({", ".join(repr(s) for s in VIDEO_STATUSES)})),
    scheduled_publish_at  TEXT,
    upload_day            TEXT,
    uploaded_at           TEXT,
    published_at          TEXT,
    error_message         TEXT,
    validation_warnings   TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    next_retry_at         TEXT,
    duplicate_of          INTEGER REFERENCES videos(id),
    manual_request        INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_videos_hash
    ON videos(file_hash) WHERE upload_status <> 'duplicate';
CREATE UNIQUE INDEX IF NOT EXISTS ux_videos_publish_slot
    ON videos(scheduled_publish_at)
    WHERE scheduled_publish_at IS NOT NULL
      AND upload_status IN ('uploading', 'scheduled', 'published', 'needs_review');
CREATE INDEX IF NOT EXISTS ix_videos_status ON videos(upload_status, created_at);

CREATE TABLE IF NOT EXISTS settings (
    id                        INTEGER PRIMARY KEY CHECK (id = 1),
    upload_time               TEXT NOT NULL DEFAULT '08:00',
    publish_time              TEXT NOT NULL DEFAULT '18:00',
    timezone                  TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    daily_limit               INTEGER NOT NULL DEFAULT 1 CHECK (daily_limit BETWEEN 1 AND 20),
    scheduler_enabled         INTEGER NOT NULL DEFAULT 1,
    upload_on_weekends        INTEGER NOT NULL DEFAULT 1,
    manual_approval           INTEGER NOT NULL DEFAULT 0,
    catch_up_missed_runs      INTEGER NOT NULL DEFAULT 1,
    publish_spacing_minutes   INTEGER NOT NULL DEFAULT 60,
    min_publish_lead_minutes  INTEGER NOT NULL DEFAULT 60,
    max_retries               INTEGER NOT NULL DEFAULT 3,
    default_language          TEXT NOT NULL DEFAULT 'en',
    default_category_id       TEXT NOT NULL DEFAULT '22',
    default_topic             TEXT NOT NULL DEFAULT '',
    contains_synthetic_media  INTEGER NOT NULL DEFAULT 0,
    notify_subscribers        INTEGER NOT NULL DEFAULT 1,
    updated_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS upload_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id            INTEGER NOT NULL REFERENCES videos(id),
    job_status          TEXT NOT NULL
                        CHECK (job_status IN ('running', 'succeeded', 'failed', 'interrupted', 'reconciled')),
    trigger             TEXT NOT NULL DEFAULT 'scheduled',
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    error_message       TEXT,
    worker_pid          INTEGER,
    resumable_uri       TEXT,
    bytes_uploaded      INTEGER NOT NULL DEFAULT 0,
    total_bytes         INTEGER,
    title_snapshot      TEXT,
    publish_at_snapshot TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_running_job
    ON upload_jobs(job_status) WHERE job_status = 'running';

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level      TEXT NOT NULL,
    category   TEXT NOT NULL,
    message    TEXT NOT NULL,
    video_id   INTEGER,
    details    TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
"""

SETTINGS_FIELDS = {
    "upload_time": str,
    "publish_time": str,
    "timezone": str,
    "daily_limit": int,
    "scheduler_enabled": bool,
    "upload_on_weekends": bool,
    "manual_approval": bool,
    "catch_up_missed_runs": bool,
    "publish_spacing_minutes": int,
    "min_publish_lead_minutes": int,
    "max_retries": int,
    "default_language": str,
    "default_category_id": str,
    "default_topic": str,
    "contains_synthetic_media": bool,
    "notify_subscribers": bool,
}

VIDEO_UPDATABLE = {
    "filename", "file_path", "file_size", "duration_seconds", "width", "height",
    "title", "description", "tags", "hashtags", "category_id", "language", "topic",
    "contains_synthetic_media", "metadata_status", "metadata_source",
    "youtube_video_id", "youtube_url", "upload_status", "scheduled_publish_at",
    "upload_day", "uploaded_at", "published_at", "error_message", "validation_warnings",
    "retry_count", "next_retry_at", "duplicate_of", "manual_request",
}
JOB_UPDATABLE = {
    "job_status", "completed_at", "error_message", "resumable_uri",
    "bytes_uploaded", "total_bytes", "title_snapshot", "publish_at_snapshot",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are not allowed")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _encode(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    if isinstance(value, datetime):
        return to_iso(value)
    return value


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.init_schema()

    # --- connection handling ---------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        if conn.in_transaction:  # nested use joins the outer transaction
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.connect().execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = self.connect().execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO settings (id, updated_at) VALUES (1, ?)", (to_iso(utc_now()),)
        )

    # --- settings ----------------------------------------------------------
    def get_settings(self) -> dict:
        row = self.query_one("SELECT * FROM settings WHERE id = 1")
        assert row is not None
        for key, typ in SETTINGS_FIELDS.items():
            if typ is bool:
                row[key] = bool(row[key])
        return row

    def update_settings(self, **values: Any) -> dict:
        unknown = set(values) - set(SETTINGS_FIELDS)
        if unknown:
            raise ValueError(f"Unknown settings: {sorted(unknown)}")
        if not values:
            return self.get_settings()
        assignments = ", ".join(f"{k} = ?" for k in values)
        params = [_encode(v) for v in values.values()] + [to_iso(utc_now())]
        with self.transaction() as conn:
            conn.execute(f"UPDATE settings SET {assignments}, updated_at = ? WHERE id = 1", params)
        return self.get_settings()

    # --- events (dashboard log) ---------------------------------------------
    def log_event(self, category: str, message: str, level: str = "INFO",
                  video_id: int | None = None, details: Any = None) -> None:
        if details is not None and not isinstance(details, str):
            details = json.dumps(details, ensure_ascii=False, default=str)
        message = redact(message)
        details = redact(details) if details else details
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO events (created_at, level, category, message, video_id, details) VALUES (?,?,?,?,?,?)",
                (to_iso(utc_now()), level.upper(), category, message, video_id, details),
            )

    def list_events(self, limit: int = 500, categories: list[str] | None = None,
                    levels: list[str] | None = None, search: str = "") -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if categories:
            sql += f" AND category IN ({','.join('?' * len(categories))})"
            params += categories
        if levels:
            sql += f" AND level IN ({','.join('?' * len(levels))})"
            params += levels
        if search:
            sql += " AND (message LIKE ? OR details LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    # --- app state (heartbeat, last run, ...) --------------------------------
    def set_state(self, key: str, value: Any) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, None if value is None else str(value), to_iso(utc_now())),
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM app_state WHERE key = ?", (key,))
        return row["value"] if row and row["value"] is not None else default

    # --- channels -------------------------------------------------------------
    def save_channel(self, channel_id: str, channel_name: str, uploads_playlist_id: str | None) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE channels SET is_active = 0")
            conn.execute(
                "INSERT INTO channels (channel_id, channel_name, uploads_playlist_id, connected_at, is_active) "
                "VALUES (?,?,?,?,1) ON CONFLICT(channel_id) DO UPDATE SET channel_name = excluded.channel_name, "
                "uploads_playlist_id = excluded.uploads_playlist_id, connected_at = excluded.connected_at, is_active = 1",
                (channel_id, channel_name, uploads_playlist_id, to_iso(utc_now())),
            )

    def get_active_channel(self) -> dict | None:
        return self.query_one("SELECT * FROM channels WHERE is_active = 1 ORDER BY connected_at DESC LIMIT 1")

    def deactivate_channels(self) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE channels SET is_active = 0")

    # --- videos ------------------------------------------------------------
    def insert_video(self, **fields: Any) -> int:
        now = to_iso(utc_now())
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        cols = ", ".join(fields)
        placeholders = ", ".join("?" * len(fields))
        with self.transaction() as conn:
            cur = conn.execute(f"INSERT INTO videos ({cols}) VALUES ({placeholders})",
                               [_encode(v) for v in fields.values()])
            return int(cur.lastrowid)

    def update_video(self, video_id: int, **fields: Any) -> None:
        unknown = set(fields) - VIDEO_UPDATABLE
        if unknown:
            raise ValueError(f"Unknown video fields: {sorted(unknown)}")
        if not fields:
            return
        if fields.get("error_message"):
            fields["error_message"] = redact(str(fields["error_message"]))
        assignments = ", ".join(f"{k} = ?" for k in fields)
        params = [_encode(v) for v in fields.values()] + [to_iso(utc_now()), video_id]
        with self.transaction() as conn:
            conn.execute(f"UPDATE videos SET {assignments}, updated_at = ? WHERE id = ?", params)

    def get_video(self, video_id: int) -> dict | None:
        return self._decode_video(self.query_one("SELECT * FROM videos WHERE id = ?", (video_id,)))

    def find_video_by_hash(self, file_hash: str) -> dict | None:
        return self._decode_video(self.query_one(
            "SELECT * FROM videos WHERE file_hash = ? AND upload_status <> 'duplicate'", (file_hash,)))

    def known_file_paths(self) -> set[str]:
        return {r["file_path"] for r in self.query("SELECT file_path FROM videos")}

    def list_videos(self, statuses: Iterable[str] | None = None, order: str = "created_at ASC, id ASC",
                    limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM videos"
        params: list[Any] = []
        statuses = list(statuses or [])
        if statuses:
            sql += f" WHERE upload_status IN ({','.join('?' * len(statuses))})"
            params += statuses
        sql += f" ORDER BY {order}"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._decode_video(r) for r in self.query(sql, params)]

    def status_counts(self) -> dict[str, int]:
        rows = self.query("SELECT upload_status, COUNT(*) AS n FROM videos GROUP BY upload_status")
        counts = {s: 0 for s in VIDEO_STATUSES}
        counts.update({r["upload_status"]: r["n"] for r in rows})
        return counts

    def count_uploads_on_day(self, local_day: str) -> int:
        row = self.query_one(
            f"SELECT COUNT(*) AS n FROM videos WHERE upload_day = ? AND upload_status IN "
            f"({','.join('?' * len(ACTIVE_UPLOAD_STATUSES))})",
            (local_day, *ACTIVE_UPLOAD_STATUSES),
        )
        return int(row["n"]) if row else 0

    def taken_publish_slots(self) -> set[datetime]:
        rows = self.query(
            f"SELECT scheduled_publish_at FROM videos WHERE scheduled_publish_at IS NOT NULL AND upload_status IN "
            f"({','.join('?' * len(ACTIVE_UPLOAD_STATUSES))})",
            ACTIVE_UPLOAD_STATUSES,
        )
        return {from_iso(r["scheduled_publish_at"]) for r in rows}

    @staticmethod
    def _decode_video(row: dict | None) -> dict | None:
        if row is None:
            return None
        for key in ("tags", "hashtags"):
            try:
                row[key] = json.loads(row.get(key) or "[]")
            except (TypeError, json.JSONDecodeError):
                row[key] = []
        row["contains_synthetic_media"] = bool(row.get("contains_synthetic_media"))
        return row

    # --- upload jobs ---------------------------------------------------------
    def start_job(self, video_id: int, trigger: str, worker_pid: int, total_bytes: int | None) -> int | None:
        """Create the single running job. Returns None if another upload is running."""
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO upload_jobs (video_id, job_status, trigger, started_at, worker_pid, total_bytes) "
                    "VALUES (?, 'running', ?, ?, ?, ?)",
                    (video_id, trigger, to_iso(utc_now()), worker_pid, total_bytes),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def update_job(self, job_id: int, **fields: Any) -> None:
        unknown = set(fields) - JOB_UPDATABLE
        if unknown:
            raise ValueError(f"Unknown job fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.transaction() as conn:
            conn.execute(f"UPDATE upload_jobs SET {assignments} WHERE id = ?",
                         [_encode(v) for v in fields.values()] + [job_id])

    def finish_job(self, job_id: int, status: str, error: str | None = None) -> None:
        self.update_job(job_id, job_status=status, completed_at=to_iso(utc_now()), error_message=error)

    def get_job(self, job_id: int) -> dict | None:
        return self.query_one("SELECT * FROM upload_jobs WHERE id = ?", (job_id,))

    def open_jobs(self) -> list[dict]:
        return self.query("SELECT * FROM upload_jobs WHERE job_status IN ('running', 'interrupted') ORDER BY id")

    def latest_job_for_video(self, video_id: int) -> dict | None:
        return self.query_one("SELECT * FROM upload_jobs WHERE video_id = ? ORDER BY id DESC LIMIT 1", (video_id,))

    def list_jobs(self, limit: int = 200) -> list[dict]:
        return self.query(
            "SELECT j.id, j.video_id, j.job_status, j.trigger, j.started_at, j.completed_at, j.error_message, "
            "j.bytes_uploaded, j.total_bytes, v.filename FROM upload_jobs j JOIN videos v ON v.id = j.video_id "
            "ORDER BY j.id DESC LIMIT ?", (limit,))
