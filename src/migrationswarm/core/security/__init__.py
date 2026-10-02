"""Small, reusable security boundaries for local and model-assisted work."""

from migrationswarm.core.security.artifacts import (
    ArtifactCorruptionError,
    ensure_json_artifact_healthy,
    load_json_object,
    write_json_atomic,
    write_text_atomic,
)
from migrationswarm.core.security.limits import (
    MAX_ARTIFACT_BYTES,
    MAX_LOG_BYTES,
    MAX_MODEL_RESPONSE_BYTES,
    MAX_REPOSITORY_FILES,
    InputLimitError,
    require_bytes,
)
from migrationswarm.core.security.paths import (
    PathSafetyError,
    ensure_contained,
    normalize_relative_path,
    safe_join,
)
from migrationswarm.core.security.redaction import redact_secrets, redact_text

__all__ = [
    "ArtifactCorruptionError",
    "InputLimitError",
    "MAX_ARTIFACT_BYTES",
    "MAX_LOG_BYTES",
    "MAX_MODEL_RESPONSE_BYTES",
    "MAX_REPOSITORY_FILES",
    "PathSafetyError",
    "ensure_contained",
    "ensure_json_artifact_healthy",
    "load_json_object",
    "normalize_relative_path",
    "redact_secrets",
    "redact_text",
    "require_bytes",
    "safe_join",
    "write_json_atomic",
    "write_text_atomic",
]
