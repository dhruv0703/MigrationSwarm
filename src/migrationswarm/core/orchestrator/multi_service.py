"""Bounded orchestration for several explicitly approved services."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from enum import StrEnum
from heapq import heappop, heappush
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from migrationswarm.agents.build_verification import BuildVerificationResult
from migrationswarm.agents.migration_planning import MigrationPlan
from migrationswarm.agents.service_boundary import (
    CandidateService,
    ServiceBoundaryReport,
)
from migrationswarm.agents.service_extraction import service_slug
from migrationswarm.core.observability.performance import PerformanceCollector
from migrationswarm.core.orchestrator.models import MigrationRunResult, MigrationRunStatus

SERVICE_APPROVALS_ARTIFACT = ".migrationswarm/service-approvals.json"
MULTI_RUNS_DIR = ".migrationswarm/multi-runs"
MIGRATION_PLANS_DIR = ".migrationswarm/migration-plans"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MultiServiceMigrationError(ValueError):
    """Base exception for multi-service input and orchestration errors."""


class UnknownServiceError(MultiServiceMigrationError):
    """Raised when a selected or approved service is absent from evidence."""


class UnapprovedServiceError(MultiServiceMigrationError):
    """Raised when an actual migration includes an unapproved service."""


class MissingServicePlanError(MultiServiceMigrationError):
    """Raised or recorded when a selected service has no grounded plan."""


class ServiceDependencyCycleError(MultiServiceMigrationError):
    """Raised internally when selected service dependencies contain a cycle."""


class MultiServiceMigrationStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    PARTIALLY_COMPLETED = "partially_completed"
    COMPLETED = "completed"
    FAILED = "failed"
    HUMAN_REVIEW = "human_review"


class ServiceMigrationStateStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    RUNNING = "running"
    VERIFYING = "verifying"
    REPAIRING = "repairing"
    COMPLETED = "completed"
    FAILED = "failed"
    HUMAN_REVIEW = "human_review"


class ServiceMigrationDependency(BaseModel):
    """A directed edge: ``service_name`` depends on ``depends_on``."""

    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(min_length=1)
    depends_on: str = Field(min_length=1)


class ServiceMigrationState(BaseModel):
    """Durable summary of one service branch in a multi-service run."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    service_name: str = Field(min_length=1)
    service_slug: str = Field(min_length=1)
    task_ids: list[UUID] = Field(default_factory=list)
    worktree_path: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    status: ServiceMigrationStateStatus = ServiceMigrationStateStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    verification_result: dict[str, Any] | None = None
    repair_attempts: int = Field(default=0, ge=0, le=3)
    warnings: list[str] = Field(default_factory=list)
    failure_reason: str | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("service migration timestamp must be timezone-aware")
        return value.astimezone(UTC)


class MultiServiceMigrationRun(BaseModel):
    """Persistable state for one bounded multi-service migration."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    repository_root: Path
    selected_services: list[str] = Field(min_length=1)
    dependencies: list[ServiceMigrationDependency] = Field(default_factory=list)
    services: list[ServiceMigrationState] = Field(min_length=1)
    status: MultiServiceMigrationStatus = MultiServiceMigrationStatus.PENDING
    started_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("migration run timestamp must be timezone-aware")
        return value.astimezone(UTC)


class MultiServiceMigrationResult(BaseModel):
    """Result plus dry-run planning information for the CLI and tests."""

    model_config = ConfigDict(extra="forbid")

    run: MultiServiceMigrationRun
    rejected_services: list[str] = Field(default_factory=list)
    approved_services: list[str] = Field(default_factory=list)
    root_services: list[str] = Field(default_factory=list)
    planned_service_workers: int = Field(default=2, ge=1, le=3)
    estimated_model_stages: dict[str, int] = Field(default_factory=dict)
    dry_run: bool = False


class ServiceApprovalArtifact(BaseModel):
    """Explicit local approval metadata; candidates are never auto-approved."""

    model_config = ConfigDict(extra="forbid")

    approved: list[str] = Field(default_factory=list)


class _ServiceDependencyGraph:
    def __init__(
        self,
        dependencies: Iterable[ServiceMigrationDependency],
        nodes: Iterable[str] = (),
    ) -> None:
        self.dependencies: dict[str, tuple[str, ...]] = {}
        for node in nodes:
            self.dependencies.setdefault(node, tuple())
        for edge in dependencies:
            self.dependencies.setdefault(edge.service_name, tuple())
            self.dependencies[edge.service_name] = tuple(
                sorted(
                    {*self.dependencies[edge.service_name], edge.depends_on},
                    key=str.casefold,
                )
            )
            self.dependencies.setdefault(edge.depends_on, tuple())

    def cycle(self) -> tuple[str, ...] | None:
        visiting: set[str] = set()
        visited: set[str] = set()
        path: list[str] = []

        def visit(name: str) -> tuple[str, ...] | None:
            if name in visiting:
                return tuple(path[path.index(name) :] + [name])
            if name in visited:
                return None
            visiting.add(name)
            path.append(name)
            for dependency in self.dependencies.get(name, ()):
                result = visit(dependency)
                if result:
                    return result
            path.pop()
            visiting.remove(name)
            visited.add(name)
            return None

        for name in sorted(self.dependencies, key=str.casefold):
            result = visit(name)
            if result:
                return result
        return None

    def topological_order(self) -> tuple[str, ...]:
        if self.cycle():
            raise ServiceDependencyCycleError("Selected service dependencies contain a cycle")
        indegree = {name: len(values) for name, values in self.dependencies.items()}
        dependents: dict[str, list[str]] = {name: [] for name in self.dependencies}
        for name, values in self.dependencies.items():
            for dependency in values:
                dependents[dependency].append(name)
        queue: list[tuple[str, str]] = []
        for name, count in indegree.items():
            if count == 0:
                heappush(queue, (name.casefold(), name))
        ordered: list[str] = []
        while queue:
            _, name = heappop(queue)
            ordered.append(name)
            for dependent in sorted(dependents[name], key=str.casefold):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heappush(queue, (dependent.casefold(), dependent))
        if len(ordered) != len(self.dependencies):
            raise ServiceDependencyCycleError("Selected service dependencies contain a cycle")
        return tuple(ordered)

    def roots(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                (name for name, values in self.dependencies.items() if not values),
                key=str.casefold,
            )
        )


WorkflowFactory = Callable[[], Any]


class MultiServiceMigrationOrchestrator:
    """Schedule independent service workflows while preserving service isolation."""

    def __init__(
        self,
        *,
        workflow_factory: WorkflowFactory | None = None,
        run_repository: Any | None = None,
        performance: PerformanceCollector | None = None,
    ) -> None:
        self.workflow_factory = workflow_factory
        self.run_repository = run_repository
        self.performance = performance

    def run(
        self,
        repository_path: str | Path,
        *,
        services: Sequence[str] | None = None,
        all_approved: bool = False,
        service_workers: int = 2,
        timeout_seconds: float = 300.0,
        max_debug_attempts: int = 2,
        dry_run: bool = False,
    ) -> MultiServiceMigrationResult:
        if not 1 <= service_workers <= 3:
            raise ValueError("service_workers must be between 1 and 3")
        root = Path(repository_path).expanduser().resolve()
        candidates = self._load_candidates(root)
        by_key = {_candidate_key(item.name): item for item in candidates}
        approved = self._load_approvals(root, by_key)
        selected, rejected = self._select_services(
            services or [], all_approved, approved, by_key
        )
        if rejected and not dry_run:
            raise UnapprovedServiceError(
                "Unapproved services cannot be migrated: " + ", ".join(rejected)
            )
        if not selected and not dry_run:
            raise MultiServiceMigrationError("At least one approved service must be selected")
        if not selected and dry_run:
            selected = rejected

        plans: dict[str, MigrationPlan] = {}
        states: list[ServiceMigrationState] = []
        warnings: list[str] = []
        selected_candidates = {item.name: item for item in candidates if item.name in selected}
        edges = self._dependencies(selected_candidates.values(), selected_candidates)
        graph = _ServiceDependencyGraph(edges, selected)
        cycle = graph.cycle()
        cycle_names = set(cycle or ())
        missing_dependencies = {
            candidate.name: sorted(
                (
                    dependency
                    for dependency in candidate.dependencies_on_other_candidates
                    if _candidate_key(dependency) not in {
                        _candidate_key(name) for name in selected_candidates
                    }
                ),
                key=str.casefold,
            )
            for candidate in selected_candidates.values()
        }
        missing_dependencies = {
            name: values for name, values in missing_dependencies.items() if values
        }
        if cycle:
            warnings.append("Service dependency cycle: " + " -> ".join(cycle))

        for name in selected:
            candidate = selected_candidates[name]
            plan = self._load_plan(root, candidate.name)
            if plan is not None:
                plans[name] = plan
            state = ServiceMigrationState(
                service_name=name,
                service_slug=service_slug(name),
                task_ids=[_planned_task_id(plan, candidate.name)] if plan else [],
                worktree_path=(
                    str(root / ".migrationswarm" / "worktrees" / str(_planned_task_id(plan, name)))
                    if plan
                    else None
                ),
                dependencies=[edge.depends_on for edge in edges if edge.service_name == name],
                status=(
                    ServiceMigrationStateStatus.HUMAN_REVIEW
                    if name in cycle_names or name in missing_dependencies
                    else ServiceMigrationStateStatus.PENDING
                ),
            )
            if plan is None:
                state.status = ServiceMigrationStateStatus.HUMAN_REVIEW
                state.failure_reason = f"Missing migration plan for {name}."
                warnings.append(state.failure_reason)
            if name in missing_dependencies:
                state.failure_reason = (
                    f"Dependencies not selected: {', '.join(missing_dependencies[name])}."
                )
                warnings.append(state.failure_reason)
            if name in rejected:
                state.status = ServiceMigrationStateStatus.HUMAN_REVIEW
                state.failure_reason = f"Service {name} is not explicitly approved."
                warnings.append(state.failure_reason)
            states.append(state)

        run = MultiServiceMigrationRun(
            run_id=uuid5(
                NAMESPACE_URL,
                f"migrationswarm:multi:{root}:{datetime.now(UTC).isoformat()}",
            ),
            project_id=uuid5(NAMESPACE_URL, f"migrationswarm:project:{root}"),
            repository_root=root,
            selected_services=selected,
            dependencies=edges,
            services=states,
            status=MultiServiceMigrationStatus.PLANNING,
            warnings=warnings,
        )
        roots = list(graph.roots())
        result = MultiServiceMigrationResult(
            run=run,
            rejected_services=rejected,
            approved_services=approved,
            root_services=roots,
            planned_service_workers=service_workers,
            estimated_model_stages={name: 1 for name in selected},
            dry_run=dry_run,
        )
        self._persist_run(run)
        if dry_run:
            run.warnings.append(
                "Dry run only: no worktrees, model calls, or source modifications."
            )
            return result
        if cycle:
            run.status = MultiServiceMigrationStatus.HUMAN_REVIEW
            run.completed_at = _utc_now()
            self._write_artifacts(run)
            self._persist_run(run)
            return result

        run.status = MultiServiceMigrationStatus.RUNNING
        self._execute(
            run,
            plans,
            service_workers,
            timeout_seconds,
            max_debug_attempts,
        )
        run.completed_at = _utc_now()
        run.status = self._overall_status(run)
        self._validate_worktree_isolation(run)
        self._write_artifacts(run)
        self._persist_run(run)
        return result

    def _persist_run(self, run: MultiServiceMigrationRun) -> None:
        if self.run_repository is None:
            return
        existing = self.run_repository.get(run.run_id)
        if existing is None:
            self.run_repository.create(run)
        else:
            self.run_repository.update(run)

    @staticmethod
    def _load_candidates(root: Path) -> list[CandidateService]:
        path = root / ".migrationswarm" / "service-boundaries.json"
        try:
            report = ServiceBoundaryReport.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception as error:
            raise MultiServiceMigrationError(
                f"Could not load service boundary evidence: {error}"
            ) from error
        return report.candidate_services

    @staticmethod
    def _load_approvals(root: Path, by_key: dict[str, CandidateService]) -> list[str]:
        path = root / SERVICE_APPROVALS_ARTIFACT
        if not path.is_file():
            return []
        try:
            artifact = ServiceApprovalArtifact.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception as error:
            raise MultiServiceMigrationError(
                f"Invalid service approval artifact: {error}"
            ) from error
        canonical: list[str] = []
        for value in artifact.approved:
            candidate = by_key.get(_candidate_key(value))
            if candidate is None:
                raise UnknownServiceError(f"Approved service is not a known candidate: {value}")
            if candidate.name not in canonical:
                canonical.append(candidate.name)
        return sorted(canonical, key=str.casefold)

    @staticmethod
    def _select_services(
        requested: Sequence[str],
        all_approved: bool,
        approved: list[str],
        by_key: dict[str, CandidateService],
    ) -> tuple[list[str], list[str]]:
        if all_approved and requested:
            raise MultiServiceMigrationError("Use --all-approved or --service, not both")
        values = approved if all_approved else list(requested)
        selected: list[str] = []
        rejected: list[str] = []
        for value in values:
            candidate = by_key.get(_candidate_key(value))
            if candidate is None:
                raise UnknownServiceError(f"Selected service is not a known candidate: {value}")
            if candidate.name not in approved:
                rejected.append(candidate.name)
            elif candidate.name not in selected:
                selected.append(candidate.name)
        return selected, rejected

    @staticmethod
    def _dependencies(
        candidates: Iterable[CandidateService],
        selected: dict[str, CandidateService],
    ) -> list[ServiceMigrationDependency]:
        result: list[ServiceMigrationDependency] = []
        selected_keys = {_candidate_key(name): name for name in selected}
        for candidate in candidates:
            for dependency in candidate.dependencies_on_other_candidates:
                dependency_name = selected_keys.get(_candidate_key(dependency))
                if dependency_name is not None:
                    result.append(
                        ServiceMigrationDependency(
                            service_name=candidate.name,
                            depends_on=dependency_name,
                        )
                    )
        return sorted(
            result,
            key=lambda item: (item.service_name.casefold(), item.depends_on.casefold()),
        )

    @staticmethod
    def _load_plan(root: Path, service: str) -> MigrationPlan | None:
        slug = service_slug(service)
        paths = [
            root / MIGRATION_PLANS_DIR / f"{slug}.json",
            root / ".migrationswarm" / f"migration-plan-{slug}.json",
            root / ".migrationswarm" / "migration-plan.json",
        ]
        for path in paths:
            if not path.is_file():
                continue
            try:
                plan = MigrationPlan.model_validate(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except Exception as error:
                raise MultiServiceMigrationError(
                    f"Invalid migration plan {path}: {error}"
                ) from error
            if _candidate_key(plan.candidate_service) == _candidate_key(service):
                return plan
        return None

    def _execute(
        self,
        run: MultiServiceMigrationRun,
        plans: dict[str, MigrationPlan],
        service_workers: int,
        timeout_seconds: float,
        max_debug_attempts: int,
    ) -> None:
        states = {state.service_name: state for state in run.services}
        futures: dict[Future[MigrationRunResult], str] = {}
        if self.performance is not None:
            for state in states.values():
                self.performance.service_queued(state.service_name)
        with ThreadPoolExecutor(
            max_workers=service_workers,
            thread_name_prefix="migrationswarm-service",
        ) as pool:
            while True:
                self._block_unrunnable(states)
                available = [
                    state
                    for state in states.values()
                    if state.status is ServiceMigrationStateStatus.PENDING
                    and self._dependencies_completed(state, states)
                    and state.service_name in plans
                ]
                available.sort(key=lambda state: state.service_name.casefold())
                while len(futures) < service_workers and available:
                    state = available.pop(0)
                    state.status = ServiceMigrationStateStatus.RUNNING
                    state.started_at = _utc_now()
                    if self.performance is not None:
                        self.performance.service_started(state.service_name)
                    future = pool.submit(
                        self._run_one,
                        run.repository_root,
                        state.service_name,
                        plans[state.service_name],
                        timeout_seconds,
                        max_debug_attempts,
                    )
                    futures[future] = state.service_name
                if not futures:
                    if not any(
                        state.status is ServiceMigrationStateStatus.PENDING
                        for state in states.values()
                    ):
                        break
                    for state in states.values():
                        if state.status is ServiceMigrationStateStatus.PENDING:
                            state.status = ServiceMigrationStateStatus.HUMAN_REVIEW
                            state.failure_reason = "Dependency scheduling could not make progress."
                    break
                if self.performance is not None:
                    self.performance.observe_queue_depth(len(futures) + len(available))
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: futures[item].casefold()):
                    name = futures.pop(future)
                    state = states[name]
                    outcome: MigrationRunResult | None = None
                    try:
                        outcome = future.result()
                    except Exception as error:
                        state.status = ServiceMigrationStateStatus.FAILED
                        state.failure_reason = f"Service workflow raised {type(error).__name__}."
                        state.warnings.append(str(error)[:500])
                    else:
                        self._apply_outcome(state, outcome)
                    if self.performance is not None:
                        verification_ms = 0.0
                        if outcome is not None and outcome.verification_result is not None:
                            verification_ms = outcome.verification_result.duration_ms
                        self.performance.service_finished(
                            name,
                            completed=state.status is ServiceMigrationStateStatus.COMPLETED,
                            verification_ms=verification_ms,
                            repair_ms=float(outcome.run.debug_attempts) if outcome else 0.0,
                        )
                    state.completed_at = _utc_now()
                    self._persist_run(run)

    def _run_one(
        self,
        root: Path,
        service: str,
        plan: MigrationPlan,
        timeout_seconds: float,
        max_debug_attempts: int,
    ) -> MigrationRunResult:
        workflow = self.workflow_factory() if self.workflow_factory else None
        if workflow is None:
            from migrationswarm.core.orchestrator.orchestrator import MigrationOrchestrator

            workflow = MigrationOrchestrator()
        return workflow.run(
            root,
            service,
            timeout_seconds=timeout_seconds,
            max_debug_attempts=max_debug_attempts,
            plan_override=plan,
        )

    @staticmethod
    def _apply_outcome(state: ServiceMigrationState, outcome: MigrationRunResult) -> None:
        state.task_ids = [outcome.task.id] if outcome.task is not None else state.task_ids
        state.worktree_path = (
            str(outcome.run.worktree_path) if outcome.run.worktree_path else state.worktree_path
        )
        state.repair_attempts = outcome.run.debug_attempts
        state.warnings.extend(outcome.run.warnings)
        state.failure_reason = outcome.run.failure_reason
        state.verification_result = _verification_summary(outcome.verification_result)
        if outcome.run.status is MigrationRunStatus.COMPLETED:
            state.status = ServiceMigrationStateStatus.COMPLETED
        elif outcome.run.status is MigrationRunStatus.HUMAN_REVIEW:
            state.status = ServiceMigrationStateStatus.HUMAN_REVIEW
        else:
            state.status = ServiceMigrationStateStatus.FAILED

    @staticmethod
    def _dependencies_completed(
        state: ServiceMigrationState,
        states: dict[str, ServiceMigrationState],
    ) -> bool:
        return all(
            states[dependency].status is ServiceMigrationStateStatus.COMPLETED
            for dependency in state.dependencies
            if dependency in states
        ) and all(dependency in states for dependency in state.dependencies)

    @staticmethod
    def _block_unrunnable(states: dict[str, ServiceMigrationState]) -> None:
        for state in states.values():
            if state.status is not ServiceMigrationStateStatus.PENDING:
                continue
            dependencies = [states.get(name) for name in state.dependencies]
            if any(
                dependency is not None
                and dependency.status
                in {
                    ServiceMigrationStateStatus.FAILED,
                    ServiceMigrationStateStatus.HUMAN_REVIEW,
                    ServiceMigrationStateStatus.BLOCKED,
                }
                for dependency in dependencies
            ):
                state.status = ServiceMigrationStateStatus.BLOCKED
                state.failure_reason = "A prerequisite service did not complete."

    @staticmethod
    def _overall_status(run: MultiServiceMigrationRun) -> MultiServiceMigrationStatus:
        statuses = [state.status for state in run.services]
        completed = sum(status is ServiceMigrationStateStatus.COMPLETED for status in statuses)
        if completed == len(statuses):
            return MultiServiceMigrationStatus.COMPLETED
        if completed:
            return MultiServiceMigrationStatus.PARTIALLY_COMPLETED
        if any(status is ServiceMigrationStateStatus.HUMAN_REVIEW for status in statuses):
            return MultiServiceMigrationStatus.HUMAN_REVIEW
        return MultiServiceMigrationStatus.FAILED

    @staticmethod
    def _validate_worktree_isolation(run: MultiServiceMigrationRun) -> None:
        paths = [state.worktree_path for state in run.services if state.worktree_path]
        if len(paths) != len(set(paths)):
            run.status = MultiServiceMigrationStatus.HUMAN_REVIEW
            run.warnings.append("Service migrations resolved to a shared worktree.")

    @staticmethod
    def _write_artifacts(run: MultiServiceMigrationRun) -> None:
        directory = run.repository_root / MULTI_RUNS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        payload = run.model_dump(mode="json")
        (directory / f"{run.run_id}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        lines = [
            f"# Multi-service migration {run.run_id}",
            "",
            f"- Status: **{run.status.value}**",
            f"- Selected services: {', '.join(run.selected_services)}",
            "",
            "## Dependency DAG",
            "",
            render_service_dag(run.dependencies, run.selected_services),
            "",
            "## Services",
            "",
        ]
        for state in run.services:
            lines.append(f"- **{state.service_name}** — {state.status.value}")
            if state.dependencies:
                lines.append(f"  - Depends on: {', '.join(state.dependencies)}")
            if state.repair_attempts:
                lines.append(f"  - Repair attempts: {state.repair_attempts}")
            if state.failure_reason:
                lines.append(f"  - Reason: {state.failure_reason}")
        (directory / f"{run.run_id}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_service_dag(
    dependencies: Sequence[ServiceMigrationDependency], selected: Sequence[str]
) -> str:
    """Render deterministic text showing dependent -> prerequisite direction."""
    by_service: dict[str, list[str]] = {name: [] for name in selected}
    for edge in dependencies:
        by_service.setdefault(edge.service_name, []).append(edge.depends_on)
    lines: list[str] = []
    for service in sorted(by_service, key=str.casefold):
        values = sorted(by_service[service], key=str.casefold)
        lines.append(service if not values else f"{service} -> {', '.join(values)}")
    return "\n".join(lines) or "(no dependencies)"


def _verification_summary(result: BuildVerificationResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "status": result.status.value,
        "build_system": result.build_system.value,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "test_summary": result.test_summary.model_dump(mode="json"),
        "warnings": list(result.warnings),
    }


def _planned_task_id(plan: MigrationPlan | None, service: str) -> UUID:
    if plan is None:
        return uuid5(NAMESPACE_URL, f"migrationswarm:service:{_candidate_key(service)}")
    return uuid5(plan.plan_id, f"service-extraction:{_candidate_key(service)}")


def _candidate_key(value: str) -> str:
    return value.strip().casefold().removesuffix(" service").strip()


__all__ = [
    "MULTI_RUNS_DIR",
    "MIGRATION_PLANS_DIR",
    "SERVICE_APPROVALS_ARTIFACT",
    "MissingServicePlanError",
    "MultiServiceMigrationError",
    "MultiServiceMigrationOrchestrator",
    "MultiServiceMigrationResult",
    "MultiServiceMigrationRun",
    "MultiServiceMigrationStatus",
    "ServiceApprovalArtifact",
    "ServiceDependencyCycleError",
    "ServiceMigrationDependency",
    "ServiceMigrationState",
    "ServiceMigrationStateStatus",
    "UnknownServiceError",
    "UnapprovedServiceError",
    "render_service_dag",
]
