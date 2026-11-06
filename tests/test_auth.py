import json
import sys

import pytest
from googleapiclient.errors import HttpError

from logging_setup import redact
from youtube_auth import AuthError, SecureTokenStore, validate_client_secret
from youtube_uploader import (AuthUploadError, PermanentUploadError, QuotaExceededError, TransientUploadError,
                              classify_http_error)


def test_token_store_round_trip_is_not_plaintext(tmp_path):
    store = SecureTokenStore(tmp_path / "token.bin")
    secret = json.dumps({"refresh_token": "1//0secret-refresh-token-value", "token": "ya29.abc"})
    store.save(secret)
    raw = (tmp_path / "token.bin").read_bytes()
    if sys.platform == "win32":
        assert b"secret-refresh-token-value" not in raw  # DPAPI encrypted
    assert store.load() == secret
    store.delete()
    assert store.load() is None


def test_client_secret_validation(tmp_path):
    path = tmp_path / "client_secret.json"
    with pytest.raises(AuthError, match="not found"):
        validate_client_secret(path)
    path.write_text(json.dumps({"web": {}}), encoding="utf-8")
    with pytest.raises(AuthError, match="Desktop app"):
        validate_client_secret(path)
    path.write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")
    validate_client_secret(path)


class Resp(dict):
    def __init__(self, status):
        super().__init__()
        self.status = status
        self.reason = "x"


def http_error(status, reason):
    content = json.dumps({"error": {"message": "msg", "errors": [{"reason": reason}]}}).encode()
    return HttpError(Resp(status), content)


@pytest.mark.parametrize("status,reason,expected", [
    (503, "backendError", TransientUploadError),
    (500, "", TransientUploadError),
    (403, "rateLimitExceeded", TransientUploadError),
    (403, "quotaExceeded", QuotaExceededError),
    (400, "uploadLimitExceeded", QuotaExceededError),
    (401, "authError", AuthUploadError),
    (403, "forbidden", AuthUploadError),
    (400, "invalidTitle", PermanentUploadError),
    (400, "invalidPublishAt", PermanentUploadError),
])
def test_http_error_classification(status, reason, expected):
    assert isinstance(classify_http_error(http_error(status, reason)), expected)


def test_redaction_patterns():
    text = redact("Bearer ya29.A0abc sk-ant-api03-ABCDEFGHIJK GOCSPX-abcdef upload_id=ABC123 password=hunter2")
    for leaked in ("ya29.A0abc", "sk-ant-api03", "GOCSPX-abcdef", "ABC123", "hunter2"):
        assert leaked not in text
