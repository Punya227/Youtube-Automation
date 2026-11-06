"""Application configuration.

Static configuration (paths, secrets, upload mode) comes from environment
variables / the local ``.env`` file. Runtime-tunable settings (upload time,
publish time, timezone, daily limit, ...) live in SQLite, see ``database.py``.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=False)

# YouTube treats square or vertical uploads of up to 3 minutes as Shorts
# (rule in effect since 15 Oct 2024). Kept configurable in case YouTube changes it.
DEFAULT_SHORTS_MAX_SECONDS = 180

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
# Read-only scope is used to show the connected channel, to verify that YouTube
# accepted the scheduled publish time, and to reconcile interrupted uploads.
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_SCOPES = [YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE]

FOLDER_NAMES = ("incoming", "processing", "uploaded", "failed", "logs", "thumbnails", "metadata")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _resolve(path_value: str | None, default: Path) -> Path:
    if not path_value:
        return default
    path = Path(path_value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _find_binary(env_name: str, binary: str) -> str:
    configured = os.getenv(env_name)
    if configured:
        return configured
    return shutil.which(binary) or binary


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    secrets_dir: Path
    client_secret_file: Path
    token_file: Path
    upload_mode: str = "mock"  # "mock" (never touches YouTube) or "live"
    dashboard_password: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    shorts_max_duration: float = DEFAULT_SHORTS_MAX_SECONDS
    allowed_extensions: tuple[str, ...] = (".mp4", ".mov", ".m4v")
    file_stable_seconds: int = 10
    scan_interval_seconds: int = 15
    ffprobe_path: str = "ffprobe"
    ffmpeg_path: str = "ffmpeg"
    upload_chunk_mb: int = 8
    max_chunk_retries: int = 8
    log_level: str = "INFO"
    mock_fail_mode: str = ""
    extra: dict = field(default_factory=dict)

    # --- derived paths ---------------------------------------------------
    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"

    @property
    def processing_dir(self) -> Path:
        return self.data_dir / "processing"

    @property
    def uploaded_dir(self) -> Path:
        return self.data_dir / "uploaded"

    @property
    def failed_dir(self) -> Path:
        return self.data_dir / "failed"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def metadata_dir(self) -> Path:
        return self.data_dir / "metadata"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shorts.db"

    @property
    def mock_store_path(self) -> Path:
        return self.data_dir / "mock_youtube.json"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "worker.lock"

    @property
    def stop_file(self) -> Path:
        return self.data_dir / "worker.stop"

    @property
    def is_mock(self) -> bool:
        return self.upload_mode != "live"

    @property
    def anthropic_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    def with_overrides(self, **kwargs) -> "AppConfig":
        return replace(self, **kwargs)


def load_config() -> AppConfig:
    data_dir = _resolve(os.getenv("YSA_DATA_DIR"), PROJECT_ROOT / "youtube_shorts")
    secrets_dir = _resolve(os.getenv("YSA_SECRETS_DIR"), PROJECT_ROOT / "secrets")
    mode = (os.getenv("UPLOAD_MODE") or "mock").strip().lower()
    if mode not in {"mock", "live"}:
        mode = "mock"
    extensions = tuple(
        ext if ext.startswith(".") else f".{ext}"
        for ext in (e.strip().lower() for e in (os.getenv("ALLOWED_EXTENSIONS") or ".mp4,.mov,.m4v").split(","))
        if ext
    )
    return AppConfig(
        data_dir=data_dir,
        secrets_dir=secrets_dir,
        client_secret_file=_resolve(os.getenv("GOOGLE_CLIENT_SECRET_FILE"), secrets_dir / "client_secret.json"),
        token_file=_resolve(os.getenv("YOUTUBE_TOKEN_FILE"), secrets_dir / "youtube_token.bin"),
        upload_mode=mode,
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL") or "claude-opus-5-5",
        shorts_max_duration=_env_float("SHORTS_MAX_DURATION_SECONDS", DEFAULT_SHORTS_MAX_SECONDS),
        allowed_extensions=extensions or (".mp4",),
        file_stable_seconds=_env_int("FILE_STABLE_SECONDS", 10),
        scan_interval_seconds=max(5, _env_int("SCAN_INTERVAL_SECONDS", 15)),
        ffprobe_path=_find_binary("FFPROBE_PATH", "ffprobe"),
        ffmpeg_path=_find_binary("FFMPEG_PATH", "ffmpeg"),
        upload_chunk_mb=max(1, _env_int("UPLOAD_CHUNK_MB", 8)),
        max_chunk_retries=max(1, _env_int("MAX_CHUNK_RETRIES", 8)),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
        mock_fail_mode=(os.getenv("MOCK_FAIL_MODE") or "").strip().lower(),
    )


def ensure_directories(cfg: AppConfig) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    for name in FOLDER_NAMES:
        (cfg.data_dir / name).mkdir(parents=True, exist_ok=True)
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
