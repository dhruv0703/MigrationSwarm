"""Deterministic discovery and dispatch of explicit repair tasks."""

import json
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from migrationswarm.agents.build_verification import BuildVerificationResult
from migrationswarm.core.orchestrator import (
    FailureCategory,
    FailureClassifier,
)
from migrationswarm.core.tasks import Task, TaskStateMachine, TaskStatus, TaskType
from migrationswarm.core.verification import (
    VerificationDecision,
    VerificationDecisionResult,
    VerificationEvidence,
)
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    RepairAttemptRepository,
    TaskRepository,
)
from migrationswarm.persistence.exceptions import RecordNotFoundError
from migrationswarm.workers.debugging.models import (
    DebugDispatchResult,
    RepairAttempt,
    RepairAttemptStatus,
)
from migrationswarm.workers.debugging.queue import DebugQueue
from migrationswarm.workers.verification.evidence import VerificationEvidenceLoader


class DebugDispatcher:
    """Create at most one durable DEBUG task per failed task/attempt."""

    def __init__(
        self,
        task_repository: TaskRepository,
        repair_repository: RepairAttemptRepository,
        queue: DebugQueue,
        *,
        repository_root: Path,
        execution_repository: AgentExecutionRepository | None = None,
        max_attempts: int = 2,
        state_machine: type[TaskStateMachine] = TaskStateMachine,
    ) -> None:
        if not 0 <= max_attempts <= 3:
            raise ValueError("max_attempts must be between 0 and 3")
        self.task_repository = task_repository
        self.repair_repository = repair_repository
        self.queue = queue
        self.repository_root = repository_root.expanduser().resolve()
        self.execution_repository = execution_repository
        self.max_attempts = max_attempts
        self.state_machine = state_machine
        self.classifier = FailureClassifier()
        self.evidence_loader = VerificationEvidenceLoader(
            self.repository_root, execution_repository
        )

    def inspect(self, project_id: UUID) -> DebugDispatchResult:
        """Report eligible failures without creating tasks or changing state."""
        result = self._scan(project_id)
        return DebugDispatchResult(
            project_id=project_id,
            eligible_task_ids=result[0],
            debug_task_ids=result[1],
            skipped_task_ids=result[2],
            eligible_count=len(result[0]),
            repair_pending_count=len(result[1]),
        )

    def dispatch(self, project_id: UUID) -> DebugDispatchResult:
        """Discover failures, create stable DEBUG tasks, and enqueue their IDs."""
        eligible: list[UUID] = []
        debug_ids: list[UUID] = []
        skipped: list[UUID] = []
        enqueued: list[UUID] = []
        for task in sorted(
            self.task_repository.list(project_id=project_id, status=TaskStatus.FAILED),
            key=lambda item: str(item.id),
        ):
            decision, evidence, verification = self._failure(task)
            if decision is None or decision.decision is not VerificationDecision.FAILED:
                continue
            if evidence is None:
                try:
                    evidence = VerificationEvidence.model_validate(decision.evidence_summary)
                except ValidationError:
                    skipped.append(task.id)
                    continue
            eligibility = self.classifier.classify(verification, evidence)
            attempts = self.repair_repository.list_for_original(task.id)
            latest_attempt = attempts[-1] if attempts else None
            if latest_attempt is not None and latest_attempt.status in {
                RepairAttemptStatus.PENDING,
                RepairAttemptStatus.RUNNING,
            }:
                if self.queue.enqueue(latest_attempt.debug_task_id):
                    enqueued.append(latest_attempt.debug_task_id)
                continue
            if not eligibility.eligible or len(attempts) >= self.max_attempts:
                if latest_attempt is not None:
                    latest_attempt.status = (
                        RepairAttemptStatus.EXHAUSTED
                        if eligibility.eligible
                        else RepairAttemptStatus.HUMAN_REVIEW
                    )
                    self.repair_repository.update(latest_attempt)
                if task.status is TaskStatus.FAILED:
                    self.state_machine.transition(task, TaskStatus.HUMAN_REVIEW)
                    self.task_repository.update(task)
                skipped.append(task.id)
                continue
            eligible.append(task.id)
            attempt_number = len(attempts) + 1
            debug_id = uuid5(
                NAMESPACE_URL, f"migrationswarm:debug:{task.id}:{attempt_number}"
            )
            attempt_id = uuid5(
                NAMESPACE_URL, f"migrationswarm:repair:{task.id}:{attempt_number}"
            )
            existing_attempt = self.repair_repository.get(attempt_id)
            if existing_attempt is not None:
                debug_ids.append(debug_id)
                if self.queue.enqueue(debug_id):
                    enqueued.append(debug_id)
                continue
            metadata = self._debug_metadata(
                task, attempt_id, attempt_number, eligibility.category, evidence, verification
            )
            debug_task = Task(
                id=debug_id,
                project_id=task.project_id,
                task_type=TaskType.DEBUG,
                title=f"Repair failed task {task.id}",
                description="Apply one bounded repair to the failed task worktree.",
                status=TaskStatus.READY,
                dependencies=[task.id],
                assigned_agent="debug",
                max_attempts=1,
                metadata=metadata,
            )
            attempt = RepairAttempt(
                id=attempt_id,
                original_task_id=task.id,
                debug_task_id=debug_id,
                attempt_number=attempt_number,
                failure_category=eligibility.category.value,
                verification_artifact_before=(
                    str(evidence.artifact_paths[0]) if evidence.artifact_paths else None
                ),
            )
            try:
                self.task_repository.create(debug_task)
                self.repair_repository.create(attempt)
            except RecordNotFoundError:
                raise
            debug_ids.append(debug_id)
            if self.queue.enqueue(debug_id):
                enqueued.append(debug_id)
        return DebugDispatchResult(
            project_id=project_id,
            eligible_task_ids=eligible,
            debug_task_ids=debug_ids,
            enqueued_task_ids=enqueued,
            skipped_task_ids=skipped,
            eligible_count=len(eligible),
            repair_pending_count=len(debug_ids),
        )

    def _scan(self, project_id: UUID) -> tuple[list[UUID], list[UUID], list[UUID]]:
        eligible: list[UUID] = []
        pending: list[UUID] = []
        skipped: list[UUID] = []
        for task in self.task_repository.list(project_id=project_id, status=TaskStatus.FAILED):
            decision, evidence, verification = self._failure(task)
            if decision is None or decision.decision is not VerificationDecision.FAILED:
                continue
            if evidence is None:
                try:
                    evidence = VerificationEvidence.model_validate(decision.evidence_summary)
                except ValidationError:
                    skipped.append(task.id)
                    continue
            eligibility = self.classifier.classify(verification, evidence)
            attempts = self.repair_repository.list_for_original(task.id)
            if attempts and attempts[-1].status in {
                RepairAttemptStatus.PENDING,
                RepairAttemptStatus.RUNNING,
            }:
                pending.append(attempts[-1].debug_task_id)
                continue
            if eligibility.eligible and len(attempts) < self.max_attempts:
                eligible.append(task.id)
            elif eligibility.eligible or not eligibility.eligible:
                skipped.append(task.id)
            if attempts:
                pending.append(attempts[-1].debug_task_id)
        return eligible, pending, skipped

    def _failure(
        self, task: Task
    ) -> tuple[
        VerificationDecisionResult | None,
        VerificationEvidence | None,
        BuildVerificationResult | None,
    ]:
        decision_path = (
            self.repository_root
            / ".migrationswarm"
            / "verification-decisions"
            / f"{task.id}.json"
        )
        try:
            decision = VerificationDecisionResult.model_validate(
                json.loads(decision_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError, ValidationError):
            return None, None, None
        loaded = self.evidence_loader.load(task.id)
        verification = self._verification_result(loaded.source_path)
        return decision, loaded.evidence, verification

    def _verification_result(self, source_path: str | None) -> BuildVerificationResult | None:
        if not source_path:
            return None
        path = Path(source_path)
        if not path.is_absolute():
            path = self.repository_root / path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload.pop("artifacts", None)
                payload.pop("task_type", None)
                return BuildVerificationResult.model_validate(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, ValidationError):
            return None
        return None

    @staticmethod
    def _debug_metadata(
        task: Task,
        attempt_id: UUID,
        attempt_number: int,
        category: FailureCategory,
        evidence: VerificationEvidence,
        verification: BuildVerificationResult | None,
    ) -> dict[str, object]:
        evidence_dict = evidence.model_dump(mode="json")
        failure_evidence: dict[str, object] = {
            key: evidence_dict.get(key)
            for key in (
                "build_status", "build_exit_code", "tests_run", "test_failures",
                "test_errors", "test_skipped", "warnings", "changed_files",
            )
        }
        values: dict[str, object] = {
            "original_task_id": str(task.id),
            "repair_attempt_id": str(attempt_id),
            "repair_attempt": attempt_number,
            "failure_category": category.value,
            "verification_artifact": (
                str(evidence_dict.get("artifact_paths", [None])[0])
                if evidence_dict.get("artifact_paths")
                else None
            ),
            "worktree_path": task.metadata.get("worktree_path"),
            "target_service_directory": task.metadata.get("target_service_directory", "."),
            "failure_evidence": failure_evidence,
            "relevant_files": list(verification.changed_files) if verification else [],
        }
        if verification is not None:
            failure_evidence.update(
                {
                "stdout_summary": verification.stdout_summary[:1200],
                "stderr_summary": verification.stderr_summary[:1200],
                }
            )
            if not values["worktree_path"]:
                values["worktree_path"] = _worktree_from_path(verification.workspace_path, task.id)
            if task.metadata.get("target_service_directory") is None:
                values["target_service_directory"] = _service_directory_from_path(
                    verification.workspace_path
                )
        return values


def _worktree_from_path(value: str, task_id: UUID) -> str | None:
    path = Path(value).expanduser()
    parts = list(path.parts)
    try:
        index = parts.index("worktrees")
    except ValueError:
        return None
    if index + 1 >= len(parts) or parts[index + 1] != str(task_id):
        return None
    return str(Path(*parts[: index + 2]))


def _service_directory_from_path(value: str) -> str:
    path = Path(value)
    parts = list(path.parts)
    try:
        index = parts.index("services")
    except ValueError:
        return "."
    return str(Path(*parts[index:])).replace("\\", "/")


__all__ = ["DebugDispatcher"]
