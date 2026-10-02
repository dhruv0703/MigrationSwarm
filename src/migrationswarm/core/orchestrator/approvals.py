"""Local, explicit service approval metadata."""

import json
from pathlib import Path

from migrationswarm.agents.service_boundary import ServiceBoundaryReport
from migrationswarm.core.orchestrator.multi_service import (
    SERVICE_APPROVALS_ARTIFACT,
    ServiceApprovalArtifact,
    UnknownServiceError,
    _candidate_key,
)
from migrationswarm.core.security import ArtifactCorruptionError, load_json_object


def load_approvals(repository_root: Path) -> ServiceApprovalArtifact:
    path = repository_root / SERVICE_APPROVALS_ARTIFACT
    if not path.is_file():
        return ServiceApprovalArtifact()
    try:
        return ServiceApprovalArtifact.model_validate(load_json_object(path))
    except (ArtifactCorruptionError, ValueError) as error:
        raise ValueError("Service approval artifact is corrupt") from error


def _candidate_names(repository_root: Path) -> dict[str, str]:
    report = ServiceBoundaryReport.model_validate(
        load_json_object(repository_root / ".migrationswarm" / "service-boundaries.json")
    )
    return {
        _candidate_key(candidate.name): candidate.name
        for candidate in report.candidate_services
    }


def update_approval(
    repository_root: str | Path,
    service: str,
    *,
    approved: bool,
) -> ServiceApprovalArtifact:
    root = Path(repository_root).expanduser().resolve()
    candidates = _candidate_names(root)
    canonical = candidates.get(_candidate_key(service))
    if canonical is None:
        raise UnknownServiceError(f"Service is not a known candidate: {service}")
    artifact = load_approvals(root)
    values = set(artifact.approved)
    if approved:
        values.add(canonical)
    else:
        values.discard(canonical)
    updated = ServiceApprovalArtifact(approved=sorted(values, key=str.casefold))
    path = root / SERVICE_APPROVALS_ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(updated.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return updated


__all__ = ["load_approvals", "update_approval"]
