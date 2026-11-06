from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from config import AppConfig, ensure_directories
from database import Database
from metadata_generator import MetadataGenerator
from queue_manager import QueueManager
from recovery import RecoveryManager
from services.metadata_service import MetadataService
from services.upload_service import UploadService
from youtube_uploader import MockUploader

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="FFmpeg/FFprobe not installed")

_COLORS = ["red", "green", "blue", "yellow", "purple", "orange", "cyan", "white", "gray", "pink"]


@pytest.fixture(scope="session")
def video_factory(tmp_path_factory):
    """Create tiny real H.264 videos with FFmpeg (cached per parameter set)."""
    cache_dir = tmp_path_factory.mktemp("videos")
    cache: dict[tuple, Path] = {}

    def make(width=360, height=640, duration=2.0, variant=0) -> Path:
        key = (width, height, duration, variant)
        if key not in cache:
            out = cache_dir / f"v_{width}x{height}_{duration}_{variant}.mp4"
            color = _COLORS[variant % len(_COLORS)]
            subprocess.run(
                [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:d={duration}:r=10",
                 "-f", "lavfi", "-i", f"sine=frequency={300 + variant * 50}:duration={duration}",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
                check=True, capture_output=True)
            cache[key] = out
        return cache[key]

    return make


@pytest.fixture
def cfg(tmp_path) -> AppConfig:
    config = AppConfig(
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        client_secret_file=tmp_path / "secrets" / "client_secret.json",
        token_file=tmp_path / "secrets" / "token.bin",
        upload_mode="mock",
        file_stable_seconds=0,
        ffprobe_path=FFPROBE or "ffprobe",
        ffmpeg_path=FFMPEG or "ffmpeg",
    )
    ensure_directories(config)
    return config


@pytest.fixture
def db(cfg) -> Database:
    database = Database(cfg.db_path)
    yield database
    database.close()


class Stack:
    """All services wired together with a mock uploader (never touches YouTube)."""

    def __init__(self, cfg: AppConfig, db: Database, fail_mode: str = ""):
        self.cfg = cfg
        self.db = db
        self.stop_event = threading.Event()
        self.metadata = MetadataService(cfg, db, MetadataGenerator())  # template only, no network
        self.queue = QueueManager(cfg, db, self.metadata)
        self.uploader = MockUploader(cfg.mock_store_path, fail_mode=fail_mode, chunk_size=64 * 1024)
        self.uploads = UploadService(cfg, db, self.uploader, self.queue, self.metadata, self.stop_event)
        self.recovery = RecoveryManager(db, self.uploader, self.queue, self.uploads)

    def add_video(self, source: Path, name: str) -> Path:
        target = self.cfg.incoming_dir / name
        shutil.copy(source, target)
        return target

    def scan(self) -> list[int]:
        self.queue.scan_incoming()  # first observation
        return self.queue.scan_incoming()  # stable now (file_stable_seconds=0)


@pytest.fixture
def stack(cfg, db) -> Stack:
    return Stack(cfg, db)


@pytest.fixture
def make_stack(cfg, db):
    def factory(fail_mode: str = "") -> Stack:
        return Stack(cfg, db, fail_mode)
    return factory
