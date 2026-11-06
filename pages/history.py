"""Upload History page with filtering, search and follow-up actions."""
from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from database import from_iso
from services.scheduling_service import get_zone
from ui import STATUS_STYLE, fmt_local, flash, get_db, get_queue, page_header, show_flash, status_label

HISTORY_STATUSES = ["scheduled", "published", "needs_review", "failed", "invalid", "duplicate", "uploading",
                    "retry_wait", "queued", "pending_approval", "skipped"]


def render() -> None:
    db = get_db()
    settings = db.get_settings()
    tz = get_zone(settings["timezone"])
    page_header("Upload History", "Everything the app has uploaded, scheduled or rejected")
    show_flash()

    f1, f2, f3 = st.columns([2, 2, 2])
    statuses = f1.multiselect("Status", HISTORY_STATUSES, default=["scheduled", "published", "needs_review",
                                                                     "failed", "invalid", "duplicate"],
                              format_func=status_label)
    search = f2.text_input("Search title, filename or video ID")
    today = date.today()
    period = f3.date_input("Added between", value=(today - timedelta(days=90), today))

    videos = db.list_videos(statuses or HISTORY_STATUSES, order="COALESCE(uploaded_at, updated_at) DESC")
    if search:
        needle = search.lower()
        videos = [v for v in videos if needle in (v["title"] or "").lower() or needle in v["filename"].lower()
                  or needle in (v["youtube_video_id"] or "").lower()]
    if isinstance(period, tuple) and len(period) == 2:
        start, end = period
        videos = [v for v in videos if start <= from_iso(v["created_at"]).astimezone(tz).date() <= end]

    st.caption(f"{len(videos)} video(s)")
    st.dataframe(
        [{
            "ID": v["id"],
            "Title": v["title"] or v["filename"],
            "Status": status_label(v["upload_status"]),
            "YouTube ID": v["youtube_video_id"] or "",
            "YouTube URL": v["youtube_url"],
            "Uploaded": fmt_local(v["uploaded_at"]),
            "Publishes": fmt_local(v["scheduled_publish_at"]),
            "Retries": v["retry_count"],
            "Error": v["error_message"] or "",
            "File": v["filename"],
        } for v in videos],
        hide_index=True, use_container_width=True,
        column_config={"YouTube URL": st.column_config.LinkColumn("YouTube URL", display_text="Open")},
    )

    actionable = [v for v in videos if v["upload_status"] in ("failed", "needs_review") and not v["youtube_video_id"]]
    review = [v for v in videos if v["upload_status"] == "needs_review" and v["youtube_video_id"]]
    if actionable:
        st.subheader("Retry failed uploads")
        st.caption("Re-queuing moves the file back to incoming/ and resets its retry counter. Videos that "
                   "might already be on YouTube are marked 'Needs review' - check YouTube Studio first.")
        choice = st.selectbox("Video", [v["id"] for v in actionable],
                              format_func=lambda i: next(f"#{v['id']} · {v['filename']} · {v['error_message'] or ''}"
                                                         for v in actionable if v["id"] == i))
        if st.button("↩️ Re-queue selected video"):
            try:
                get_queue().restore_to_queue(choice)
                flash("Video re-queued.")
            except ValueError as exc:
                flash(str(exc), "error")
            st.rerun()
    if review:
        st.subheader("Uploaded - needs review")
        for v in review:
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{v['title']}** ([open]({v['youtube_url']})) — {v['error_message'] or ''}")
            if c2.button("Mark as OK", key=f"ok_{v['id']}"):
                db.update_video(v["id"], upload_status="scheduled", error_message=None)
                db.log_event("upload", f"Marked '{v['title']}' as verified by user", "INFO", v["id"])
                st.rerun()

    with st.expander("Status legend"):
        for key, (color, label) in STATUS_STYLE.items():
            st.markdown(f":{color}-badge[{label}] `{key}`")
