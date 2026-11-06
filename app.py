"""Streamlit dashboard entry point: ``streamlit run app.py``.

The dashboard only reads/writes SQLite and requests actions; uploads are
performed by the independent worker (``scheduler.py``), so closing the browser
or stopping the dashboard never interrupts scheduled uploads.
"""
from __future__ import annotations

import hmac
import time

import streamlit as st

from logging_setup import setup_logging
from pages import history, logs, overview, queue, settings, youtube_settings
from ui import get_config, get_db, worker_status

st.set_page_config(page_title="Shorts Autopilot", page_icon="🎬", layout="wide",
                   initial_sidebar_state="expanded")

cfg = get_config()
setup_logging(cfg.logs_dir, "dashboard", cfg.log_level, console=False)


def password_gate() -> bool:
    expected = cfg.dashboard_password
    if not expected:
        st.session_state["authenticated"] = True
        return True
    if st.session_state.get("authenticated"):
        return True
    st.title("🎬 Shorts Autopilot")
    with st.form("login", border=True):
        st.write("Enter the dashboard password (DASHBOARD_PASSWORD in .env).")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Unlock", type="primary")
    if submitted:
        failures = st.session_state.get("login_failures", 0)
        if failures >= 3:
            time.sleep(min(10, failures))  # slow down guessing
        if hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
            st.session_state["authenticated"] = True
            st.session_state["login_failures"] = 0
            st.rerun()
        else:
            st.session_state["login_failures"] = failures + 1
            st.error("Incorrect password")
    return False


if password_gate():
    db = get_db()
    nav = st.navigation({
        "Dashboard": [
            st.Page(overview.render, title="Overview", icon=":material/space_dashboard:", url_path="overview",
                    default=True),
            st.Page(queue.render, title="Video Queue", icon=":material/video_library:", url_path="queue"),
            st.Page(history.render, title="Upload History", icon=":material/history:", url_path="history"),
            st.Page(logs.render, title="Logs", icon=":material/receipt_long:", url_path="logs"),
        ],
        "Configuration": [
            st.Page(settings.render, title="Scheduler Settings", icon=":material/schedule:", url_path="scheduler"),
            st.Page(youtube_settings.render, title="YouTube Settings", icon=":material/smart_display:",
                    url_path="youtube"),
        ],
    })
    with st.sidebar:
        online, text = worker_status()
        st.markdown(f"{':green-badge[● Worker online]' if online else ':red-badge[● Worker offline]'}")
        st.caption(text)
        if cfg.is_mock:
            st.markdown(":orange-badge[MOCK MODE] nothing is sent to YouTube")
        else:
            st.markdown(":green-badge[LIVE MODE] uploads go to YouTube")
        channel = db.get_active_channel()
        st.caption(f"Channel: {channel['channel_name']}" if channel else "Channel: not connected")
        if not cfg.dashboard_password:
            st.warning("No DASHBOARD_PASSWORD set. The dashboard is only bound to this PC (127.0.0.1); "
                       "set a password in .env to protect it.", icon="🔓")
        elif st.button("Lock dashboard", use_container_width=True):
            st.session_state["authenticated"] = False
            st.rerun()
    nav.run()
