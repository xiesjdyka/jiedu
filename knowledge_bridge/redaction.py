"""Fixed-shape recursive redaction for configuration, diagnostics and audits."""
from __future__ import annotations

import re
from typing import Any

REDACTED = "<sensitive-value-hidden>"
_SENSITIVE_KEYS = (
    "secret", "password", "passphrase", "authorization", "cookie", "csrf",
    "approval_token", "recovery_key", "access_token", "refresh_token",
)
_CLIENT_SECRET = re.compile(r"jiedu-client-v1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_AUTH_HEADER = re.compile(r"(?i)(?:Bearer|JieduClient)\s+[A-Za-z0-9._~+/:=-]+")
_ENV_SECRET_LINE = re.compile(
    r"(?im)^(\s*(?:KNOWLEDGE_BRIDGE_CLIENT_SECRET|AUTHORIZATION|COOKIE|X-JIEDU-CSRF)\s*[=:]\s*).*$"
)


def sensitive_key(key: object) -> bool:
    folded = str(key).casefold()
    return any(marker in folded for marker in _SENSITIVE_KEYS)


def redact_text(value: object) -> str:
    text = str(value)
    text = _CLIENT_SECRET.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(REDACTED, text)
    text = _ENV_SECRET_LINE.sub(lambda match: match.group(1) + REDACTED, text)
    return text


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): REDACTED if sensitive_key(key) else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
