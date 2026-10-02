"""Explicit synchronous orchestration for one approved service migration."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

import structlog
from pydantic import BaseModel, ValidationError

from migrationswarm.agents.architecture_analysis import (
    ARCHITECTURE_REPORT_ARTIFACT,
    ArchitectureReport,
)
from migrationswarm.agents.build_verification import (
    BuildVerificationAgent,
    BuildVerificationError,
    BuildVerificationResult,
    VerificationStatus,
)
from migrationswarm.agents.debug import DebugAgent
from migrationswarm.agents.dependency_analysis import (
    JAVA_DEPENDENCY_ARTIFACT,
    JavaDependencyGraph,
)
from migrationswarm.agents.migration_planning import (
    MIGRATION_PLAN_JSON_ARTIFACT,
    MigrationPlan,
    MigrationPlanningAgent,
)
from migrationswarm.agents.service_boundary import (
    SERVICE_BOUNDARIES_JSON_ARTIFACT,
    CandidateService,
    ServiceBoundaryReport,
)
from migrationswarm.agents.service_extraction import (
    ServiceExtractionAgent,
    service_slug,
)
from migrationswarm.core.agents import (
    AgentContext,
    AgentRegistry,
    WorkerRuntime,
)
from migrationswarm.core.git import (
    GitRepository,
    GitWorktreeManager,
    UnknownWorktreeError,
)
from migrationswarm.core.observability.performance import PerformanceCollector
from migrationswarm.core.orchestrator.debugging import FailureClassifier
from migrationswarm.core.orchestrator.models import (
    MigrationRun,
    MigrationRunError,
    MigrationRunEventType,
    MigrationRunResult,
    MigrationRunStatus,
)
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.security import redact_text
from migrationswarm.core.tasks import Task, TaskStateMachine, TaskStatus, TaskType
from migrationswarm.core.verification import (
    VerificationCoordinator,
    VerificationDecision,
    VerificationEvidence,
)
from migrationswarm.core.workspaces import TaskWorkspace
from migrationswarm.persistence.mapping import Project

if TYPE_CHECKING:
    from migrationswarm.persistence.service import MigrationStateService

logger = structlog.get_logger(__name__)

RUNS_DIR = ".migrationswarm/runs"
class MigrationOrchestrator:
    """Coordinate existing task, extraction, build, and decision components."""

    def __init__(
        self,
        *,
        extraction_agent: ServiceExtractionAgent | None = None,
        build_agent: BuildVerificationAgent | None = None,
        debug_agent: DebugAgent | None = None,
        state_service: MigrationStateService | None = None,
        performance: PerformanceCollector | None = None,
    ) -> None:
        self.extraction_agent = extraction_agent
        self.build_agent = build_agent
        self.debug_agent = debug_agent
        self.state_service = state_service
        self.performance = performance

    def run(
        self,
        repository_path: str | Path,
        service_name: str,
        *,
        timeout_seconds: float = 300.0,
        dry_run: bool = False,
        max_debug_attempts: int = 2,
        plan_override: MigrationPlan | None = None,
    ) -> MigrationRunResult:
        """Run or validate one service migration with bounded repair attempts."""
        repository_root = Path(repository_path).expanduser().resolve()
        run: MigrationRun | None = None
        task: Task | None = None
        verification_result: BuildVerificationResult | None = None
        verification_decision = None
        repository: GitRepository | None = None
        try:
            if dry_run:
                if not repository_root.is_dir():
                    raise MigrationRunError(
                        f"Repository path is not a directory: {repository_root}"
                    )
                validation_root = repository_root
            else:
                repository = GitRepository.from_path(repository_root)
                validation_root = repository.root
            candidate, plan = self._validate_inputs(
                validation_root, service_name, plan_override=plan_override
            )
            task = self._make_task(plan, candidate)
            run = MigrationRun(
                repository_root=validation_root,
                selected_service=candidate.name,
                task_id=task.id,
                project_id=task.project_id,
                max_debug_attempts=max_debug_attempts,
            )
            if not dry_run:
                self._persist_state(task, run)
            if dry_run:
                run.current_stage = "validated"
                run.warnings.append(
                    "Dry run only: no worktree, model call, application write, or run artifact."
                )
                return MigrationRunResult(run=run, task=task, dry_run=True)

            if repository is None:
                raise MigrationRunError("Real migration execution requires a Git repository")
            run.status = MigrationRunStatus.PREPARING_WORKSPACE
            run.current_stage = "preparing_workspace"
            manager = GitWorktreeManager(repository)
            with self._measure("worktree_creation"):
                workspace, reused = self._get_or_create_workspace(manager, task)
            run.worktree_path = workspace.workspace_path
            run.record(
                MigrationRunEventType.WORKTREE_CREATED,
                "preparing_workspace",
                {"reused": reused},
            )
            self._persist_run(run, task)
            run.status = MigrationRunStatus.EXTRACTING
            run.current_stage = "extracting"
            run.record(
                MigrationRunEventType.EXTRACTION_STARTED,
                "extracting",
                {"service": candidate.name},
            )
            self._persist_run(run, task)
            with self._measure("task_execution"):
                extraction_result = self._execute_extraction(
                    task, workspace, candidate.name, plan
                )
            extraction_data = extraction_result.metadata.get("extraction_result", {})
            generated_files = extraction_data.get("changed_files", [])
            run.generated_files = [str(path) for path in generated_files]
            run.record(
                MigrationRunEventType.EXTRACTION_COMPLETED,
                "extracting",
                {"generated_file_count": len(run.generated_files)},
            )
            self._persist_run(run, task)

            coordinator = VerificationCoordinator(repository.root)
            classifier = FailureClassifier()
            repair_was_eligible = False
            while True:
                stage = "verifying_build"
                run.status = MigrationRunStatus.VERIFYING_BUILD
                run.current_stage = stage
                first_build = run.debug_attempts == 0
                run.record(
                    MigrationRunEventType.BUILD_STARTED
                    if first_build
                    else MigrationRunEventType.REVERIFY_STARTED,
                    stage,
                    {"verification_root": f"services/{service_slug(candidate.name)}"},
                )
                self._persist_run(run, task)
                with self._measure("maven_build"):
                    verification_result, evidence = self._verify_build(
                        task,
                        workspace,
                        candidate.name,
                        timeout_seconds,
                    )
                build_passed = verification_result is not None and (
                    verification_result.status is VerificationStatus.PASSED
                )
                if first_build:
                    result_event = (
                        MigrationRunEventType.BUILD_PASSED
                        if build_passed
                        else MigrationRunEventType.BUILD_FAILED
                    )
                else:
                    result_event = (
                        MigrationRunEventType.REVERIFY_PASSED
                        if build_passed
                        else MigrationRunEventType.REVERIFY_FAILED
                    )
                run.record(
                    result_event,
                    stage,
                    {
                        "status": evidence.build_status.value,
                        "duration_ms": verification_result.duration_ms
                        if verification_result
                        else None,
                    },
                )
                self._persist_run(run, task)
                if build_passed:
                    break
                eligibility = classifier.classify(verification_result, evidence)
                run.warnings.append(f"Verification failure category: {eligibility.category.value}.")
                if not eligibility.eligible:
                    run.failure_reason = eligibility.reason
                    break
                repair_was_eligible = True
                if run.debug_attempts >= run.max_debug_attempts:
                    run.record(
                        MigrationRunEventType.DEBUG_ATTEMPTS_EXHAUSTED,
                        stage,
                        {"attempts": run.debug_attempts, "category": eligibility.category.value},
                    )
                    self._persist_run(run, task)
                    run.failure_reason = (
                        f"Debug attempts exhausted after {run.debug_attempts} attempt(s)."
                    )
                    break
                run.debug_attempts += 1
                run.record(
                    MigrationRunEventType.DEBUG_STARTED,
                    "debugging",
                    {"attempt": run.debug_attempts, "category": eligibility.category.value},
                )
                self._persist_run(run, task)
                try:
                    with self._measure("repair"):
                        debug_result = self._execute_debug(
                            task,
                            workspace,
                            candidate.name,
                            run.debug_attempts,
                            verification_result,
                            evidence,
                        )
                except Exception as error:
                    run.record(
                        MigrationRunEventType.DEBUG_FAILED,
                        "debugging",
                        {
                            "attempt": run.debug_attempts,
                            "error_type": type(error).__name__,
                        },
                    )
                    run.failure_reason = f"Debug attempt failed: {type(error).__name__}."
                    break
                run.record(
                    MigrationRunEventType.DEBUG_APPLIED,
                    "debugging",
                    {
                        "attempt": run.debug_attempts,
                        "changed_files": debug_result.metadata.get("changed_files", []),
                    },
                )
                self._persist_run(run, task)

            run.status = MigrationRunStatus.DECIDING
            run.current_stage = "deciding"
            with self._measure("verification"):
                verification_decision = coordinator.decide(task, evidence)
            run.verification_status = verification_decision.decision.value
            run.final_task_status = task.status
            if verification_decision.decision is VerificationDecision.PASSED:
                run.status = MigrationRunStatus.COMPLETED
                run.current_stage = "completed"
                run.record(
                    MigrationRunEventType.VERIFICATION_PASSED,
                    "deciding",
                    {"task_status": task.status.value},
                )
            elif verification_decision.decision is VerificationDecision.FAILED:
                run.status = (
                    MigrationRunStatus.HUMAN_REVIEW
                    if run.debug_attempts >= run.max_debug_attempts
                    and run.max_debug_attempts > 0
                    and repair_was_eligible
                    else MigrationRunStatus.FAILED
                )
                run.current_stage = (
                    "human_review" if run.status is MigrationRunStatus.HUMAN_REVIEW else "failed"
                )
                run.failure_reason = "; ".join(verification_decision.reasons)
                run.record(
                    MigrationRunEventType.VERIFICATION_FAILED,
                    "deciding",
                    {"task_status": task.status.value},
                )
                if run.status is MigrationRunStatus.HUMAN_REVIEW:
                    run.record(
                        MigrationRunEventType.HUMAN_REVIEW_REQUIRED,
                        "deciding",
                        {"task_status": task.status.value},
                    )
            else:
                run.status = MigrationRunStatus.HUMAN_REVIEW
                run.current_stage = "human_review"
                run.warnings.extend(verification_decision.warnings)
                run.record(
                    MigrationRunEventType.HUMAN_REVIEW_REQUIRED,
                    "deciding",
                    {"task_status": task.status.value},
                )
            run.completed_at = _utc_now()
            self._persist_state(task, run)
            self._write_run_artifact(run, verification_result, verification_decision)
            return MigrationRunResult(
                run=run,
                task=task,
                verification_result=verification_result,
                verification_decision=verification_decision,
            )
        except Exception as error:
            if run is None:
                run = MigrationRun(
                    repository_root=repository_root,
                    selected_service=service_name,
                    task_id=task.id if task else UUID(int=0),
                )
            run.status = MigrationRunStatus.FAILED
            run.current_stage = "failed"
            run.failure_reason = str(error)
            run.completed_at = _utc_now()
            if task is not None:
                self._fail_task(task)
                run.final_task_status = task.status
                self._persist_state(task, run)
            if repository is not None and not dry_run:
                self._write_run_artifact(run, verification_result, verification_decision)
            logger.warning(
                "migration_run_failed", run_id=str(run.run_id), error=redact_text(str(error))[:500]
            )
            return MigrationRunResult(
                run=run,
                task=task,
                verification_result=verification_result,
                verification_decision=verification_decision,
                dry_run=dry_run,
            )

    @staticmethod
    def _fail_task(task: Task) -> None:
        """Reach FAILED through the centralized lifecycle when coordination aborts."""
        if task.status is TaskStatus.PENDING:
            TaskStateMachine.transition(task, TaskStatus.READY)
        if task.status is TaskStatus.READY:
            TaskStateMachine.transition(task, TaskStatus.RUNNING)
        if task.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
            TaskStateMachine.transition(task, TaskStatus.FAILED)

    def _persist_state(self, task: Task, run: MigrationRun) -> None:
        if self.state_service is None:
            return
        project = Project(
            id=task.project_id,
            name=run.selected_service,
            repository_path=run.repository_root,
            created_at=run.started_at,
            updated_at=run.completed_at or _utc_now(),
        )
        self.state_service.persist_project(project)
        self.state_service.persist_task(task)
        self.state_service.persist_run(run, project_id=task.project_id)

    def _persist_run(self, run: MigrationRun, task: Task) -> None:
        if self.state_service is not None:
            self.state_service.persist_run(run, project_id=task.project_id)

    def _measure(self, name: str) -> Any:
        if self.performance is None:
            from contextlib import nullcontext

            return nullcontext()
        return self.performance.stage(name)

    def _execute_extraction(
        self,
        task: Task,
        workspace: TaskWorkspace,
        service_name: str,
        plan: MigrationPlan,
    ) -> Any:
        """Run extraction through TaskScheduler, AgentRegistry, and WorkerRuntime."""
        extraction_agent = self.extraction_agent or ServiceExtractionAgent()
        graph = TaskGraph([task])
        scheduler = TaskScheduler(graph)
        scheduler.schedule()
        registry = AgentRegistry()
        registry.register(extraction_agent)
        context = AgentContext(
            project_id=task.project_id,
            task=task,
            workspace_path=str(workspace.workspace_path),
            metadata={
                "service_name": service_name,
                "evidence_root": str(workspace.repository_root),
                "migration_plan": plan.model_dump(mode="json"),
            },
        )
        result = WorkerRuntime(scheduler, registry).execute(task, context)
        if self.state_service is not None:
            self.state_service.persist_agent_result(result)
            self.state_service.persist_task(task)
        return result

    def _execute_debug(
        self,
        task: Task,
        workspace: TaskWorkspace,
        service_name: str,
        attempt: int,
        verification_result: BuildVerificationResult | None,
        evidence: VerificationEvidence,
    ) -> Any:
        """Apply one bounded repair without changing task lifecycle state."""
        agent = self.debug_agent or DebugAgent()
        service_directory = f"services/{service_slug(service_name)}"
        changed_files = list(verification_result.changed_files) if verification_result else []
        relevant_files = [
            path
            for path in changed_files
            if path.replace("\\", "/").startswith(f"{service_directory}/")
        ]
        failure_evidence: dict[str, Any] = {
            "build_status": evidence.build_status.value,
            "build_exit_code": evidence.build_exit_code,
            "tests_run": evidence.tests_run,
            "test_failures": evidence.test_failures,
            "test_errors": evidence.test_errors,
            "warnings": evidence.warnings[:8],
        }
        if verification_result is not None:
            failure_evidence.update(
                {
                    "stdout_summary": verification_result.stdout_summary[:1200],
                    "stderr_summary": verification_result.stderr_summary[:1200],
                }
            )
        context = AgentContext(
            project_id=task.project_id,
            task=task,
            workspace_path=str(workspace.workspace_path),
            metadata={
                "debug_attempt": attempt,
                "service_name": service_name,
                "target_service_directory": service_directory,
                "failure_evidence": failure_evidence,
                "changed_files": changed_files,
                "relevant_files": relevant_files,
            },
        )
        return agent.execute(task, context)

    def _verify_build(
        self,
        task: Task,
        workspace: TaskWorkspace,
        service_name: str,
        timeout_seconds: float,
    ) -> tuple[BuildVerificationResult | None, VerificationEvidence]:
        """Run safe subproject verification and normalize even execution errors."""
        agent = self.build_agent or BuildVerificationAgent(timeout_seconds=timeout_seconds)
        context = AgentContext(
            project_id=task.project_id,
            task=task,
            workspace_path=str(workspace.workspace_path),
            metadata={"verification_root": f"services/{service_slug(service_name)}"},
        )
        try:
            result = agent.execute(task, context)
            if self.state_service is not None:
                self.state_service.persist_agent_result(result)
        except BuildVerificationError as error:
            return None, VerificationEvidence(
                task_id=task.id,
                build_status=VerificationStatus.ERROR,
                warnings=[str(error)],
            )
        verification_payload = result.metadata.get("verification_result")
        if not isinstance(verification_payload, dict):
            raise MigrationRunError("Build verification returned no normalized result")
        build_result = BuildVerificationResult.model_validate(verification_payload)
        evidence = VerificationEvidence.from_build_result(
            build_result,
            list(result.artifacts),
        )
        return build_result, evidence

    @staticmethod
    def _get_or_create_workspace(
        manager: GitWorktreeManager,
        task: Task,
    ) -> tuple[TaskWorkspace, bool]:
        """Reuse a managed clean worktree or create the task-owned one."""
        try:
            return manager.task_workspace(task.id), True
        except UnknownWorktreeError:
            worktree = manager.create_worktree(task)
            return manager.task_workspace(worktree.task_id), False

    @staticmethod
    def _validate_inputs(
        repository_root: Path,
        service_name: str,
        *,
        plan_override: MigrationPlan | None = None,
    ) -> tuple[CandidateService, MigrationPlan]:
        """Validate candidate, plan, graph, architecture, and source paths."""
        boundary = _load_artifact(
            repository_root / SERVICE_BOUNDARIES_JSON_ARTIFACT,
            ServiceBoundaryReport,
        )
        candidate = _find_candidate(boundary, service_name)
        if candidate is None:
            raise MigrationRunError(f"Selected candidate service is not present: {service_name}")
        plan = plan_override or _load_artifact(
            repository_root / MIGRATION_PLAN_JSON_ARTIFACT, MigrationPlan
        )
        if _candidate_key(plan.candidate_service) != _candidate_key(candidate.name):
            raise MigrationRunError("Migration plan candidate does not match selected service")
        if not any(step.task_type is TaskType.TEST for step in plan.steps):
            raise MigrationRunError("Migration plan must include a testing step")
        if not any(step.task_type is TaskType.VERIFY for step in plan.steps):
            raise MigrationRunError("Migration plan must include a verification step")
        MigrationPlanningAgent.to_tasks(plan)
        graph = _load_artifact(repository_root / JAVA_DEPENDENCY_ARTIFACT, JavaDependencyGraph)
        _load_artifact(repository_root / ARCHITECTURE_REPORT_ARTIFACT, ArchitectureReport)
        classes = {item.fully_qualified_name: item for item in graph.classes}
        for class_name in candidate.classes:
            java_class = classes.get(class_name)
            if java_class is None:
                raise MigrationRunError(
                    "Candidate class is absent from dependency evidence: " f"{class_name}"
                )
            source_path = (repository_root / _safe_relative_path(java_class.file_path)).resolve()
            try:
                source_path.relative_to(repository_root)
            except ValueError as error:
                raise MigrationRunError("Candidate source path escapes the repository") from error
            if not source_path.is_file():
                raise MigrationRunError(f"Candidate source file is missing: {java_class.file_path}")
        return candidate, plan

    @staticmethod
    def _make_task(plan: MigrationPlan, candidate: CandidateService) -> Task:
        """Create a stable extraction task for this plan and candidate."""
        project_id = uuid5(plan.plan_id, "migration-project")
        task_id = uuid5(plan.plan_id, f"service-extraction:{_candidate_key(candidate.name)}")
        matching_steps = [
            step for step in plan.steps if step.task_type is TaskType.SERVICE_EXTRACTION
        ]
        if matching_steps:
            step = matching_steps[0]
            return Task(
                id=task_id,
                project_id=project_id,
                task_type=TaskType.SERVICE_EXTRACTION,
                title=step.title,
                description=step.description,
                assigned_agent=ServiceExtractionAgent.name,
            )
        return Task(
            id=task_id,
            project_id=project_id,
            task_type=TaskType.SERVICE_EXTRACTION,
            title=f"Extract {candidate.name}",
            description="Copy the approved candidate into an isolated service.",
            assigned_agent=ServiceExtractionAgent.name,
        )

    @staticmethod
    def _write_run_artifact(
        run: MigrationRun,
        verification_result: BuildVerificationResult | None,
        verification_decision: Any,
    ) -> str:
        """Persist bounded run metadata without source, logs, or credentials."""
        artifact = run.repository_root / RUNS_DIR / f"{run.run_id}.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        verification_payload: dict[str, Any] | None = None
        if verification_result is not None:
            verification_payload = {
                "status": verification_result.status.value,
                "build_system": verification_result.build_system.value,
                "exit_code": verification_result.exit_code,
                "duration_ms": verification_result.duration_ms,
                "test_summary": verification_result.test_summary.model_dump(mode="json"),
                "warnings": verification_result.warnings,
            }
        payload = {
            "run_id": str(run.run_id),
            "repository_root": str(run.repository_root),
            "selected_service": run.selected_service,
            "task_id": str(run.task_id),
            "worktree_path": str(run.worktree_path) if run.worktree_path else None,
            "started_at": run.started_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "status": run.status.value,
            "current_stage": run.current_stage,
            "stages": [event.model_dump(mode="json") for event in run.events],
            "generated_files": run.generated_files,
            "debug_attempts": run.debug_attempts,
            "max_debug_attempts": run.max_debug_attempts,
            "verification_status": run.verification_status,
            "verification_result": verification_payload,
            "final_task_status": (run.final_task_status.value if run.final_task_status else None),
            "warnings": run.warnings,
            "failure_reason": run.failure_reason,
        }
        if verification_decision is not None:
            payload["verification_decision"] = {
                "decision": verification_decision.decision.value,
                "reasons": verification_decision.reasons,
                "warnings": verification_decision.warnings,
            }
        artifact.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return str(artifact.relative_to(run.repository_root).as_posix())


def _utc_now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _load_artifact(path: Path, model_type: type[BaseModel]) -> Any:
    try:
        return model_type.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise MigrationRunError(f"Could not load migration evidence {path}: {error}") from error


def _candidate_key(value: str) -> str:
    return value.strip().casefold().removesuffix(" service").strip()


def _find_candidate(
    report: ServiceBoundaryReport,
    requested: str,
) -> CandidateService | None:
    requested_key = _candidate_key(requested)
    return next(
        (
            candidate
            for candidate in report.candidate_services
            if _candidate_key(candidate.name) == requested_key
        ),
        None,
    )


def _safe_relative_path(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or raw.startswith("/") or ".." in path.parts:
        raise MigrationRunError(f"Unsafe evidence source path: {value}")
    if any(part.lower() in {".git", ".migrationswarm"} for part in path.parts):
        raise MigrationRunError(f"Protected evidence source path: {value}")
    return path.as_posix()


__all__ = ["MigrationOrchestrator", "RUNS_DIR"]
