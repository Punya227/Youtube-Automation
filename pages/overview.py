"""Overview page: channel, counters, next run times, recent activity."""
from __future__ import annotations

import html

import streamlit as st

from database import from_iso, utc_now
from services.scheduling_service import compute_publish_slot, format_local, local_today, next_upload_run
from ui import fmt_local, get_config, get_db, get_queue, page_header, status_badge, worker_status


def render() -> None:
    cfg = get_config()
    db = get_db()
    settings = db.get_settings()
    counts = db.status_counts()
    page_header("Overview", "Daily YouTube Shorts automation at a glance")

    auth_error = db.get_state("auth_error")
    if auth_error:
        st.error(f"YouTube authorization problem: {auth_error}. Reconnect on the YouTube Settings page.", icon="🔑")
    online, worker_text = worker_status()
    if not online:
        st.warning(f"{worker_text}. Scheduled uploads only happen while the worker runs "
                   "(start.bat or the Windows startup task).", icon="⚠️")

    c1, c2, c3, c4 = st.columns(4)
    with c1.container(border=True):
        st.metric("Uploaded to YouTube", counts["scheduled"] + counts["published"] + counts["needs_review"])
    with c2.container(border=True):
        st.metric("Scheduled (not yet public)", counts["scheduled"])
    with c3.container(border=True):
        waiting = counts["queued"] + counts["pending_approval"] + counts["retry_wait"]
        unregistered = len(get_queue().unregistered_files())
        st.metric("Waiting in queue", waiting, delta=f"+{unregistered} copying/new" if unregistered else None,
                  delta_color="off")
    with c4.container(border=True):
        st.metric("Failed / invalid", counts["failed"] + counts["invalid"] + counts["duplicate"])

    now = utc_now()
    next_run = next_upload_run(now, settings)
    next_slot = compute_publish_slot(next_run or now, settings, db.taken_publish_slots())
    channel = db.get_active_channel()

    left, middle, right = st.columns(3)
    with left.container(border=True):
        st.markdown("**📺 Channel**")
        if channel:
            st.markdown(f"<p class='ysa-hero'>{html.escape(channel['channel_name'] or '')}</p>", unsafe_allow_html=True)
            st.caption(f"ID {channel['channel_id']} · connected {fmt_local(channel['connected_at'])}")
        else:
            st.markdown("<p class='ysa-hero'>Not connected</p>", unsafe_allow_html=True)
            st.caption("Connect on the YouTube Settings page" + (" (optional in mock mode)" if cfg.is_mock else ""))
        st.markdown(":orange-badge[Mock mode]" if cfg.is_mock else ":green-badge[Live mode]")
    with middle.container(border=True):
        st.markdown("**⏰ Scheduler**")
        if settings["scheduler_enabled"]:
            st.markdown(":green-badge[Enabled]")
        else:
            st.markdown(":gray-badge[Disabled]")
        st.caption(f"Next upload run: **{format_local(next_run, settings['timezone']) if next_run else '-'}**")
        st.caption(f"Next publication slot: **{format_local(next_slot, settings['timezone'])}**")
        st.caption(f"{settings['daily_limit']} video(s)/day · {settings['timezone']}"
                   f"{'' if settings['upload_on_weekends'] else ' · weekdays only'}")
    with right.container(border=True):
        st.markdown("**🎯 Today**")
        today = local_today(now, settings).isoformat()
        done = db.count_uploads_on_day(today)
        st.progress(min(1.0, done / max(1, settings["daily_limit"])),
                    text=f"{done} of {settings['daily_limit']} daily upload(s) used")
        last_run = db.get_state("last_daily_run_at")
        st.caption(f"Last run: {fmt_local(last_run) if last_run else 'never'}")
        uploading = db.list_videos(["uploading"])
        if uploading:
            job = db.latest_job_for_video(uploading[0]["id"])
            if job and job["total_bytes"]:
                st.progress(min(1.0, (job["bytes_uploaded"] or 0) / job["total_bytes"]),
                            text=f"Uploading {uploading[0]['filename']}")

    st.subheader("Upcoming publications")
    upcoming = [v for v in db.list_videos(["scheduled"], order="scheduled_publish_at ASC")
                if from_iso(v["scheduled_publish_at"]) and from_iso(v["scheduled_publish_at"]) > now][:8]
    if upcoming:
        st.dataframe(
            [{"Publishes": fmt_local(v["scheduled_publish_at"]), "Title": v["title"],
              "YouTube": v["youtube_url"]} for v in upcoming],
            hide_index=True, use_container_width=True,
            column_config={"YouTube": st.column_config.LinkColumn("YouTube", display_text="Open")},
        )
    else:
        st.caption("Nothing scheduled yet.")

    needs_attention = db.list_videos(["needs_review", "failed", "invalid"], order="updated_at DESC", limit=5)
    if needs_attention:
        st.subheader("Needs attention")
        for video in needs_attention:
            st.markdown(f"{status_badge(video['upload_status'])} **{video['filename']}** — "
                        f"{video['error_message'] or ''}")

    st.subheader("Recent activity")
    events = db.list_events(limit=12)
    if events:
        st.dataframe(
            [{"Time": fmt_local(e["created_at"], fmt="%d %b %I:%M:%S %p"), "Level": e["level"],
              "Category": e["category"], "Message": e["message"]} for e in events],
            hide_index=True, use_container_width=True,
        )
    else:
        st.caption("No activity yet.")
