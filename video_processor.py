"""Video inspection: copy-completion detection, hashing and Shorts validation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

NINE_SIXTEEN = 9 / 16
ASPECT_TOLERANCE = 0.02
YOUTUBE_MAX_FILE_BYTES = 256 * 1024 ** 3  # API limit: 256 GB
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProbeError(Exception):
    pass


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    rotation: int = 0
    codec: str = ""
    has_audio: bool = False
    sample_aspect_ratio: float = 1.0
    container: str = ""

    @property
    def display_width(self) -> int:
        w = round(self.width * self.sample_aspect_ratio)
        return self.height if abs(self.rotation) % 180 == 90 else w

    @property
    def display_height(self) -> int:
        w = round(self.width * self.sample_aspect_ratio)
        return w if abs(self.rotation) % 180 == 90 else self.height

    @property
    def aspect_ratio(self) -> float:
        return self.display_width / self.display_height if self.display_height else 0.0

    @property
    def orientation(self) -> str:
        if self.display_width == self.display_height:
            return "square"
        return "vertical" if self.display_height > self.display_width else "horizontal"


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: VideoInfo | None = None

    @property
    def summary(self) -> str:
        return "; ".join(self.errors or self.warnings) or "OK"


# --- file helpers -------------------------------------------------------------

def file_sha256(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def is_file_locked(path: Path) -> bool:
    """True while another process (e.g. Explorer copying) holds the file for writing."""
    try:
        with open(path, "rb"):
            pass
    except PermissionError:
        return True
    except FileNotFoundError:
        return True
    if not os.access(path, os.W_OK):
        return False  # read-only file: can't probe the write lock, rely on size stability
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return True


class FileStabilityTracker:
    """Reports a file as ready only after its size and mtime stop changing.

    A file must be observed unchanged for ``stable_seconds`` across at least two
    scans, and must not be locked by the writing process.
    """

    def __init__(self, stable_seconds: float, clock=time.monotonic):
        self.stable_seconds = stable_seconds
        self.clock = clock
        self._seen: dict[str, tuple[int, float, float]] = {}

    def is_ready(self, path: Path) -> bool:
        key = str(path)
        try:
            st = path.stat()
        except FileNotFoundError:
            self._seen.pop(key, None)
            return False
        signature = (st.st_size, st.st_mtime)
        now = self.clock()
        previous = self._seen.get(key)
        if previous is None or (previous[0], previous[1]) != signature:
            self._seen[key] = (st.st_size, st.st_mtime, now)
            return False
        if st.st_size == 0 or now - previous[2] < self.stable_seconds:
            return False
        return not is_file_locked(path)

    def forget(self, path: Path) -> None:
        self._seen.pop(str(path), None)


# --- ffprobe ------------------------------------------------------------------

def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise ProbeError(f"'{cmd[0]}' was not found. Install FFmpeg and add it to PATH "
                         f"(or set FFPROBE_PATH / FFMPEG_PATH in .env).") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"{Path(cmd[0]).name} timed out after {timeout}s") from exc


def probe(path: Path, ffprobe: str = "ffprobe") -> dict:
    result = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], 120)
    if result.returncode != 0:
        raise ProbeError((result.stderr or "ffprobe could not read the file").strip().splitlines()[-1])
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe returned malformed output") from exc


def _parse_ratio(value: str | None) -> float:
    if not value or ":" not in value:
        return 1.0
    num, den = value.split(":", 1)
    try:
        n, d = float(num), float(den)
    except ValueError:
        return 1.0
    return n / d if n > 0 and d > 0 else 1.0


def parse_probe(data: dict) -> VideoInfo:
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise ProbeError("No video stream found")
    duration = None
    for candidate in ((data.get("format") or {}).get("duration"), video.get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if duration is None:
        raise ProbeError("Could not determine the video duration")

    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(float(side["rotation"]))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = int((video.get("tags") or {}).get("rotate", 0))
        except (TypeError, ValueError):
            rotation = 0

    return VideoInfo(
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        rotation=rotation,
        codec=video.get("codec_name", ""),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        sample_aspect_ratio=_parse_ratio(video.get("sample_aspect_ratio")),
        container=(data.get("format") or {}).get("format_name", ""),
    )


def evaluate(info: VideoInfo, max_duration: float) -> ValidationResult:
    """Apply YouTube Shorts format rules to probed video information."""
    errors: list[str] = []
    warnings: list[str] = []
    if info.width <= 0 or info.height <= 0:
        errors.append("Video has no valid resolution")
    if info.duration <= 0:
        errors.append("Video duration is zero")
    elif info.duration > max_duration + 0.05:
        errors.append(f"Duration {info.duration:.1f}s exceeds the Shorts limit of {max_duration:.0f}s")
    if info.width > 0 and info.height > 0:
        if info.orientation == "horizontal":
            errors.append(f"Horizontal video ({info.display_width}x{info.display_height}); "
                          "Shorts must be vertical or square")
        elif abs(info.aspect_ratio - NINE_SIXTEEN) > ASPECT_TOLERANCE:
            warnings.append(f"Aspect ratio {info.display_width}x{info.display_height} is accepted but "
                            "9:16 (e.g. 1080x1920) is preferred")
        if min(info.display_width, info.display_height) < 720:
            warnings.append("Resolution is below 720p; consider exporting at 1080x1920")
    if not info.has_audio:
        warnings.append("Video has no audio track")
    return ValidationResult(ok=not errors, errors=errors, warnings=warnings, info=info)


def check_integrity(path: Path, ffmpeg: str = "ffmpeg") -> str | None:
    """Decode the start and the last seconds of the file. Returns an error string or None."""
    commands = (
        [ffmpeg, "-v", "error", "-i", str(path), "-t", "2", "-map", "0:v:0", "-f", "null", "-"],
        [ffmpeg, "-v", "error", "-sseof", "-2", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
    )
    for cmd in commands:
        result = _run(cmd, 300)
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            return "File appears corrupt or truncated: " + (detail[-1] if detail else "decode failed")
    return None


def validate_video(path: Path, max_duration: float, allowed_extensions: tuple[str, ...],
                   ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg") -> ValidationResult:
    if path.suffix.lower() not in allowed_extensions:
        return ValidationResult(False, [f"Unsupported file type '{path.suffix}'. Allowed: {', '.join(allowed_extensions)}"])
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return ValidationResult(False, ["File not found"])
    if size == 0:
        return ValidationResult(False, ["File is empty"])
    if size > YOUTUBE_MAX_FILE_BYTES:
        return ValidationResult(False, ["File exceeds YouTube's 256 GB upload limit"])
    try:
        info = parse_probe(probe(path, ffprobe))
    except ProbeError as exc:
        return ValidationResult(False, [f"Not a readable video: {exc}"])
    result = evaluate(info, max_duration)
    if result.ok:
        integrity_error = check_integrity(path, ffmpeg)
        if integrity_error:
            result.ok = False
            result.errors.append(integrity_error)
    if path.suffix.lower() != ".mp4":
        result.warnings.append("MP4 (H.264/AAC) is the recommended format")
    return result
