"""YouTube Settings page: OAuth connection, channel info and API connectivity test."""
from __future__ import annotations

import streamlit as st

from config import YOUTUBE_SCOPES
from youtube_auth import AuthError, YouTubeAuth, fetch_channel, validate_client_secret
from youtube_uploader import UploadError, create_uploader
from ui import fmt_local, flash, get_config, get_db, page_header, show_flash


def render() -> None:
    cfg = get_config()
    db = get_db()
    auth = YouTubeAuth(cfg)
    page_header("YouTube Settings", "Official Google OAuth 2.0 + YouTube Data API v3")
    show_flash()

    if cfg.is_mock:
        st.info("**Mock mode is active** (`UPLOAD_MODE=mock` in .env). Uploads are simulated locally and "
                "nothing is sent to YouTube. You can still connect your channel here; set `UPLOAD_MODE=live` "
                "and restart the worker and dashboard to upload for real.", icon="🧪")

    channel = db.get_active_channel()
    client_ok, client_msg = True, "Found"
    try:
        validate_client_secret(cfg.client_secret_file)
    except AuthError as exc:
        client_ok, client_msg = False, str(exc)

    c1, c2 = st.columns(2)
    with c1.container(border=True):
        st.markdown("**Connected channel**")
        if channel:
            st.markdown(f"### {channel['channel_name']}")
            st.caption(f"Channel ID: `{channel['channel_id']}`")
            st.caption(f"Connected: {fmt_local(channel['connected_at'])}")
            st.markdown(f"[Open YouTube Studio](https://studio.youtube.com/channel/{channel['channel_id']})")
        else:
            st.markdown("### Not connected")
    with c2.container(border=True):
        st.markdown("**OAuth status**")
        token_present = auth.has_token()
        st.markdown(f"Client file (`{cfg.client_secret_file.name}`): "
                    f"{':green-badge[found]' if client_ok else ':red-badge[missing]'}")
        st.markdown(f"Stored token: {':green-badge[present, encrypted]' if token_present else ':gray-badge[none]'}")
        if token_present:
            granted = set(auth.granted_scopes())
            missing = [s.rsplit('/', 1)[-1] for s in YOUTUBE_SCOPES if s not in granted]
            st.markdown("Scopes: " + (":green-badge[upload + readonly]" if not missing
                                       else f":orange-badge[missing {', '.join(missing)}]"))
        auth_error = db.get_state("auth_error")
        if auth_error:
            st.error(auth_error)
        st.caption("Tokens are never shown here. Your Google password is never seen or stored by this app.")

    if not client_ok:
        st.warning(client_msg)
        st.markdown("See **README → Google Cloud Console setup** for the step-by-step instructions.")

    b1, b2, b3 = st.columns(3)
    if b1.button("🔗 Connect YouTube account" if not channel else "🔄 Reconnect / switch account",
                 type="primary", disabled=not client_ok, use_container_width=True):
        with st.spinner("A Google sign-in page opened in your browser. Approve access there (5 minute timeout)..."):
            try:
                creds = auth.connect()
                info = fetch_channel(creds)
                previous = db.get_active_channel()
                db.save_channel(info.channel_id, info.title, info.uploads_playlist_id)
                db.set_state("auth_error", None)
                if previous and previous["channel_id"] != info.channel_id:
                    db.log_event("auth", f"Switched channel from {previous['channel_name']} to {info.title}", "WARNING")
                db.log_event("auth", f"Connected YouTube channel {info.title} ({info.channel_id})")
                flash(f"Connected to {info.title}.")
            except AuthError as exc:
                db.log_event("auth", f"Connect failed: {exc}", "ERROR")
                flash(str(exc), "error")
            except Exception as exc:
                db.log_event("auth", f"Connect failed: {type(exc).__name__}: {exc}", "ERROR")
                flash(f"Connection failed: {type(exc).__name__}: {exc}", "error")
        st.rerun()

    if b2.button("🧪 Test API connectivity", use_container_width=True, disabled=not (token_present or cfg.is_mock)):
        try:
            message = create_uploader(cfg, auth).test_connection()
            st.success(message)
            db.log_event("auth", f"Connectivity test passed: {message}")
        except (AuthError, UploadError) as exc:
            st.error(f"Test failed: {exc}")
            db.log_event("auth", f"Connectivity test failed: {exc}", "ERROR")
        except Exception as exc:
            st.error(f"Test failed: {type(exc).__name__}: {exc}")

    if channel or token_present:
        with b3.popover("Disconnect", use_container_width=True):
            st.write("Revokes the app's access at Google and deletes the local token. Scheduled videos on "
                     "YouTube are not affected.")
            if st.button("Disconnect now", type="primary"):
                auth.disconnect(revoke=True)
                db.deactivate_channels()
                db.log_event("auth", "YouTube account disconnected", "WARNING")
                flash("Disconnected.", "info")
                st.rerun()

    with st.expander("API quota and YouTube rules"):
        st.markdown(
            "- Each upload (`videos.insert`) costs about **1,600 quota units**; the default project quota is "
            "**10,000 units/day** (~6 uploads). Quota resets at midnight Pacific Time.\n"
            "- Videos are uploaded with `privacyStatus=private` and `status.publishAt` set to your publication "
            "time; YouTube makes them public automatically at that moment.\n"
            "- Shorts eligibility depends on the file (vertical or square, up to 3 minutes) - not on the "
            "`#Shorts` hashtag.\n"
            "- **Important:** API projects that have not passed Google's YouTube API compliance audit have "
            "uploads locked to *private*. Scheduled publishing only works after the audit, see README.\n"
            "- While the OAuth consent screen is in *Testing* status, Google expires refresh tokens after "
            "7 days; publish the app (status *In production*) to keep the connection."
        )
