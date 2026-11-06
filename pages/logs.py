"""Logs page: structured events from SQLite plus the raw worker log file."""
from __future__ import annotations

import streamlit as st

from ui import fmt_local, get_config, get_db, page_header

CATEGORY_LABELS = {
    "upload": "Uploads",
    "retry": "Retry attempts",
    "auth": "Authentication",
    "schedule": "Scheduling",
    "queue": "Queue / validation",
    "metadata": "Metadata",
    "recovery": "Recovery",
    "system": "System",
}


def render() -> None:
    cfg = get_config()
    db = get_db()
    page_header("Logs", "Uploads, failures, authentication, scheduling, retries and recovery")

    c1, c2, c3, c4 = st.columns([2, 2, 3, 1])
    categories = c1.multiselect("Category", list(CATEGORY_LABELS), format_func=CATEGORY_LABELS.get)
    levels = c2.multiselect("Level", ["INFO", "WARNING", "ERROR"])
    search = c3.text_input("Search messages")
    limit = c4.number_input("Rows", min_value=50, max_value=5000, value=300, step=50)

    tab_events, tab_jobs, tab_file = st.tabs(["Events", "Upload jobs", "Worker log file"])
    with tab_events:
        events = db.list_events(limit=int(limit), categories=categories or None, levels=levels or None,
                                search=search)
        errors = sum(1 for e in events if e["level"] == "ERROR")
        warnings = sum(1 for e in events if e["level"] == "WARNING")
        st.caption(f"{len(events)} events · {errors} errors · {warnings} warnings")
        st.dataframe(
            [{"Time": fmt_local(e["created_at"], fmt="%d %b %Y %I:%M:%S %p"), "Level": e["level"],
              "Category": CATEGORY_LABELS.get(e["category"], e["category"]), "Video": e["video_id"] or "",
              "Message": e["message"], "Details": e["details"] or ""} for e in events],
            hide_index=True, use_container_width=True, height=520,
        )
    with tab_jobs:
        jobs = db.list_jobs(int(limit))
        st.dataframe(
            [{"Job": j["id"], "Video": f"#{j['video_id']} {j['filename']}", "Status": j["job_status"],
              "Trigger": j["trigger"], "Started": fmt_local(j["started_at"]),
              "Finished": fmt_local(j["completed_at"]),
              "Progress": f"{100 * (j['bytes_uploaded'] or 0) // j['total_bytes']}%" if j["total_bytes"] else "-",
              "Error": j["error_message"] or ""} for j in jobs],
            hide_index=True, use_container_width=True, height=520,
        )
    with tab_file:
        log_file = cfg.logs_dir / "worker.log"
        if log_file.is_file():
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
            if search:
                lines = [line for line in lines if search.lower() in line.lower()]
            st.code("\n".join(lines[-400:]) or "(no matching lines)", language="log")
            st.caption(f"{log_file} (secrets are redacted before writing)")
        else:
            st.caption("No worker log yet - start the worker.")
