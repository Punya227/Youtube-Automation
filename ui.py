"""Shared Streamlit helpers: cached services, status badges and formatting."""
from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from config import AppConfig, ensure_directories, load_config
from database import Database, from_iso, utc_now
from queue_manager import QueueManager
from services.metadata_service import MetadataService
from services.scheduling_service import format_local

STATUS_STYLE = {
    "queued": ("blue", "Queued"),
    "pending_approval": ("orange", "Needs approval"),
    "uploading": ("violet", "Uploading"),
    "retry_wait": ("orange", "Retry pending"),
    "scheduled": ("green", "Scheduled"),
    "published": ("green", "Published"),
    "needs_review": ("orange", "Needs review"),
    "failed": ("red", "Failed"),
    "invalid": ("red", "Invalid"),
    "duplicate": ("gray", "Duplicate"),
    "skipped": ("gray", "Removed"),
}

CSS = """
<style>
[data-testid="stMetricValue"] { font-size: 1.9rem; font-weight: 650; }
[data-testid="stMetricLabel"] p { font-size: 0.85rem; opacity: 0.8; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 14px; }
.ysa-muted { opacity: 0.7; font-size: 0.85rem; }
.ysa-hero { font-size: 1.05rem; font-weight: 600; margin: 0; }
</style>
"""


@st.cache_resource
def get_config() -> AppConfig:
    cfg = load_config()
    ensure_directories(cfg)
    return cfg


@st.cache_resource
def get_db() -> Database:
    return Database(get_config().db_path)


def get_metadata_service() -> MetadataService:
    return MetadataService(get_config(), get_db())


def get_queue() -> QueueManager:
    return QueueManager(get_config(), get_db(), get_metadata_service())


def status_badge(status: str) -> str:
    color, label = STATUS_STYLE.get(status, ("gray", status))
    return f":{color}-badge[{label}]"


def status_label(status: str) -> str:
    return STATUS_STYLE.get(status, ("gray", status))[1]


def fmt_local(value: str | datetime | None, tz: str | None = None, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    if value is None or value == "":
        return "-"
    dt = from_iso(value) if isinstance(value, str) else value
    return format_local(dt, tz or get_db().get_settings()["timezone"], fmt)


def fmt_size(num: int | None) -> str:
    if not num:
        return "-"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def worker_status() -> tuple[bool, str]:
    db = get_db()
    beat = from_iso(db.get_state("worker_heartbeat"))
    if beat and utc_now() - beat < timedelta(seconds=90):
        pid = db.get_state("worker_pid")
        return True, f"Worker running (pid {pid}), last heartbeat {int((utc_now() - beat).total_seconds())}s ago"
    if beat:
        return False, f"Worker offline - last heartbeat {fmt_local(beat)} (stopped unexpectedly or PC asleep)"
    if db.get_state("worker_started_at"):
        return False, f"Worker stopped (last started {fmt_local(db.get_state('worker_started_at'))})"
    return False, "Worker has not been started"


def page_header(title: str, subtitle: str = "") -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def flash(message: str, kind: str = "success") -> None:
    st.session_state["_flash"] = (kind, message)


def show_flash() -> None:
    item = st.session_state.pop("_flash", None)
    if item:
        kind, message = item
        getattr(st, kind, st.info)(message)
