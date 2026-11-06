"""Scheduler Settings page (persisted in SQLite, picked up by the worker automatically)."""
from __future__ import annotations

from datetime import time as dtime
from zoneinfo import available_timezones

import streamlit as st

from database import utc_now
from metadata_generator import CATEGORIES
from services.scheduling_service import compute_publish_slot, format_local, next_upload_run, parse_hhmm
from ui import flash, get_db, page_header, show_flash

COMMON_ZONES = ["Asia/Kolkata", "UTC", "Asia/Dubai", "Asia/Singapore", "Europe/London", "America/New_York",
                "America/Los_Angeles"]


def render() -> None:
    db = get_db()
    s = db.get_settings()
    page_header("Scheduler Settings", "The worker applies changes within a few seconds - no restart needed")
    show_flash()

    zones = COMMON_ZONES + sorted(z for z in available_timezones() if z not in COMMON_ZONES and "/" in z)
    with st.form("scheduler_settings", border=True):
        st.markdown("**Daily schedule**")
        c1, c2, c3 = st.columns(3)
        upload_time = c1.time_input("Daily upload time", value=parse_hhmm(s["upload_time"]), step=300)
        publish_time = c2.time_input("Daily publication time", value=parse_hhmm(s["publish_time"]), step=300)
        timezone_name = c3.selectbox("Timezone", zones, index=zones.index(s["timezone"]) if s["timezone"] in zones else 0)

        c4, c5, c6 = st.columns(3)
        daily_limit = c4.number_input("Videos per day", min_value=1, max_value=6, value=int(s["daily_limit"]),
                                      help="YouTube's default API quota (10,000 units/day) allows about 6 uploads.")
        spacing = c5.number_input("Minutes between publications (when >1/day)", min_value=5, max_value=720,
                                  value=int(s["publish_spacing_minutes"]))
        lead = c6.number_input("Minimum minutes between upload and publication", min_value=15, max_value=1440,
                               value=int(s["min_publish_lead_minutes"]))

        t1, t2, t3, t4 = st.columns(4)
        enabled = t1.toggle("Scheduler enabled", value=s["scheduler_enabled"])
        weekends = t2.toggle("Upload & publish on weekends", value=s["upload_on_weekends"])
        approval = t3.toggle("Manual approval mode", value=s["manual_approval"],
                             help="New videos wait for you to approve their metadata in the Video Queue.")
        catch_up = t4.toggle("Catch up missed runs", value=s["catch_up_missed_runs"],
                             help="If the PC was off at upload time, run today's upload when the worker starts.")

        st.markdown("**Retries & metadata defaults**")
        d1, d2, d3, d4 = st.columns(4)
        max_retries = d1.number_input("Max retries for temporary failures", min_value=0, max_value=10,
                                      value=int(s["max_retries"]))
        language = d2.text_input("Default language (ISO 639-1)", value=s["default_language"])
        cat_ids = list(CATEGORIES)
        category = d3.selectbox("Default category", cat_ids, index=cat_ids.index(s["default_category_id"])
                                if s["default_category_id"] in cat_ids else cat_ids.index("22"),
                                format_func=lambda c: CATEGORIES[c])
        notify = d4.toggle("Notify subscribers", value=s["notify_subscribers"])
        topic = st.text_input("Default topic for new videos (optional)", value=s["default_topic"],
                              help="Used for AI metadata when a video has no topic or sidecar file.")
        synthetic = st.checkbox("New videos contain realistic altered/synthetic (AI-generated) content",
                                value=s["contains_synthetic_media"])
        submitted = st.form_submit_button("Save settings", type="primary")

    if submitted:
        if language.strip() == "":
            st.error("Default language is required")
        else:
            db.update_settings(
                upload_time=_hhmm(upload_time), publish_time=_hhmm(publish_time), timezone=timezone_name,
                daily_limit=int(daily_limit), publish_spacing_minutes=int(spacing),
                min_publish_lead_minutes=int(lead), scheduler_enabled=enabled, upload_on_weekends=weekends,
                manual_approval=approval, catch_up_missed_runs=catch_up, max_retries=int(max_retries),
                default_language=language.strip(), default_category_id=category, default_topic=topic.strip(),
                contains_synthetic_media=synthetic, notify_subscribers=notify)
            db.log_event("schedule", "Scheduler settings updated from the dashboard")
            flash("Settings saved.")
            st.rerun()

    s = db.get_settings()
    now = utc_now()
    run = next_upload_run(now, s)
    with st.container(border=True):
        st.markdown("**Preview**")
        if run is None:
            st.write("The scheduler is disabled - only manual uploads will happen.")
        else:
            slot = compute_publish_slot(run, s, db.taken_publish_slots())
            st.write(f"Next automatic upload: **{format_local(run, s['timezone'])}**")
            st.write(f"That video would publish at: **{format_local(slot, s['timezone'])}** "
                     f"({slot.strftime('%Y-%m-%d %H:%M')} UTC)")
        st.caption("Publication is never scheduled in the past: if today's publication time is too close or "
                   "already passed, the next eligible day is used.")


def _hhmm(value: dtime) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"
