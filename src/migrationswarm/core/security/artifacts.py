"""Bounded and corruption-explicit JSON artifact I/O."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from migrationswarm.core.security.limits import MAX_ARTIFACT_BYTES, require_bytes
from migrationswarm.core.security.redaction import redact_secrets


class ArtifactCorruptionError(ValueError):
    """Raised when a required artifact is missing, oversized, or malformed."""


def load_json_object(path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> dict[str, Any]:
    """Load one JSON object without silently regenerating a bad artifact."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ArtifactCorruptionError(f"Could not read artifact: {path.name}") from error
    try:
        require_bytes(raw, max_bytes, label="artifact")
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ArtifactCorruptionError(f"Artifact is corrupt: {path.name}") from error
    if not isinstance(payload, dict):
        raise ArtifactCorruptionError(f"Artifact must contain a JSON object: {path.name}")
    return payload


def write_json_atomic(path: Path, payload: object, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> None:
    """Redact and atomically write a bounded JSON artifact."""
    content = json.dumps(redact_secrets(payload), indent=2, sort_keys=True) + "\n"
    require_bytes(content, max_bytes, label="artifact")
    temporary: str | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False
        ) as handle:
            handle.write(content)
            temporary = handle.name
        os.replace(temporary, path)
    finally:
        if temporary is not None and Path(temporary).exists():
            Path(temporary).unlink()


def write_text_atomic(path: Path, content: str, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> None:
    """Atomically write a bounded text artifact."""
    require_bytes(content, max_bytes, label="artifact")
    temporary: str | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False
        ) as handle:
            handle.write(content)
            temporary = handle.name
        os.replace(temporary, path)
    finally:
        if temporary is not None and Path(temporary).exists():
            Path(temporary).unlink()


def ensure_json_artifact_healthy(path: Path) -> None:
    """Reject an existing corrupt JSON artifact before a producer overwrites it."""
    if path.exists():
        load_json_object(path)
