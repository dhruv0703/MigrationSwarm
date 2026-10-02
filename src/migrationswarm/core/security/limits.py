"""Explicit input and output bounds used by security-sensitive code."""

from __future__ import annotations

MAX_MODEL_RESPONSE_BYTES = 1_000_000
MAX_ARTIFACT_BYTES = 5_000_000
MAX_LOG_BYTES = 2_000_000
MAX_REPOSITORY_FILES = 50_000


class InputLimitError(ValueError):
    """Raised when an external or generated value exceeds a safety bound."""


def require_bytes(value: str | bytes, limit: int, *, label: str) -> str | bytes:
    """Reject an oversized value before it can be parsed or written."""
    size = len(value.encode("utf-8") if isinstance(value, str) else value)
    if size > limit:
        raise InputLimitError(f"{label} exceeds the {limit}-byte limit")
    return value
