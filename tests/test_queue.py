import json
import shutil

import pytest

from conftest import needs_ffmpeg
from queue_manager import UnsafePathError, ensure_within, move_file, safe_filename
from video_processor import FileStabilityTracker, VideoInfo, evaluate, parse_probe


# --- pure validation rules -----------------------------------------------------------

def test_vertical_9_16_ok_without_warning():
    result = evaluate(VideoInfo(duration=30, width=1080, height=1920, has_audio=True), 180)
    assert result.ok and result.warnings == []


def test_square_accepted_with_preference_warning():
    result = evaluate(VideoInfo(duration=30, width=1080, height=1080, has_audio=True), 180)
    assert result.ok and any("9:16" in w for w in result.warnings)


def test_horizontal_rejected():
    result = evaluate(VideoInfo(duration=30, width=1920, height=1080, has_audio=True), 180)
    assert not result.ok and "Horizontal" in result.errors[0]


def test_rotation_metadata_is_respected():
    # Stored landscape but rotated 90 degrees = displayed vertical
    info = VideoInfo(duration=10, width=1920, height=1080, rotation=-90, has_audio=True)
    assert info.orientation == "vertical"
    assert evaluate(info, 180).ok


def test_too_long_rejected():
    result = evaluate(VideoInfo(duration=181, width=1080, height=1920, has_audio=True), 180)
    assert not result.ok and "exceeds" in result.errors[0]


def test_parse_probe_reads_rotation_side_data():
    data = {"format": {"duration": "12.5"},
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080, "codec_name": "h264",
                         "side_data_list": [{"rotation": 90}]}, {"codec_type": "audio"}]}
    info = parse_probe(data)
    assert info.duration == 12.5 and info.display_width == 1080 and info.has_audio


# --- path safety -------------------------------------------------------------------------

def test_safe_filename():
    assert safe_filename("my video?.mp4") == "my video_.mp4"
    for bad in ("../evil.mp4", "C:\\x\\a.mp4", "con.mp4", "", ".."):
        with pytest.raises(UnsafePathError):
            safe_filename(bad)


def test_ensure_within(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    ensure_within(base, base / "a.mp4")
    with pytest.raises(UnsafePathError):
        ensure_within(base, base / ".." / "outside.mp4")


def test_move_never_overwrites(tmp_path):
    src_dir, dest = tmp_path / "s", tmp_path / "d"
    src_dir.mkdir()
    dest.mkdir()
    (dest / "a.mp4").write_bytes(b"existing")
    (src_dir / "a.mp4").write_bytes(b"new")
    moved = move_file(src_dir / "a.mp4", dest)
    assert moved.name == "a_1.mp4"
    assert (dest / "a.mp4").read_bytes() == b"existing" and moved.read_bytes() == b"new"


def test_stability_tracker_waits_for_unchanged_file(tmp_path):
    clock = [0.0]
    tracker = FileStabilityTracker(10, clock=lambda: clock[0])
    f = tmp_path / "copying.mp4"
    f.write_bytes(b"x" * 100)
    assert tracker.is_ready(f) is False          # first sighting
    clock[0] = 5
    assert tracker.is_ready(f) is False          # not stable long enough
    with open(f, "ab") as fh:                    # still growing
        fh.write(b"y" * 100)
    clock[0] = 20
    assert tracker.is_ready(f) is False          # size changed -> timer restarts
    clock[0] = 31
    assert tracker.is_ready(f) is True


# --- folder scanning with real videos --------------------------------------------------

@needs_ffmpeg
def test_scan_registers_valid_video_with_metadata(stack, video_factory):
    stack.add_video(video_factory(), "hidden_places_in_kashmir.mp4")
    ids = stack.scan()
    assert len(ids) == 1
    video = stack.db.get_video(ids[0])
    assert video["upload_status"] == "queued"
    assert video["width"] == 360 and video["height"] == 640
    assert video["title"] == "Hidden Places In Kashmir"
    assert video["metadata_status"] == "template"


@needs_ffmpeg
def test_scan_ignores_partial_copies(stack, video_factory):
    shutil.copy(video_factory(), stack.cfg.incoming_dir / "big.mp4.partial")
    assert stack.scan() == []


@needs_ffmpeg
def test_duplicate_content_detected_by_hash(stack, video_factory):
    src = video_factory(variant=1)
    stack.add_video(src, "first.mp4")
    stack.scan()
    stack.add_video(src, "first_copy_renamed.mp4")
    ids = stack.scan()
    dup = stack.db.get_video(ids[0])
    assert dup["upload_status"] == "duplicate"
    assert (stack.cfg.failed_dir / "first_copy_renamed.mp4").exists()   # moved, not deleted
    assert len(stack.db.list_videos(["queued"])) == 1


@needs_ffmpeg
def test_invalid_videos_rejected_and_moved_to_failed(stack, video_factory):
    stack.add_video(video_factory(width=640, height=360, variant=2), "landscape.mp4")
    stack.add_video(video_factory(duration=181.5, variant=3), "too_long.mp4")
    (stack.cfg.incoming_dir / "garbage.mp4").write_bytes(b"this is not a video" * 100)
    ids = stack.scan()
    videos = {stack.db.get_video(i)["filename"]: stack.db.get_video(i) for i in ids}
    assert all(v["upload_status"] == "invalid" for v in videos.values())
    assert "Horizontal" in videos["landscape.mp4"]["error_message"]
    assert "exceeds" in videos["too_long.mp4"]["error_message"]
    assert "Not a readable video" in videos["garbage.mp4"]["error_message"]
    for name in videos:
        assert (stack.cfg.failed_dir / name).exists()


@needs_ffmpeg
def test_truncated_file_fails_integrity(stack, video_factory):
    data = video_factory(variant=4).read_bytes()
    (stack.cfg.incoming_dir / "truncated.mp4").write_bytes(data[: len(data) // 2])
    ids = stack.scan()
    assert stack.db.get_video(ids[0])["upload_status"] == "invalid"


@needs_ffmpeg
def test_sidecar_override_applied(stack, video_factory):
    (stack.cfg.metadata_dir / "kashmir.json").write_text(json.dumps({
        "title": "Amazing Hidden Places in Kashmir", "description": "Explore the breathtaking beauty of Kashmir.",
        "tags": ["Kashmir", "Travel", "Nature"], "hashtags": ["#Kashmir", "#Travel", "#Shorts"], "language": "en"}),
        encoding="utf-8")
    stack.add_video(video_factory(variant=5), "kashmir.mp4")
    video = stack.db.get_video(stack.scan()[0])
    assert video["title"] == "Amazing Hidden Places in Kashmir" and video["metadata_status"] == "override"


@needs_ffmpeg
def test_manual_approval_blocks_until_approved(stack, video_factory):
    stack.db.update_settings(manual_approval=True)
    stack.add_video(video_factory(variant=6), "needs_ok.mp4")
    vid = stack.scan()[0]
    assert stack.db.get_video(vid)["upload_status"] == "pending_approval"
    assert stack.queue.eligible_videos() == []
    stack.metadata.approve(vid)
    assert [v["id"] for v in stack.queue.eligible_videos()] == [vid]


@needs_ffmpeg
def test_oldest_video_selected_first(stack, video_factory):
    import os
    import time
    first = stack.add_video(video_factory(variant=7), "older.mp4")
    os.utime(first, (time.time() - 3600, time.time() - 3600))
    stack.add_video(video_factory(variant=8), "newer.mp4")
    stack.scan()
    assert stack.queue.next_eligible()["filename"] == "older.mp4"


@needs_ffmpeg
def test_remove_and_restore(stack, video_factory):
    stack.add_video(video_factory(variant=9), "x.mp4")
    vid = stack.scan()[0]
    stack.queue.remove_from_queue(vid)
    assert stack.db.get_video(vid)["upload_status"] == "skipped"
    assert (stack.cfg.incoming_dir / "x.mp4").exists()   # file kept
    assert stack.queue.eligible_videos() == []
    stack.queue.restore_to_queue(vid)
    assert stack.db.get_video(vid)["upload_status"] == "queued"


def test_dashboard_upload_rejects_bad_names(stack):
    with pytest.raises(UnsafePathError):
        stack.queue.save_uploaded_file("../../evil.mp4", b"x")
    with pytest.raises(UnsafePathError):
        stack.queue.save_uploaded_file("script.exe", b"x")
    saved = stack.queue.save_uploaded_file("ok clip.mp4", b"data")
    assert saved.parent == stack.cfg.incoming_dir.resolve() or saved.parent == stack.cfg.incoming_dir
