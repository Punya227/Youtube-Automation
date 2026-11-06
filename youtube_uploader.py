"""YouTube uploading: resumable videos.insert with scheduled publication.

``LiveUploader`` talks to the official YouTube Data API v3. ``MockUploader``
implements the same interface against a local JSON file so the whole workflow
(including crash recovery) can be exercised without touching YouTube.

Neither class touches SQLite; persistence is the job of ``services.upload_service``.
"""
from __future__ import annotations

import http.client
import json
import logging
import random
import socket
import ssl
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from metadata_generator import compose_description

log = logging.getLogger("ysa.uploader")

RETRIABLE_STATUS = {500, 502, 503, 504}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
AUTH_REASONS = {"authError", "forbidden", "insufficientPermissions", "youtubeSignupRequired",
                "channelNotFound", "accessNotConfigured"}


# --- errors -------------------------------------------------------------------------

class UploadError(Exception):
    kind = "error"
    retryable = False


class TransientUploadError(UploadError):
    kind = "transient"
    retryable = True


class PermanentUploadError(UploadError):
    kind = "permanent"


class AuthUploadError(UploadError):
    kind = "auth"


class QuotaExceededError(UploadError):
    kind = "quota"
    retryable = True


class UploadInterrupted(UploadError):
    """Shutdown requested mid-upload; the resumable session is kept for recovery."""
    kind = "interrupted"


class SimulatedCrash(BaseException):
    """Mock-only: emulates the process dying right after YouTube accepted the upload."""


# --- data types ----------------------------------------------------------------------

def to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass
class UploadRequest:
    file_path: Path
    title: str
    description: str
    tags: list[str]
    hashtags: list[str]
    category_id: str
    language: str
    publish_at: datetime  # aware, future
    made_for_kids: bool = False
    contains_synthetic_media: bool = False
    audio_language: str | None = None
    notify_subscribers: bool = True

    def body(self) -> dict:
        snippet = {
            "title": self.title,
            "description": compose_description(self.description, self.hashtags),
            "tags": self.tags,
            "categoryId": self.category_id,
            "defaultLanguage": self.language,
        }
        if self.audio_language:
            snippet["defaultAudioLanguage"] = self.audio_language
        status = {
            "privacyStatus": "private",           # required for publishAt scheduling
            "publishAt": to_rfc3339(self.publish_at),
            "selfDeclaredMadeForKids": self.made_for_kids,
            "embeddable": True,
            "license": "youtube",
        }
        if self.contains_synthetic_media:
            status["containsSyntheticMedia"] = True
        return {"snippet": snippet, "status": status}


@dataclass
class UploadResult:
    video_id: str
    privacy_status: str
    publish_at: datetime | None
    title: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"https://youtube.com/shorts/{self.video_id}"

    def schedule_confirmed(self, expected: datetime) -> bool:
        return (self.privacy_status == "private" and self.publish_at is not None
                and abs((self.publish_at - expected.astimezone(timezone.utc)).total_seconds()) < 1)

    @classmethod
    def from_resource(cls, resource: dict) -> "UploadResult":
        status = resource.get("status") or {}
        return cls(
            video_id=resource["id"],
            privacy_status=status.get("privacyStatus", ""),
            publish_at=parse_rfc3339(status.get("publishAt")),
            title=(resource.get("snippet") or {}).get("title", ""),
            raw=resource,
        )


@dataclass
class SessionStatus:
    state: str  # "complete" | "incomplete" | "expired"
    offset: int = 0
    result: UploadResult | None = None


ProgressCallback = Callable[[int, int], None]
SessionCallback = Callable[[str, int], None]


class BaseUploader(ABC):
    name = "base"

    @abstractmethod
    def upload(self, request: UploadRequest, progress: ProgressCallback | None = None,
               on_session: SessionCallback | None = None, stop_event: threading.Event | None = None,
               resume: tuple[str, int] | None = None) -> UploadResult: ...

    @abstractmethod
    def query_session(self, resumable_uri: str, total_bytes: int) -> SessionStatus: ...

    @abstractmethod
    def find_recent_upload(self, title: str, since: datetime) -> UploadResult | None: ...

    @abstractmethod
    def get_videos(self, video_ids: list[str]) -> dict[str, UploadResult]: ...

    @abstractmethod
    def set_publish_at(self, video_id: str, publish_at: datetime) -> UploadResult: ...

    @abstractmethod
    def test_connection(self) -> str: ...


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 300.0) -> float:
    return min(cap, base * (2 ** (attempt - 1))) * random.uniform(0.75, 1.25)


def _sleep(seconds: float, stop_event: threading.Event | None) -> None:
    if stop_event is not None:
        if stop_event.wait(seconds):
            raise UploadInterrupted("Shutdown requested while waiting to retry")
    else:
        time.sleep(seconds)


# --- live uploader --------------------------------------------------------------------

def classify_http_error(exc) -> UploadError:
    """Map a googleapiclient HttpError to our error taxonomy."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason, message = "", str(exc)
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        error = payload.get("error", {})
        message = error.get("message", message)
        details = error.get("errors") or []
        if details:
            reason = details[0].get("reason", "")
    except Exception:
        pass
    text = f"HTTP {status} {reason}: {message}".strip()
    if status in RETRIABLE_STATUS or reason in RATE_REASONS or status == 429:
        return TransientUploadError(text)
    if reason in QUOTA_REASONS:
        return QuotaExceededError(text)
    if status == 401 or reason in AUTH_REASONS:
        return AuthUploadError(text)
    return PermanentUploadError(text)


class LiveUploader(BaseUploader):
    name = "live"

    def __init__(self, credentials_provider: Callable[[], object], chunk_mb: int = 8, max_chunk_retries: int = 8):
        self._credentials_provider = credentials_provider
        self.chunk_size = chunk_mb * 1024 * 1024
        self.max_chunk_retries = max_chunk_retries

    def _service(self):
        from google.auth.exceptions import TransportError

        from youtube_auth import AuthError, build_youtube_service
        try:
            creds = self._credentials_provider()
        except AuthError as exc:
            raise AuthUploadError(str(exc)) from exc
        except TransportError as exc:
            raise TransientUploadError(f"Network error refreshing token: {exc}") from exc
        return build_youtube_service(creds), creds

    def _execute(self, request_fn):
        from googleapiclient.errors import HttpError
        try:
            return request_fn().execute(num_retries=3)
        except HttpError as exc:
            raise classify_http_error(exc) from exc
        except (OSError, http.client.HTTPException, socket.timeout, ssl.SSLError) as exc:
            raise TransientUploadError(f"Network error: {exc}") from exc

    def upload(self, request, progress=None, on_session=None, stop_event=None, resume=None):
        import httplib2
        from google.auth.exceptions import RefreshError, TransportError
        from googleapiclient.errors import HttpError, ResumableUploadError
        from googleapiclient.http import MediaFileUpload

        youtube, _ = self._service()
        media = MediaFileUpload(str(request.file_path), mimetype="video/*", chunksize=self.chunk_size, resumable=True)
        insert = youtube.videos().insert(
            part="snippet,status", body=request.body(), media_body=media,
            notifySubscribers=request.notify_subscribers,
        )
        if resume:
            insert.resumable_uri, insert.resumable_progress = resume[0], resume[1]
            log.info("Resuming upload session at byte %s", resume[1])

        response = None
        attempt = 0
        reported_uri = None
        while response is None:
            if stop_event is not None and stop_event.is_set():
                raise UploadInterrupted("Shutdown requested during upload")
            try:
                status, response = insert.next_chunk(num_retries=0)
                attempt = 0
                if insert.resumable_uri and on_session:
                    if insert.resumable_uri != reported_uri or status:
                        on_session(insert.resumable_uri, insert.resumable_progress)
                        reported_uri = insert.resumable_uri
                if status and progress:
                    progress(status.resumable_progress, status.total_size)
            except ResumableUploadError as exc:
                error = classify_http_error(exc)
                if not error.retryable or isinstance(error, QuotaExceededError):
                    raise error from exc
                attempt = self._retry_or_raise(attempt, error, stop_event)
            except HttpError as exc:
                error = classify_http_error(exc)
                if not isinstance(error, TransientUploadError):
                    raise error from exc
                attempt = self._retry_or_raise(attempt, error, stop_event)
            except RefreshError as exc:
                raise AuthUploadError(f"Token refresh failed: {exc}") from exc
            except (httplib2.HttpLib2Error, OSError, http.client.HTTPException, socket.timeout,
                    ssl.SSLError, TransportError) as exc:
                attempt = self._retry_or_raise(attempt, TransientUploadError(f"Network error: {exc}"), stop_event)

        if progress:
            size = request.file_path.stat().st_size
            progress(size, size)
        if "id" not in response:
            raise PermanentUploadError(f"Unexpected upload response: {str(response)[:300]}")
        return UploadResult.from_resource(response)

    def _retry_or_raise(self, attempt: int, error: UploadError, stop_event) -> int:
        attempt += 1
        if attempt > self.max_chunk_retries:
            raise error
        delay = backoff_delay(attempt)
        log.warning("Upload chunk failed (%s); retry %d/%d in %.0fs", error, attempt, self.max_chunk_retries, delay)
        _sleep(delay, stop_event)
        return attempt

    def query_session(self, resumable_uri, total_bytes):
        from google.auth.transport.requests import AuthorizedSession

        _, creds = self._service()
        session = AuthorizedSession(creds)
        try:
            resp = session.put(resumable_uri, headers={"Content-Range": f"bytes */{total_bytes}",
                                                       "Content-Length": "0"}, timeout=60)
        except Exception as exc:
            raise TransientUploadError(f"Could not query upload session: {type(exc).__name__}") from exc
        if resp.status_code in (200, 201):
            return SessionStatus("complete", total_bytes, UploadResult.from_resource(resp.json()))
        if resp.status_code == 308:
            range_header = resp.headers.get("Range", "")
            offset = int(range_header.split("-")[-1]) + 1 if range_header else 0
            return SessionStatus("incomplete", offset)
        if resp.status_code in (404, 410):
            return SessionStatus("expired")
        if resp.status_code in RETRIABLE_STATUS:
            raise TransientUploadError(f"Upload session query returned HTTP {resp.status_code}")
        if resp.status_code == 401:
            raise AuthUploadError("Not authorized to query the upload session")
        return SessionStatus("expired")

    def find_recent_upload(self, title, since):
        youtube, _ = self._service()
        channels = self._execute(lambda: youtube.channels().list(part="contentDetails", mine=True))
        items = channels.get("items") or []
        if not items:
            return None
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        playlist = self._execute(lambda: youtube.playlistItems().list(
            part="contentDetails", playlistId=uploads, maxResults=25))
        ids = [i["contentDetails"]["videoId"] for i in playlist.get("items", [])]
        if not ids:
            return None
        videos = self._execute(lambda: youtube.videos().list(part="snippet,status", id=",".join(ids)))
        threshold = since.astimezone(timezone.utc).timestamp() - 600
        for item in videos.get("items", []):
            snippet = item.get("snippet") or {}
            published = parse_rfc3339(snippet.get("publishedAt"))
            if snippet.get("title") == title and published and published.timestamp() >= threshold:
                return UploadResult.from_resource(item)
        return None

    def get_videos(self, video_ids):
        if not video_ids:
            return {}
        youtube, _ = self._service()
        results: dict[str, UploadResult] = {}
        for start in range(0, len(video_ids), 50):
            batch = video_ids[start:start + 50]
            response = self._execute(lambda: youtube.videos().list(part="snippet,status", id=",".join(batch)))
            for item in response.get("items", []):
                results[item["id"]] = UploadResult.from_resource(item)
        return results

    def set_publish_at(self, video_id, publish_at):
        youtube, _ = self._service()
        current = self._execute(lambda: youtube.videos().list(part="status", id=video_id))
        items = current.get("items") or []
        if not items:
            raise PermanentUploadError(f"Video {video_id} not found")
        status = items[0]["status"]
        status.update({"privacyStatus": "private", "publishAt": to_rfc3339(publish_at)})
        updated = self._execute(lambda: youtube.videos().update(part="status", body={"id": video_id, "status": status}))
        return UploadResult.from_resource(updated)

    def test_connection(self):
        youtube, _ = self._service()
        response = self._execute(lambda: youtube.channels().list(part="snippet", mine=True))
        items = response.get("items") or []
        if not items:
            raise AuthUploadError("Connected account has no YouTube channel")
        return f"API reachable - channel '{items[0]['snippet']['title']}' ({items[0]['id']})"


# --- mock uploader --------------------------------------------------------------------

class MockUploader(BaseUploader):
    """Simulates YouTube locally. Nothing leaves the machine.

    ``fail_mode`` (or the MOCK_FAIL_MODE env var) injects failures:
    ``transient_once``, ``transient_always``, ``permanent``, ``auth``, ``quota``,
    ``crash_after_upload`` (process "dies" after YouTube accepted the file),
    ``interrupt_midway`` (upload stops halfway, as if the PC lost power),
    ``drop_publish_at`` (YouTube response lacks the schedule).
    """

    name = "mock"
    _lock = threading.Lock()

    def __init__(self, store_path: Path, fail_mode: str = "", chunk_size: int = 256 * 1024, delay: float = 0.0):
        self.store_path = store_path
        self.fail_mode = fail_mode
        self.chunk_size = chunk_size
        self.delay = delay
        self._failed_once = False

    # storage -------------------------------------------------------------------
    def _load(self) -> dict:
        if self.store_path.is_file():
            return json.loads(self.store_path.read_text(encoding="utf-8"))
        return {"videos": {}, "sessions": {}}

    def _save(self, data: dict) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.store_path)

    def all_videos(self) -> dict:
        with self._lock:
            return self._load()["videos"]

    # interface --------------------------------------------------------------------
    def upload(self, request, progress=None, on_session=None, stop_event=None, resume=None):
        mode = self.fail_mode
        if mode == "auth":
            raise AuthUploadError("Mock: authorization revoked")
        if mode == "quota":
            raise QuotaExceededError("Mock: quotaExceeded")
        if mode == "permanent":
            raise PermanentUploadError("Mock: HTTP 400 invalidVideoMetadata")
        if mode == "transient_always" or (mode == "transient_once" and not self._failed_once):
            self._failed_once = True
            raise TransientUploadError("Mock: HTTP 503 backendError")

        total = request.file_path.stat().st_size
        with self._lock:
            data = self._load()
            if resume and resume[0] in data["sessions"]:
                session_id, offset = resume[0], resume[1]
            else:
                session_id, offset = f"mock-session://{uuid.uuid4().hex}", 0
                data["sessions"][session_id] = {"body": request.body(), "received": 0, "total": total,
                                                "video_id": None, "created": time.time()}
                self._save(data)
        if on_session:
            on_session(session_id, offset)

        chunk_size = max(1, total // 4) if mode == "interrupt_midway" else self.chunk_size
        with open(request.file_path, "rb") as fh:  # read the real bytes to exercise file access
            fh.seek(offset)
            while offset < total:
                if stop_event is not None and stop_event.is_set():
                    raise UploadInterrupted("Shutdown requested during upload")
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                offset += len(chunk)
                with self._lock:
                    data = self._load()
                    data["sessions"][session_id]["received"] = offset
                    self._save(data)
                if on_session:
                    on_session(session_id, offset)
                if progress:
                    progress(offset, total)
                if self.delay:
                    time.sleep(self.delay)
                if mode == "interrupt_midway" and offset >= total / 2 and offset < total:
                    raise SimulatedCrash("Mock: power lost mid-upload")

        result = self._complete_session(session_id)
        if mode == "crash_after_upload":
            raise SimulatedCrash("Mock: process died after YouTube accepted the upload")
        return result

    def _complete_session(self, session_id: str) -> UploadResult:
        with self._lock:
            data = self._load()
            session = data["sessions"][session_id]
            if session["video_id"]:
                return UploadResult.from_resource(data["videos"][session["video_id"]])
            video_id = "MOCK" + uuid.uuid4().hex[:7]
            body = json.loads(json.dumps(session["body"]))
            if self.fail_mode == "drop_publish_at":
                body["status"].pop("publishAt", None)
            resource = {"id": video_id, "snippet": {**body["snippet"],
                                                     "publishedAt": to_rfc3339(datetime.now(timezone.utc))},
                        "status": body["status"]}
            data["videos"][video_id] = resource
            session["video_id"] = video_id
            self._save(data)
            log.info("[MOCK] Upload accepted as %s (nothing was sent to YouTube)", video_id)
            return UploadResult.from_resource(resource)

    def query_session(self, resumable_uri, total_bytes):
        with self._lock:
            session = self._load()["sessions"].get(resumable_uri)
        if session is None:
            return SessionStatus("expired")
        if session["video_id"]:
            with self._lock:
                resource = self._load()["videos"][session["video_id"]]
            return SessionStatus("complete", total_bytes, UploadResult.from_resource(resource))
        if session["received"] >= session["total"]:
            return SessionStatus("complete", total_bytes, self._complete_session(resumable_uri))
        return SessionStatus("incomplete", session["received"])

    def find_recent_upload(self, title, since):
        threshold = since.astimezone(timezone.utc).timestamp() - 600
        for resource in self.all_videos().values():
            published = parse_rfc3339(resource["snippet"].get("publishedAt"))
            if resource["snippet"].get("title") == title and published and published.timestamp() >= threshold:
                return UploadResult.from_resource(resource)
        return None

    def get_videos(self, video_ids):
        now = datetime.now(timezone.utc)
        out = {}
        with self._lock:
            data = self._load()
            changed = False
            for vid in video_ids:
                resource = data["videos"].get(vid)
                if not resource:
                    continue
                status = resource["status"]
                publish_at = parse_rfc3339(status.get("publishAt"))
                if status.get("privacyStatus") == "private" and publish_at and publish_at <= now:
                    status["privacyStatus"] = "public"   # what YouTube does at publishAt
                    status.pop("publishAt", None)
                    changed = True
                out[vid] = UploadResult.from_resource(resource)
            if changed:
                self._save(data)
        return out

    def set_publish_at(self, video_id, publish_at):
        with self._lock:
            data = self._load()
            resource = data["videos"].get(video_id)
            if not resource:
                raise PermanentUploadError(f"Video {video_id} not found")
            resource["status"].update({"privacyStatus": "private", "publishAt": to_rfc3339(publish_at)})
            self._save(data)
            return UploadResult.from_resource(resource)

    def test_connection(self):
        return f"Mock mode - simulated YouTube store at {self.store_path.name} ({len(self.all_videos())} videos)"


def create_uploader(cfg, auth=None) -> BaseUploader:
    if cfg.is_mock:
        return MockUploader(cfg.mock_store_path, fail_mode=cfg.mock_fail_mode)
    if auth is None:
        from youtube_auth import YouTubeAuth
        auth = YouTubeAuth(cfg)
    return LiveUploader(auth.load_credentials, chunk_mb=cfg.upload_chunk_mb, max_chunk_retries=cfg.max_chunk_retries)
