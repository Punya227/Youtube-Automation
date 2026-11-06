"""Logging configuration with secret redaction."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"ya29\.[0-9A-Za-z\-_\.]+"),                 # Google access tokens
    re.compile(r"1//[0-9A-Za-z\-_]{20,}"),                    # Google refresh tokens
    re.compile(r"sk-ant-[0-9A-Za-z\-_]{10,}"),                # Anthropic API keys
    re.compile(r"GOCSPX-[0-9A-Za-z\-_]+"),                    # Google OAuth client secrets
    re.compile(r"(?i)(\"?(?:access_token|refresh_token|client_secret|id_token|api_key|password)\"?\s*[:=]\s*\"?)[^\s\",}]+"),
    re.compile(r"(?i)(upload_id=)[0-9A-Za-z\-_]+"),           # resumable upload session ids
    re.compile(r"(?i)(Bearer\s+)[0-9A-Za-z\-_\.]+"),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args; leave as is
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_configured: set[str] = set()


def setup_logging(logs_dir: Path, name: str = "app", level: str = "INFO", console: bool = True) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if name in _configured:
        return logging.getLogger("ysa")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    redactor = RedactingFilter()

    file_handler = RotatingFileHandler(logs_dir / f"{name}.log", maxBytes=5 * 1024 * 1024,
                                       backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        stream.addFilter(redactor)
        root.addHandler(stream)

    # Third-party libraries are chatty at INFO and may echo request URLs.
    for noisy in ("googleapiclient.discovery_cache", "googleapiclient.discovery", "urllib3", "httpx", "httpx2",
                  "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # "maximum number of running instances reached" is expected while an upload runs.
    logging.getLogger("apscheduler.scheduler").setLevel(logging.ERROR)
    _configured.add(name)
    return logging.getLogger("ysa")
