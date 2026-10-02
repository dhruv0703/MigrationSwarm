"""Load normalized verification evidence without reparsing raw build logs."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from migrationswarm.agents.build_verification import BuildVerificationResult
from migrationswarm.core.security import (
    ArtifactCorruptionError,
    PathSafetyError,
    ensure_contained,
    load_json_object,
)
from migrationswarm.core.verification import VerificationEvidence
from migrationswarm.persistence.db.repositories import AgentExecutionRepository
from migrationswarm.workers.verification.models import EvidenceLoadResult


class VerificationEvidenceLoader:
    """Prefer the structured JSON artifact, then persisted execution metadata."""

    def __init__(
        self,
        repository_root: Path,
        execution_repository: AgentExecutionRepository | None = None,
    ) -> None:
        self.repository_root = repository_root.expanduser().resolve()
        self.execution_repository = execution_repository

    def load(self, task_id: UUID) -> EvidenceLoadResult:
        """Return normalized evidence or an explicit insufficient-evidence reason."""
        artifact = (
            self.repository_root
            / ".migrationswarm"
            / "verification-results"
            / f"{task_id}.json"
        )
        if artifact.exists():
            return self._load_artifact(task_id, artifact)
        if self.execution_repository is not None:
            return self._load_persisted_metadata(task_id)
        return EvidenceLoadResult(
            task_id=task_id,
            reason="Verification evidence artifact is missing.",
        )

    def _load_artifact(self, task_id: UUID, artifact: Path) -> EvidenceLoadResult:
        try:
            payload = load_json_object(artifact)
            artifact_metadata = payload.pop("artifacts", {})
            payload.pop("task_type", None)
            result = BuildVerificationResult.model_validate(payload)
            if result.task_id != task_id:
                raise ValueError("verification artifact task ID does not match requested task")
            paths = [str(artifact)]
            if isinstance(artifact_metadata, dict):
                log_path = artifact_metadata.get("log")
                if isinstance(log_path, str):
                    try:
                        candidate = (
                            ensure_contained(self.repository_root, log_path)
                            if Path(log_path).expanduser().is_absolute()
                            else ensure_contained(
                                self.repository_root,
                                self.repository_root / Path(log_path),
                            )
                        )
                    except PathSafetyError as error:
                        raise ArtifactCorruptionError("verification log path is unsafe") from error
                    paths.append(str(candidate))
            return EvidenceLoadResult(
                task_id=task_id,
                evidence=VerificationEvidence.from_build_result(result, paths),
                source_path=str(artifact),
            )
        except (ArtifactCorruptionError, ValueError, TypeError, ValidationError) as error:
            return EvidenceLoadResult(
                task_id=task_id,
                source_path=str(artifact),
                reason=f"Verification evidence artifact is corrupt: {type(error).__name__}.",
            )

    def _load_persisted_metadata(self, task_id: UUID) -> EvidenceLoadResult:
        assert self.execution_repository is not None
        executions = self.execution_repository.list_for_task(task_id)
        for execution in reversed(executions):
            payload = execution.metadata.get("verification_result")
            if isinstance(payload, dict):
                try:
                    result = BuildVerificationResult.model_validate(payload)
                except (ValueError, TypeError, ValidationError) as error:
                    return EvidenceLoadResult(
                        task_id=task_id,
                        reason=(
                            "Persisted verification evidence is corrupt: "
                            f"{type(error).__name__}."
                        ),
                    )
                return EvidenceLoadResult(
                    task_id=task_id,
                    evidence=VerificationEvidence.from_build_result(
                        result, list(execution.artifacts)
                    ),
                    source_path="persisted agent execution metadata",
                )
        return EvidenceLoadResult(
            task_id=task_id,
            reason="Verification evidence is missing from artifacts and persisted metadata.",
        )


__all__ = ["VerificationEvidenceLoader"]
