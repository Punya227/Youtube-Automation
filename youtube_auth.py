"""Google OAuth 2.0 (Desktop app flow) for the YouTube Data API v3.

* Only an OAuth token is stored - never a Google password.
* On Windows the token file is encrypted with DPAPI (bound to the current
  Windows user account); elsewhere it is written with 0600 permissions.
* Access tokens are refreshed automatically from the refresh token.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from config import YOUTUBE_SCOPES, AppConfig

log = logging.getLogger("ysa.auth")


class AuthError(Exception):
    """The user must (re)connect their YouTube account."""


# --- secure token storage ----------------------------------------------------------

_DPAPI_HEADER = b"YSA-DPAPI-1\n"
_PLAIN_HEADER = b"YSA-PLAIN-1\n"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), ctypes.c_wchar_p("youtube-shorts-automation"),
                                      None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.FormatError())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class SecureTokenStore:
    def __init__(self, path: Path):
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, token_json: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = token_json.encode("utf-8")
        payload = _DPAPI_HEADER + _dpapi(raw, True) if sys.platform == "win32" else _PLAIN_HEADER + raw
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            fh.write(payload)
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def load(self) -> str | None:
        if not self.path.is_file():
            return None
        payload = self.path.read_bytes()
        if payload.startswith(_DPAPI_HEADER):
            return _dpapi(payload[len(_DPAPI_HEADER):], False).decode("utf-8")
        if payload.startswith(_PLAIN_HEADER):
            return payload[len(_PLAIN_HEADER):].decode("utf-8")
        raise AuthError("Token file has an unknown format; please reconnect YouTube")

    def delete(self) -> None:
        # Removes our own credential file (not a user video); needed for disconnect.
        if self.path.is_file():
            self.path.unlink()


# --- credentials -----------------------------------------------------------------------

@dataclass
class ChannelInfo:
    channel_id: str
    title: str
    uploads_playlist_id: str | None


def validate_client_secret(path: Path) -> None:
    if not path.is_file():
        raise AuthError(
            f"OAuth client file not found at {path}. Download it from Google Cloud Console "
            "(APIs & Services > Credentials > OAuth client ID > Desktop app) and save it there."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuthError(f"{path.name} is not valid JSON") from exc
    if "installed" not in data:
        kind = next(iter(data), "unknown")
        raise AuthError(f"{path.name} is a '{kind}' client. Create an OAuth client of type 'Desktop app'.")


class YouTubeAuth:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.store = SecureTokenStore(cfg.token_file)

    def has_token(self) -> bool:
        return self.store.exists()

    def load_credentials(self, refresh: bool = True):
        """Return valid google Credentials or raise AuthError."""
        from google.auth.exceptions import RefreshError, TransportError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        token_json = self.store.load()
        if not token_json:
            raise AuthError("YouTube account is not connected")
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_json))
        except (ValueError, KeyError) as exc:
            raise AuthError("Stored YouTube token is invalid; please reconnect") from exc
        if refresh and (not creds.valid):
            if not creds.refresh_token:
                raise AuthError("Stored token has no refresh token; please reconnect")
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise AuthError("YouTube authorization was revoked or expired; please reconnect") from exc
            except TransportError:
                raise  # network problem: caller treats as temporary
            self.store.save(creds.to_json())
            log.info("Refreshed YouTube access token")
        return creds

    def granted_scopes(self) -> list[str]:
        try:
            token_json = self.store.load()
        except Exception:
            return []
        if not token_json:
            return []
        return list(json.loads(token_json).get("scopes") or [])

    def connect(self, timeout_seconds: int = 300):
        """Run the installed-app OAuth flow in the local browser and store the token."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        validate_client_secret(self.cfg.client_secret_file)
        flow = InstalledAppFlow.from_client_secrets_file(str(self.cfg.client_secret_file), scopes=YOUTUBE_SCOPES)
        creds = flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
            timeout_seconds=timeout_seconds,
            authorization_prompt_message="Opening Google sign-in in your browser...",
            success_message="YouTube account connected. You can close this tab and return to the dashboard.",
        )
        if creds is None or not creds.refresh_token:
            raise AuthError("Google did not return a refresh token; try connecting again")
        self.store.save(creds.to_json())
        log.info("YouTube OAuth token stored")
        return creds

    def disconnect(self, revoke: bool = True) -> None:
        if revoke:
            try:
                import requests

                token_json = self.store.load()
                if token_json:
                    data = json.loads(token_json)
                    token = data.get("refresh_token") or data.get("token")
                    if token:
                        requests.post("https://oauth2.googleapis.com/revoke", data={"token": token},
                                      headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
            except Exception as exc:  # revocation is best effort
                log.warning("Token revocation failed (token removed locally anyway): %s", type(exc).__name__)
        self.store.delete()


def build_youtube_service(creds):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def fetch_channel(creds) -> ChannelInfo:
    youtube = build_youtube_service(creds)
    response = youtube.channels().list(part="snippet,contentDetails", mine=True).execute()
    items = response.get("items") or []
    if not items:
        raise AuthError("This Google account has no YouTube channel. Create a channel first, then reconnect.")
    item = items[0]
    return ChannelInfo(
        channel_id=item["id"],
        title=item["snippet"]["title"],
        uploads_playlist_id=(item.get("contentDetails", {}).get("relatedPlaylists") or {}).get("uploads"),
    )
