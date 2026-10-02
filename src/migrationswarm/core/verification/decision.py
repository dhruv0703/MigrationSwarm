"""Deterministic policy decisions from build verification evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from migrationswarm.agents.build_verification import (
    BuildVerificationResult,
)
from migrationswarm.agents.build_verification import (
    VerificationStatus as BuildVerificationStatus,
)
from migrationswarm.core.security import PathSafetyError, ensure_contained
from migrationswarm.core.tasks import Task, TaskStateMachine, TaskStatus, TaskType

DECISION_RESULTS_DIR = ".migrationswarm/verification-decisions"


class VerificationDecision(StrEnum):
    """Possible deterministic lifecycle decisions."""

    PASSED = "passed"
    FAILED = "failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class VerificationEvidence(BaseModel):
    """Normalized, bounded evidence used by the decision policy."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    build_status: BuildVerificationStatus
    build_exit_code: int | None = None
    tests_run: int | None = Field(default=None, ge=0)
    test_failures: int | None = Field(default=None, ge=0)
    test_errors: int | None = Field(default=None, ge=0)
    test_skipped: int | None = Field(default=None, ge=0)
    changed_files: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_build_result(
        cls,
        result: BuildVerificationResult,
        artifact_paths: list[str],
    ) -> VerificationEvidence:
        """Normalize an existing build result without copying its raw logs."""
        summary = result.test_summary
        return cls(
            task_id=result.task_id,
            build_status=result.status,
            build_exit_code=result.exit_code,
            tests_run=summary.tests_run,
            test_failures=summary.failures,
            test_errors=summary.errors,
            test_skipped=summary.skipped,
            changed_files=list(result.changed_files),
            artifact_paths=list(artifact_paths),
            warnings=list(result.warnings),
        )


class VerificationPolicy(BaseModel):
    """Configurable deterministic completion rules."""

    model_config = ConfigDict(extra="forbid")

    minimum_artifacts: int = Field(default=2, ge=1)
    code_changing_task_types: set[TaskType] = Field(
        default_factory=lambda: {TaskType.CODE_REFACTOR}
    )
    fatal_warning_prefixes: tuple[str, ...] = ("fatal:", "fatal ", "[fatal]")


class VerificationDecisionResult(BaseModel):
    """Decision, reasons, and bounded evidence summary for one task."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    task_id: UUID
    decision: VerificationDecision
    reasons: list[str] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    evidence_summary: dict[str, Any]
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    artifact_path: str | None = None

    @field_validator("decided_at")
    @classmethod
    def ensure_utc_timestamp(cls, value: datetime) -> datetime:
        """Require a timezone-aware UTC decision timestamp."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        return value.astimezone(UTC)


class VerificationCoordinatorError(ValueError):
    """Base exception for lifecycle verification coordination."""


class VerificationStateError(VerificationCoordinatorError):
    """Raised when a task is not in VERIFYING."""


class VerificationDecisionEngine:
    """Evaluate evidence using a deterministic, ordered policy."""

    _NO_TESTS_WARNING: ClassVar[str] = "No tests were discovered or executed."

    def __init__(
        self,
        policy: VerificationPolicy | None = None,
        *,
        artifact_root: Path | None = None,
    ) -> None:
        self.policy = policy or VerificationPolicy()
        self.artifact_root = artifact_root.expanduser().resolve() if artifact_root else None

    def decide(
        self,
        task: Task,
        evidence: VerificationEvidence,
    ) -> VerificationDecisionResult:
        """Return the same decision and reasons for the same task/evidence."""
        if task.id != evidence.task_id:
            raise VerificationCoordinatorError(
                f"Evidence task does not match task: {task.id}"
            )

        warnings = list(evidence.warnings)
        if evidence.tests_run is None or evidence.tests_run == 0:
            warnings.append(self._NO_TESTS_WARNING)

        reasons: list[str] = []
        decision = VerificationDecision.PASSED
        if evidence.build_status in {
            BuildVerificationStatus.FAILED,
            BuildVerificationStatus.ERROR,
        }:
            decision = VerificationDecision.FAILED
            reasons.append(f"Build verification status was {evidence.build_status.value}.")
        elif evidence.build_status is not BuildVerificationStatus.PASSED:
            decision = VerificationDecision.INSUFFICIENT_EVIDENCE
            reasons.append(f"Build verification status was {evidence.build_status.value}.")
        elif evidence.build_exit_code is None:
            decision = VerificationDecision.INSUFFICIENT_EVIDENCE
            reasons.append("Build verification exit code is missing.")
        elif evidence.build_exit_code != 0:
            decision = VerificationDecision.FAILED
            reasons.append(f"Build verification exit code was {evidence.build_exit_code}.")

        if evidence.test_failures is not None and evidence.test_failures > 0:
            decision = VerificationDecision.FAILED
            reasons.append(f"Test failures were reported: {evidence.test_failures}.")
        if evidence.test_errors is not None and evidence.test_errors > 0:
            decision = VerificationDecision.FAILED
            reasons.append(f"Test errors were reported: {evidence.test_errors}.")

        fatal_warnings = [warning for warning in warnings if self._is_fatal_warning(warning)]
        if fatal_warnings:
            decision = VerificationDecision.FAILED
            reasons.append("Fatal verification warning was reported.")

        if decision is not VerificationDecision.FAILED:
            missing_artifacts = self._missing_artifacts(evidence.artifact_paths)
            if missing_artifacts:
                decision = VerificationDecision.INSUFFICIENT_EVIDENCE
                reasons.extend(missing_artifacts)
            if (
                task.task_type in self.policy.code_changing_task_types
                and not evidence.changed_files
            ):
                decision = VerificationDecision.INSUFFICIENT_EVIDENCE
                reasons.append(
                    "No changed files were provided for the code-changing task."
                )

        if not reasons:
            reasons.append("Build and test evidence satisfied the verification policy.")

        return VerificationDecisionResult(
            task_id=task.id,
            decision=decision,
            reasons=reasons,
            warnings=warnings,
            evidence_summary=evidence.model_dump(mode="json"),
        )

    def _missing_artifacts(self, artifact_paths: list[str]) -> list[str]:
        if len(artifact_paths) < self.policy.minimum_artifacts:
            return [
                "Required verification artifacts are missing "
                f"(expected at least {self.policy.minimum_artifacts})."
            ]
        missing: list[str] = []
        for artifact in artifact_paths:
            path = Path(artifact)
            if self.artifact_root is not None:
                try:
                    path = (
                        ensure_contained(self.artifact_root, path)
                        if path.is_absolute()
                        else ensure_contained(self.artifact_root, self.artifact_root / path)
                    )
                except PathSafetyError:
                    missing.append(f"Required verification artifact path is unsafe: {artifact}.")
                    continue
            if not path.is_file():
                missing.append(f"Required verification artifact does not exist: {artifact}.")
        return missing

    def _is_fatal_warning(self, warning: str) -> bool:
        normalized = warning.strip().lower()
        return any(
            normalized.startswith(prefix.lower())
            for prefix in self.policy.fatal_warning_prefixes
        )


class VerificationCoordinator:
    """Apply deterministic verification decisions to VERIFYING tasks."""

    def __init__(
        self,
        repository_root: Path | None = None,
        *,
        state_machine: type[TaskStateMachine] | None = None,
        engine: VerificationDecisionEngine | None = None,
    ) -> None:
        self.repository_root = (repository_root or Path.cwd()).expanduser().resolve()
        self.state_machine = state_machine or TaskStateMachine
        self.engine = engine or VerificationDecisionEngine(artifact_root=self.repository_root)

    def decide(
        self,
        task: Task,
        evidence: VerificationEvidence,
    ) -> VerificationDecisionResult:
        """Decide, transition through TaskStateMachine, and write evidence metadata."""
        if task.status is not TaskStatus.VERIFYING:
            raise VerificationStateError(
                f"Task must be VERIFYING before decision; current status is "
                f"{task.status.value}"
            )
        result = self.engine.decide(task, evidence)
        if result.decision is VerificationDecision.PASSED:
            self.state_machine.transition(task, TaskStatus.COMPLETED)
        elif result.decision is VerificationDecision.FAILED:
            self.state_machine.transition(task, TaskStatus.FAILED)
        result.artifact_path = self._write_artifact(result)
        return result

    def record_insufficient_evidence(
        self,
        task: Task,
        reasons: list[str],
    ) -> VerificationDecisionResult:
        """Record an insufficient-evidence decision without changing task state."""
        if task.status is not TaskStatus.VERIFYING:
            raise VerificationStateError(
                f"Task must be VERIFYING before decision; current status is "
                f"{task.status.value}"
            )
        result = VerificationDecisionResult(
            task_id=task.id,
            decision=VerificationDecision.INSUFFICIENT_EVIDENCE,
            reasons=reasons or ["Verification evidence is insufficient."],
            evidence_summary={},
        )
        result.artifact_path = self._write_artifact(result)
        return result

    def _write_artifact(self, result: VerificationDecisionResult) -> str:
        artifact = self.repository_root / DECISION_RESULTS_DIR / f"{result.task_id}.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": str(result.task_id),
            "decision": result.decision.value,
            "reasons": result.reasons,
            "warnings": result.warnings,
            "evidence_summary": result.evidence_summary,
            "decided_at": result.decided_at.isoformat(),
        }
        artifact.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return str(artifact.relative_to(self.repository_root).as_posix())


__all__ = [
    "DECISION_RESULTS_DIR",
    "VerificationCoordinator",
    "VerificationCoordinatorError",
    "VerificationDecision",
    "VerificationDecisionEngine",
    "VerificationDecisionResult",
    "VerificationEvidence",
    "VerificationPolicy",
    "VerificationStateError",
]
