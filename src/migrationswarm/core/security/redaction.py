"""Central secret redaction for errors, metadata, artifacts, and reports."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "database_url",
    "password",
    "redis_url",
    "secret",
    "token",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)((?:api[_ -]?key|password|token|secret)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(://[^:/\s]+:)[^@\s]+(@)"),
)


def redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    """Redact known secret values and common credential-shaped text."""
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)(bearer"):
            result = pattern.sub(r"\1[redacted]", result)
        elif "://" in pattern.pattern:
            result = pattern.sub(r"\1[redacted]\2", result)
        else:
            result = pattern.sub(r"\1[redacted]", result)
    return result


def redact_secrets(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively redact sensitive mapping values while preserving structure."""
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            key: "[redacted]"
            if str(key).casefold() in _SECRET_KEYS
            else redact_secrets(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item, secrets) for item in value)
    if isinstance(value, set):
        return {redact_secrets(item, secrets) for item in value}
    return value
