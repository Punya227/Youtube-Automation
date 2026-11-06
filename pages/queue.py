"""Video Queue page: incoming videos, metadata editing/approval and manual actions."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from metadata_generator import CATEGORIES
from queue_manager import UnsafePathError, ensure_within
from ui import (fmt_duration, fmt_local, fmt_size, flash, get_config, get_db, get_metadata_service, get_queue,
                page_header, show_flash, status_badge, status_label, worker_status)

QUEUE_STATUSES = ["queued", "pending_approval", "retry_wait", "uploading", "skipped"]


def render() -> None:
    cfg = get_config()
    db = get_db()
    queue = get_queue()
    settings = db.get_settings()
    page_header("Video Queue", f"Drop MP4 files into {cfg.incoming_dir} - the worker picks them up once "
                               "copying has finished.")
    show_flash()

    with st.expander("➕ Add videos from this browser", expanded=False):
        files = st.file_uploader("Vertical or square MP4 videos", type=[e.lstrip(".") for e in cfg.allowed_extensions],
                                 accept_multiple_files=True)
        if files and st.button("Add to incoming folder", type="primary"):
            for f in files:
                try:
                    saved = queue.save_uploaded_file(f.name, f.getvalue())
                    st.success(f"Saved {saved.name}")
                except UnsafePathError as exc:
                    st.error(f"{f.name}: {exc}")
            st.caption("The worker will validate and queue them within a few seconds.")

    pending_files = queue.unregistered_files()
    if pending_files:
        online, _ = worker_status()
        st.info(f"{len(pending_files)} new file(s) detected in incoming/ "
                f"({', '.join(p.name for p in pending_files[:5])}{'…' if len(pending_files) > 5 else ''}). "
                + ("They will be validated once copying has finished." if online
                   else "Start the worker to validate and queue them."), icon="📥")

    videos = db.list_videos(QUEUE_STATUSES)
    if not videos:
        st.caption("The queue is empty.")
        return

    rows = []
    for v in videos:
        job = db.latest_job_for_video(v["id"]) if v["upload_status"] == "uploading" else None
        progress = f" {100 * (job['bytes_uploaded'] or 0) // job['total_bytes']}%" if job and job["total_bytes"] else ""
        rows.append({
            "ID": v["id"],
            "Filename": v["filename"],
            "Duration": fmt_duration(v["duration_seconds"]),
            "Size": fmt_size(v["file_size"]),
            "Resolution": f"{v['width']}x{v['height']}" if v["width"] else "-",
            "Status": status_label(v["upload_status"]) + progress
                      + (" · upload requested" if v["manual_request"] else ""),
            "Scheduled": fmt_local(v["scheduled_publish_at"]) if v["scheduled_publish_at"]
                         else (f"retry {fmt_local(v['next_retry_at'])}" if v["next_retry_at"] else "-"),
            "Metadata": v["metadata_status"],
            "Title": v["title"] or "",
            "Added": fmt_local(v["created_at"]),
        })
    st.dataframe(rows, hide_index=True, use_container_width=True)

    options = {v["id"]: f"#{v['id']} · {v['filename']} · {status_label(v['upload_status'])}" for v in videos}
    selected_id = st.selectbox("Select a video", list(options), format_func=options.get)
    video = db.get_video(selected_id)
    if video is None:
        return
    _render_detail(video, settings)


def _render_detail(video: dict, settings: dict) -> None:
    cfg = get_config()
    db = get_db()
    queue = get_queue()
    metadata = get_metadata_service()

    st.markdown(f"### {video['filename']} {status_badge(video['upload_status'])}")
    if video["error_message"]:
        st.warning(video["error_message"])
    if video["validation_warnings"]:
        st.caption(f"⚠️ {video['validation_warnings']}")

    preview_col, edit_col = st.columns([1, 2], gap="large")
    with preview_col:
        try:
            path = ensure_within(cfg.data_dir, Path(video["file_path"]))
            if path.exists():
                st.video(str(path))
            else:
                st.caption("File not found on disk.")
        except UnsafePathError:
            st.error("File path is outside the managed folders; preview disabled.")
        st.caption(f"{fmt_duration(video['duration_seconds'])} · {video['width']}x{video['height']} · "
                   f"{fmt_size(video['file_size'])}")

        waiting = video["upload_status"] in ("queued", "pending_approval", "retry_wait")
        st.markdown("**Actions**")
        override = st.checkbox("Ignore today's daily limit", key=f"override_{video['id']}",
                               disabled=not waiting)
        if st.button("⬆️ Upload now", type="primary", disabled=not waiting, use_container_width=True,
                     key=f"up_{video['id']}"):
            try:
                queue.request_upload_now(video["id"], override_limit=override)
                online, _ = worker_status()
                flash("Upload requested - the worker will start it within a few seconds." if online else
                      "Upload requested, but the worker is offline. Start it with start.bat.",
                      "success" if online else "warning")
            except ValueError as exc:
                flash(str(exc), "error")
            st.rerun()
        if waiting and st.button("🗑️ Remove from queue", use_container_width=True, key=f"rm_{video['id']}"):
            queue.remove_from_queue(video["id"])
            flash("Removed from the queue. The file stays in incoming/.")
            st.rerun()
        if video["upload_status"] == "skipped" and st.button("↩️ Restore to queue", use_container_width=True):
            queue.restore_to_queue(video["id"])
            flash("Restored to the queue.")
            st.rerun()

    with edit_col:
        editable = video["upload_status"] in ("queued", "pending_approval", "retry_wait", "skipped")
        st.markdown(f"**Metadata** · source: `{video['metadata_source'] or '-'}` · status: "
                    f"`{video['metadata_status']}`")
        if settings["manual_approval"] and video["metadata_status"] != "approved":
            st.info("Manual approval mode is on: review and approve this metadata before it can be uploaded.")
        topic = st.text_input("Topic (optional, used for AI generation)", value=video["topic"] or "",
                              disabled=not editable, key=f"topic_{video['id']}")
        if st.button("✨ Regenerate metadata", disabled=not editable, key=f"regen_{video['id']}"):
            with st.spinner("Generating..."):
                metadata.ensure_metadata(video["id"], force=True, topic=topic)
            flash("Metadata regenerated." + ("" if cfg.anthropic_enabled else
                                             " (local template - set ANTHROPIC_API_KEY for AI generation)"))
            st.rerun()

        with st.form(f"meta_{video['id']}", border=True):
            title = st.text_input("Title", value=video["title"] or "", max_chars=100, disabled=not editable)
            description = st.text_area("Description", value=video["description"] or "", height=140,
                                       disabled=not editable)
            tags = st.text_input("Tags (comma separated)", value=", ".join(video["tags"]), disabled=not editable)
            hashtags = st.text_input("Hashtags (space separated)", value=" ".join(video["hashtags"]),
                                     disabled=not editable)
            c1, c2 = st.columns(2)
            cat_ids = list(CATEGORIES)
            current_cat = video["category_id"] if video["category_id"] in CATEGORIES else settings["default_category_id"]
            category = c1.selectbox("Category", cat_ids, index=cat_ids.index(current_cat),
                                    format_func=lambda c: f"{CATEGORIES[c]} ({c})", disabled=not editable)
            language = c2.text_input("Language (ISO code)", value=video["language"] or settings["default_language"],
                                     disabled=not editable)
            synthetic = st.checkbox("Contains realistic altered or synthetic (AI-generated) content",
                                    value=bool(video["contains_synthetic_media"]), disabled=not editable,
                                    help="Sets YouTube's altered/synthetic content disclosure.")
            b1, b2 = st.columns(2)
            save = b1.form_submit_button("💾 Save", disabled=not editable, use_container_width=True)
            approve = b2.form_submit_button("✅ Save & approve", type="primary", disabled=not editable,
                                            use_container_width=True)
        if save or approve:
            if not title.strip():
                st.error("Title is required")
            else:
                metadata.save_edit(
                    video["id"], title=title, description=description,
                    tags=[t.strip() for t in tags.split(",") if t.strip()],
                    hashtags=[h for h in hashtags.replace(",", " ").split() if h.strip()],
                    category_id=category, language=language.strip(), contains_synthetic_media=synthetic,
                    approve=approve)
                flash("Metadata approved." if approve else "Metadata saved.")
                st.rerun()
