"""Loading and validating fixture benchmark metadata."""

import json
from pathlib import Path

from pydantic import ValidationError

from migrationswarm.evaluation.models import FixtureMetadata

METADATA_FILENAME = "benchmark.json"


class FixtureMetadataError(ValueError):
    """Raised when a fixture has missing or invalid benchmark metadata."""


def load_fixture_metadata(fixture: str | Path) -> FixtureMetadata:
    """Load the explicit benchmark contract at a fixture root."""
    root = Path(fixture).expanduser().resolve()
    path = root / METADATA_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        metadata = FixtureMetadata.model_validate(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise FixtureMetadataError(f"Could not load fixture metadata {path}: {error}") from error
    if metadata.name != root.name:
        raise FixtureMetadataError(
            f"Fixture metadata name {metadata.name!r} does not match directory {root.name!r}"
        )
    return metadata
