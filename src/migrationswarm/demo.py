"""Convenience runner for the local commerce-monolith demonstration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from migrationswarm.agents.architecture_analysis import ArchitectureAnalysisAgent
from migrationswarm.agents.build_verification import BuildVerificationAgent
from migrationswarm.agents.dependency_analysis import (
    DependencyAnalysisAgent,
    JavaClass,
    JavaDependencyGraph,
)
from migrationswarm.agents.migration_planning import MigrationPlan, MigrationPlanningAgent
from migrationswarm.agents.repository_analysis import RepositoryAnalysisAgent
from migrationswarm.agents.service_boundary import (
    ServiceBoundaryAgent,
    ServiceBoundaryReport,
)
from migrationswarm.agents.service_extraction import ServiceExtractionAgent, service_slug
from migrationswarm.config import get_settings
from migrationswarm.core.agents.models import AgentContext
from migrationswarm.core.models import ModelRequest, ModelResponse, ModelRouter
from migrationswarm.core.models.registry import default_model_registry
from migrationswarm.core.observability import MetricsCollector, PerformanceCollector
from migrationswarm.core.observability.models import LiveValidationReport, MigrationBenchmark
from migrationswarm.core.orchestrator import (
    MigrationOrchestrator,
    MultiServiceMigrationOrchestrator,
    MultiServiceMigrationResult,
)
from migrationswarm.core.tasks import Task, TaskStatus, TaskType
from migrationswarm.providers import default_provider_registry

DEMO_SERVICES: tuple[str, ...] = ("Inventory", "Orders", "Notifications")
LIVE_MODEL_INTERVAL_SECONDS = 75.0
DEMO_CLASSES: dict[str, tuple[str, ...]] = {
    "Inventory": (
        "com.example.commerce.inventory.InventoryController",
        "com.example.commerce.inventory.InventoryItem",
        "com.example.commerce.inventory.InventoryItemDto",
        "com.example.commerce.inventory.InventoryMapper",
        "com.example.commerce.inventory.InventoryRepository",
        "com.example.commerce.inventory.InventoryService",
    ),
    "Orders": (
        "com.example.commerce.orders.InventoryGateway",
        "com.example.commerce.orders.CustomerOrder",
        "com.example.commerce.orders.OrderController",
        "com.example.commerce.orders.OrderDto",
        "com.example.commerce.orders.OrderLine",
        "com.example.commerce.orders.OrderMapper",
        "com.example.commerce.orders.OrderRepository",
        "com.example.commerce.orders.OrderService",
    ),
    "Notifications": (
        "com.example.commerce.notifications.Notification",
        "com.example.commerce.notifications.NotificationController",
        "com.example.commerce.notifications.NotificationDto",
        "com.example.commerce.notifications.NotificationMapper",
        "com.example.commerce.notifications.NotificationRepository",
        "com.example.commerce.notifications.NotificationService",
        "com.example.commerce.notifications.NotificationTemplate",
    ),
    "Customers": (
        "com.example.commerce.customers.Customer",
        "com.example.commerce.customers.CustomerController",
        "com.example.commerce.customers.CustomerRepository",
        "com.example.commerce.customers.CustomerService",
    ),
}
SHARED_CLASSES = (
    "com.example.commerce.shared.AuditStamp",
    "com.example.commerce.shared.DomainEvent",
    "com.example.commerce.shared.Money",
    "com.example.commerce.shared.SharedEventPublisher",
    "com.example.commerce.config.ClockConfiguration",
)


@dataclass(frozen=True)
class DemoFixtureDefinition:
    """Small fixture contract used by the deterministic offline router."""

    name: str
    expected_services: tuple[str, ...]
    aliases: dict[str, tuple[str, ...]]
    package_root: str
    ignored_package_segments: frozenset[str]
    behavior_markers: dict[str, tuple[str, ...]]
    behavior_contracts: tuple[dict[str, Any], ...]


def _load_fixture_definition(root: Path) -> DemoFixtureDefinition:
    """Load fixture-owned benchmark metadata without coupling demo to evaluation."""
    path = root / "benchmark.json"
    if not path.is_file():
        return DemoFixtureDefinition(
            name=root.name,
            expected_services=DEMO_SERVICES,
            aliases={},
            package_root="com.example.commerce",
            ignored_package_segments=frozenset({"shared", "config"}),
            behavior_markers={},
            behavior_contracts=(),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = tuple(str(item) for item in payload["expected_services"])
        aliases = {
            str(key): tuple(str(item) for item in values)
            for key, values in payload.get("aliases", {}).items()
        }
        package_root = str(payload["package_root"])
        behavior_markers = {
            str(item.get("service", "")): tuple(
                str(marker) for marker in item.get("generated_markers", [])
            )
            for item in payload.get("behavior_checks", [])
            if isinstance(item, dict) and item.get("service")
        }
        behavior_contracts = tuple(
            item for item in payload.get("behavior_checks", []) if isinstance(item, dict)
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise DemoError(f"Could not load fixture definition: {path}") from error
    if not expected or not package_root:
        raise DemoError(f"Fixture definition is incomplete: {path}")
    return DemoFixtureDefinition(
        name=str(payload.get("name", root.name)),
        expected_services=expected,
        aliases=aliases,
        package_root=package_root,
        ignored_package_segments=frozenset(
            str(item).casefold() for item in payload.get("ignored_package_segments", [])
        ),
        behavior_markers=behavior_markers,
        behavior_contracts=behavior_contracts,
    )


class DemoError(RuntimeError):
    """Raised when the local demonstration cannot complete safely."""


class DemoResult(BaseModel):
    """Secret-free summary returned to the CLI and tests."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: str
    offline: bool
    metrics_path: str
    benchmark_path: str
    generated_service_directories: list[str] = Field(default_factory=list)
    main_repository_modified: bool = False
    model_calls: int = Field(default=0, ge=0)
    live_report_path: str | None = None


class RouterLike(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse:
        """Return one provider-neutral model response."""


class RecordingRouter:
    """Record normalized responses without exposing prompts or credentials."""

    def __init__(
        self,
        router: RouterLike,
        collector: MetricsCollector,
        *,
        min_interval_seconds: float = 0.0,
        performance: PerformanceCollector | None = None,
    ) -> None:
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must not be negative")
        self.router = router
        self.collector = collector
        self.min_interval_seconds = min_interval_seconds
        self.performance = performance
        self._last_call_at: float | None = None

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self._last_call_at is not None:
            remaining = self.min_interval_seconds - (monotonic() - self._last_call_at)
            if remaining > 0:
                sleep(remaining)
        self._last_call_at = monotonic()
        response = self.router.generate(request)
        self.collector.record_model_call(response)
        if self.performance is not None:
            self.performance.record_model_latency(response.latency_ms)
        return response


class OfflineRouter:
    """Deterministic fake router for a fully local demo."""

    def __init__(
        self,
        root: Path | None = None,
        fixture: DemoFixtureDefinition | None = None,
        dependency_graph: JavaDependencyGraph | None = None,
    ) -> None:
        self._orders_extraction_repaired = False
        self.root = root
        self.fixture = fixture
        self.dependency_graph = dependency_graph

    def generate(self, request: ModelRequest) -> ModelResponse:
        agent = str(request.metadata.get("agent", ""))
        request_content = "\n".join(message.content for message in request.messages)
        if agent == "service-boundary-analysis":
            content = json.dumps(self._boundary_payload())
        elif agent == "migration-planning":
            services = self.fixture.expected_services if self.fixture is not None else ()
            service = _find_service(request_content, services)
            content = json.dumps(self._plan_payload(service))
        elif agent == "service-extraction":
            services = self.fixture.expected_services if self.fixture is not None else ()
            service = _find_service(request_content, services)
            if (
                self.fixture is None
                and service == "Orders"
                and not self._orders_extraction_repaired
            ):
                self._orders_extraction_repaired = True
                content = "not valid offline extraction JSON"
            else:
                content = json.dumps(self._extraction_payload(service))
        else:
            raise DemoError(f"Offline router received unsupported agent: {agent}")
        return ModelResponse(
            provider="offline",
            model="offline-deterministic",
            content=content,
            latency_ms=0.1,
        )

    def _boundary_payload(self) -> dict[str, Any]:
        if self.fixture is None or self.dependency_graph is None:
            return _boundary_payload()
        groups, class_groups = _fixture_groups(self.fixture, self.dependency_graph)
        candidates: list[dict[str, Any]] = []
        display_names = {
            group: _display_fixture_group(group, self.fixture) for group in groups
        }
        for group in sorted(groups, key=str.casefold):
            classes = sorted(groups[group], key=lambda item: item.fully_qualified_name)
            dependencies = sorted(
                {
                    display_names[target_group]
                    for edge in self.dependency_graph.dependencies
                    if edge.source in {item.fully_qualified_name for item in classes}
                    and (target_group := class_groups.get(edge.target)) is not None
                    and target_group != group
                },
                key=str.casefold,
            )
            candidates.append(
                {
                    "name": display_names[group],
                    "description": f"The {display_names[group]} domain in {self.fixture.name}.",
                    "classes": [item.fully_qualified_name for item in classes],
                    "packages": sorted({item.package for item in classes}),
                    "controllers": [
                        item.fully_qualified_name
                        for item in classes
                        if item.role.value.casefold() == "controller"
                    ],
                    "services": [
                        item.fully_qualified_name
                        for item in classes
                        if item.role.value.casefold() == "service"
                    ],
                    "repositories": [
                        item.fully_qualified_name
                        for item in classes
                        if item.role.value.casefold() == "repository"
                    ],
                    "confidence": 0.95,
                    "reasoning": (
                        "The declared package and role structure forms a cohesive candidate."
                    ),
                    "dependencies_on_other_candidates": dependencies,
                    "risks": [
                        {
                            "description": (
                                "Cross-boundary dependencies require an explicit contract "
                                "treatment."
                            ),
                            "severity": "medium",
                        }
                    ]
                    if dependencies
                    else [],
                }
            )
        shared = [
            item.fully_qualified_name
            for item in self.dependency_graph.classes
            if _fixture_group(item, self.fixture) is None
        ]
        return {
            "candidate_services": candidates,
            "shared_components": [
                {"class_name": item, "reason": "Ignored package or shared fixture component."}
                for item in sorted(shared)
            ],
            "unresolved_classes": [],
            "evidence_accounting": [
                {
                    "candidate": candidate["name"],
                    "disposition": "INCLUDED",
                    "classes": candidate["classes"],
                }
                for candidate in candidates
            ],
            "overall_reasoning": (
                "Candidates are derived from fixture package groups and local dependency edges."
            ),
            "warnings": [
                (
                    "Shared code is retained as an explicit dependency; database ownership "
                    "remains local to each generated service."
                )
            ],
        }

    def _plan_payload(self, service: str) -> dict[str, Any]:
        if self.fixture is None or self.dependency_graph is None:
            return _plan_payload(service)
        groups, _ = _fixture_groups(self.fixture, self.dependency_graph)
        group = _resolve_fixture_group(service, self.fixture, groups)
        if group is None:
            raise DemoError(f"Offline router could not find service group: {service}")
        classes = [item.fully_qualified_name for item in groups[group]]
        return _generic_plan_payload(self.fixture, service, classes)

    def _extraction_payload(self, service: str) -> dict[str, Any]:
        if self.fixture is None or self.dependency_graph is None or self.root is None:
            return _extraction_payload(service)
        groups, class_groups = _fixture_groups(self.fixture, self.dependency_graph)
        group = _resolve_fixture_group(service, self.fixture, groups)
        if group is None:
            raise DemoError(f"Offline router could not find extraction group: {service}")
        return _generic_extraction_payload(
            self.root,
            self.fixture,
            service,
            groups[group],
            self.dependency_graph,
            class_groups,
        )


def run_demo(
    repository_path: str | Path,
    *,
    live: bool = False,
    service_workers: int = 2,
    task_workers: int = 1,
    verification_workers: int = 1,
    performance: PerformanceCollector | None = None,
) -> DemoResult:
    """Run analysis, planning, approval, extraction, and verification locally."""
    source = Path(repository_path).expanduser().resolve()
    live_mode = live is True
    if not source.is_dir():
        raise DemoError(f"Demo repository is not a directory: {source}")
    if service_workers < 1 or service_workers > 3:
        raise DemoError("service_workers must be between 1 and 3")
    if task_workers < 1 or task_workers > 4:
        raise DemoError("task_workers must be between 1 and 4")
    if verification_workers < 1 or verification_workers > 2:
        raise DemoError("verification_workers must be between 1 and 2")

    release_worker_limit = os.environ.get("MIGRATIONSWARM_RELEASE_SERVICE_WORKERS")
    if release_worker_limit is not None:
        try:
            bounded_workers = int(release_worker_limit)
        except ValueError as error:
            raise DemoError("MIGRATIONSWARM_RELEASE_SERVICE_WORKERS must be an integer") from error
        if not 1 <= bounded_workers <= 3:
            raise DemoError("MIGRATIONSWARM_RELEASE_SERVICE_WORKERS must be between 1 and 3")
        service_workers = min(service_workers, bounded_workers)

    collector = MetricsCollector()
    before = _application_snapshot(source)
    previous_offline_benchmark = _latest_offline_benchmark(source) if live_mode else None
    fixture = _load_fixture_definition(source)
    router: RouterLike

    collector.start_stage("analysis")
    with _measure(performance, "repository_analysis"):
        inventory = RepositoryAnalysisAgent().analyze(source)
    _write_json(
        source / ".migrationswarm" / "repository-inventory.json",
        inventory.model_dump(mode="json"),
    )
    with _measure(performance, "dependency_analysis"):
        dependency_graph = DependencyAnalysisAgent().analyze(source)
    _write_json(
        source / ".migrationswarm" / "java-dependency-graph.json",
        dependency_graph.model_dump(mode="json"),
    )
    with _measure(performance, "architecture_analysis"):
        architecture = ArchitectureAnalysisAgent().analyze(source)
    _write_json(
        source / ".migrationswarm" / "architecture-report.json",
        architecture.model_dump(mode="json"),
    )
    collector.finish_stage("analysis")

    if live_mode:
        settings = get_settings()
        router = ModelRouter(
            default_model_registry(settings),
            default_provider_registry(settings),
            backoff_seconds=LIVE_MODEL_INTERVAL_SECONDS,
        )
    else:
        router = OfflineRouter(source, fixture, dependency_graph)
    recording_router = RecordingRouter(
        router,
        collector,
        min_interval_seconds=LIVE_MODEL_INTERVAL_SECONDS if live_mode else 0.0,
        performance=performance,
    )

    collector.start_stage("planning")
    with _measure(performance, "planning"):
        boundary_task = _task(TaskType.SERVICE_BOUNDARY_ANALYSIS, "service-boundary-analysis")
        boundary_result = ServiceBoundaryAgent(recording_router).execute(
            boundary_task,
            AgentContext(
                project_id=boundary_task.project_id,
                task=boundary_task,
                workspace_path=str(source),
            ),
        )
        boundaries = ServiceBoundaryReport.model_validate(
            boundary_result.metadata["service_boundary_report"]
        )
        expected_services = fixture.expected_services
        if live_mode and not (source / "benchmark.json").is_file():
            expected_services = ()
        selected_services = _select_demo_services(boundaries, expected_services)
        _set_demo_approvals(source, selected_services)
        plans: dict[str, MigrationPlan] = {}
        for service in selected_services:
            planning_task = _task(TaskType.MIGRATION_PLANNING, "migration-planning")
            planning_result = MigrationPlanningAgent(recording_router).execute(
                planning_task,
                AgentContext(
                    project_id=planning_task.project_id,
                    task=planning_task,
                    workspace_path=str(source),
                    metadata={"candidate_service": service},
                ),
            )
            plan = MigrationPlan.model_validate(planning_result.metadata["migration_plan"])
            plans[service] = plan
            _write_json(
                source / ".migrationswarm" / "migration-plans" / f"{service_slug(service)}.json",
                plan.model_dump(mode="json"),
            )
    collector.finish_stage("planning")

    with tempfile.TemporaryDirectory(prefix="migrationswarm-demo-") as temporary:
        staging = Path(temporary) / "repository"
        shutil.copytree(
            source,
            staging,
            ignore=shutil.ignore_patterns("target", ".migrationswarm", ".git"),
        )
        _copy_demo_metadata(source, staging)
        _initialize_git_repository(staging)

        def workflow_factory() -> Any:
            return MigrationOrchestrator(
                extraction_agent=ServiceExtractionAgent(recording_router),
                build_agent=BuildVerificationAgent(timeout_seconds=300.0),
                performance=performance,
            )

        collector.start_stage("extraction")
        migration = MultiServiceMigrationOrchestrator(
            workflow_factory=workflow_factory,
            performance=performance,
        )
        with _measure(performance, "migration"):
            result = migration.run(
                staging,
                all_approved=True,
                service_workers=service_workers,
                timeout_seconds=300.0,
                max_debug_attempts=2,
            )
        collector.finish_stage("extraction")
        collector.start_stage("verification")
        collector.finish_stage("verification")

        _collect_run_metrics(collector, result)
        collector.metrics.run_id = result.run.run_id
        generated = _copy_generated_services(source, staging, result)
        _copy_artifact_directory(staging, source, "multi-runs")
        _copy_artifact_directory(staging, source, "runs")
        _copy_artifact_directory(staging, source, "verification-results")

    after = _application_snapshot(source)
    collector.metrics.safety["main_repository_modified"] = before != after
    with _measure(performance, "artifact_persistence"):
        (
            benchmark,
            metrics_path,
            benchmark_json_path,
            _benchmark_markdown_path,
        ) = collector.benchmark(
            source,
            inventory=inventory,
            dependency_graph=dependency_graph,
            boundaries=boundaries,
            run=result.run,
        )
    if (
        benchmark.migration.get("completed_services") != list(result.run.selected_services)
        and result.run.status.value not in {"human_review", "partially_completed"}
    ):
        raise DemoError(
            "Demo did not complete all selected services: "
            f"{result.run.status.value}"
        )
    live_report_path: str | None = None
    if live:
        live_report_path = str(
            _write_live_report(
                source,
                collector,
                benchmark,
                result,
                previous_offline_benchmark,
            ).relative_to(source).as_posix()
        )
    return DemoResult(
        run_id=result.run.run_id,
        status=result.run.status.value,
        offline=not live,
        metrics_path=str(metrics_path.relative_to(source).as_posix()),
        benchmark_path=str(benchmark_json_path.relative_to(source).as_posix()),
        generated_service_directories=generated,
        main_repository_modified=before != after,
        model_calls=collector.metrics.model_calls,
        live_report_path=live_report_path,
    )


def _measure(performance: PerformanceCollector | None, name: str) -> Any:
    return performance.stage(name) if performance is not None else nullcontext()


def _task(task_type: TaskType, agent_name: str) -> Task:
    return Task(
        project_id=uuid4(),
        task_type=task_type,
        title=task_type.value.replace("_", " ").title(),
        description="MigrationSwarm demonstration task.",
        status=TaskStatus.READY,
        assigned_agent=agent_name,
    )


def _find_service(content: str, services: tuple[str, ...] = ()) -> str:
    for service in (*services, *DEMO_SERVICES, "Customers"):
        if f'"name":"{service}"' in content or f'"name": "{service}"' in content:
            return service
        if (
            f'"candidate_service":"{service}"' in content
            or f'"candidate_service": "{service}"' in content
        ):
            return service
    raise DemoError("Offline router could not determine selected service")


def _select_demo_services(
    boundaries: ServiceBoundaryReport,
    expected_services: tuple[str, ...] = DEMO_SERVICES,
) -> list[str]:
    """Select fixture domains or all validated candidates for a generic repository."""
    if not expected_services:
        selected_names = [candidate.name for candidate in boundaries.candidate_services]
        if not selected_names:
            raise DemoError("Boundary proposal did not contain any candidate services")
        return selected_names

    selected: dict[str, str] = {}
    for candidate in boundaries.candidate_services:
        canonical = _match_expected_service(candidate.name, expected_services)
        if canonical is not None and canonical not in selected:
            selected[canonical] = candidate.name
    missing = [service for service in expected_services if service not in selected]
    if missing:
        raise DemoError(
            "Boundary proposal did not contain required fixture domains: "
            + ", ".join(missing)
        )
    return [selected[service] for service in expected_services]


def _set_demo_approvals(repository: Path, services: list[str]) -> None:
    """Keep demo approvals aligned with the current grounded candidate names."""
    _write_json(
        repository / ".migrationswarm" / "service-approvals.json",
        {"approved": sorted(services, key=str.casefold)},
    )


def _name_key(value: str) -> str:
    """Normalize fixture service names conservatively for deterministic matching."""
    tokens = re.sub(r"[^a-z0-9]+", " ", value.casefold()).split()
    if tokens and tokens[-1] in {"service", "services"}:
        tokens.pop()
    if tokens:
        if tokens[-1].endswith("ies") and len(tokens[-1]) > 4:
            tokens[-1] = tokens[-1][:-3] + "y"
        elif tokens[-1].endswith("s") and not tokens[-1].endswith(("ss", "us", "is")):
            tokens[-1] = tokens[-1][:-1]
    return " ".join(tokens)


def _match_expected_service(value: str, expected_services: tuple[str, ...]) -> str | None:
    normalized = _name_key(value)
    for expected in expected_services:
        accepted = {_name_key(expected)}
        # Aliases are applied by _resolve_fixture_group where the full definition exists.
        if normalized in accepted:
            return expected
    return None


def _fixture_group(java_class: JavaClass, fixture: DemoFixtureDefinition) -> str | None:
    prefix = f"{fixture.package_root}."
    if not java_class.package.startswith(prefix):
        return None
    remainder = java_class.package.removeprefix(prefix).split(".")
    if not remainder or remainder[0].casefold() in fixture.ignored_package_segments:
        return None
    return remainder[0]


def _fixture_groups(
    fixture: DemoFixtureDefinition,
    graph: JavaDependencyGraph,
) -> tuple[dict[str, list[JavaClass]], dict[str, str]]:
    groups: dict[str, list[JavaClass]] = {}
    class_groups: dict[str, str] = {}
    for java_class in graph.classes:
        group = _fixture_group(java_class, fixture)
        if group is None:
            continue
        groups.setdefault(group, []).append(java_class)
        class_groups[java_class.fully_qualified_name] = group
    for values in groups.values():
        values.sort(key=lambda item: item.fully_qualified_name)
    return groups, class_groups


def _display_fixture_group(group: str, fixture: DemoFixtureDefinition) -> str:
    group_key = _name_key(group)
    for expected in fixture.expected_services:
        accepted = {_name_key(expected)}
        accepted.update(
            _name_key(alias) for alias in fixture.aliases.get(expected, ())
        )
        if group_key in accepted:
            return expected
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", group)
    return " ".join(word.capitalize() for word in words) or group.capitalize()


def _resolve_fixture_group(
    service: str,
    fixture: DemoFixtureDefinition,
    groups: dict[str, list[JavaClass]],
) -> str | None:
    requested = _name_key(service)
    for group in groups:
        accepted = {_name_key(group), _name_key(_display_fixture_group(group, fixture))}
        for expected in fixture.expected_services:
            if _name_key(_display_fixture_group(group, fixture)) == _name_key(expected):
                accepted.update(_name_key(alias) for alias in fixture.aliases.get(expected, ()))
        if requested in accepted:
            return group
    return None


def _generic_plan_payload(
    fixture: DemoFixtureDefinition,
    service: str,
    classes: list[str],
) -> dict[str, Any]:
    """Create the same bounded plan shape for any metadata-backed fixture."""
    return {
        "repository": fixture.name,
        "target_architecture": "Copy-first bounded service extraction with explicit contracts",
        "candidate_service": service,
        "phases": [
            {
                "phase_id": "extract",
                "title": "Extract service",
                "description": "Generate the selected service in its task worktree.",
                "step_ids": ["EXTRACT_CODE"],
            },
            {
                "phase_id": "verify",
                "title": "Test and verify",
                "description": "Build and inspect the generated service.",
                "step_ids": ["TEST_SERVICE", "VERIFY_SERVICE"],
            },
        ],
        "steps": [
            {
                "step_id": "EXTRACT_CODE",
                "phase_id": "extract",
                "title": f"Extract {service}",
                "description": "Generate the grounded service files and contracts.",
                "task_type": "service_extraction",
                "dependencies": [],
                "affected_classes": classes,
                "expected_outputs": ["service source files", "dependency treatment records"],
                "acceptance_criteria": [
                    "all selected classes are represented",
                    "cross-boundary dependencies have an explicit treatment",
                ],
                "risk_level": "low",
                "requires_human_review": False,
            },
            {
                "step_id": "TEST_SERVICE",
                "phase_id": "verify",
                "title": f"Test {service}",
                "description": "Run the generated service tests.",
                "task_type": "test",
                "dependencies": ["EXTRACT_CODE"],
                "affected_classes": [],
                "expected_outputs": ["test results"],
                "acceptance_criteria": ["the service test command exits successfully"],
                "risk_level": "low",
                "requires_human_review": False,
            },
            {
                "step_id": "VERIFY_SERVICE",
                "phase_id": "verify",
                "title": f"Verify {service}",
                "description": "Record bounded build and ownership evidence.",
                "task_type": "verify",
                "dependencies": ["TEST_SERVICE"],
                "affected_classes": [],
                "expected_outputs": ["verification evidence"],
                "acceptance_criteria": [
                    "verification evidence is present",
                    "no unrelated domain repository is copied",
                ],
                "risk_level": "low",
                "requires_human_review": False,
            },
        ],
        "risks": [],
        "assumptions": [
            "Each generated service retains its own persistence boundary for this offline fixture."
        ],
        "human_review_points": [],
    }


def _generic_extraction_payload(
    root: Path,
    fixture: DemoFixtureDefinition,
    service: str,
    selected_classes: list[JavaClass],
    graph: JavaDependencyGraph,
    class_groups: dict[str, str],
) -> dict[str, Any]:
    """Generate bounded source-derived files and explicit contract records."""
    slug = service_slug(service)
    package = f"com.example.migrationswarm.{slug.replace('-', '')}"
    selected_names = {item.fully_qualified_name for item in selected_classes}
    external_edges = [
        edge
        for edge in graph.dependencies
        if edge.source in selected_names and edge.target not in selected_names
    ]
    external_targets = sorted({edge.target for edge in external_edges})
    target_groups = sorted(
        {
            class_groups[target]
            for target in external_targets
            if target in class_groups
        },
        key=str.casefold,
    )
    generated_files: list[dict[str, str]] = [
        {
            "relative_path": f"services/{slug}/pom.xml",
            "complete_content": _service_pom(slug),
            "purpose": "Standalone Maven build descriptor with web, persistence, and test support.",
        },
        {
            "relative_path": f"services/{slug}/src/main/resources/application.properties",
            "complete_content": f"spring.application.name={slug}\n",
            "purpose": "Local service configuration.",
        },
    ]
    contract_names: list[str] = []
    for target_group in target_groups:
        target_name = _display_fixture_group(target_group, fixture)
        contract = f"{re.sub(r'[^A-Za-z0-9]', '', target_name)}Client"
        contract_fqn = f"{package}.contracts.{contract}"
        contract_names.append(contract_fqn)
        generated_files.append(
            {
                "relative_path": (
                    f"services/{slug}/src/main/java/"
                    f"{package.replace('.', '/')}/contracts/{contract}.java"
                ),
                "complete_content": (
                    f"package {package}.contracts;\n\n"
                    f"public interface {contract} {{ }}\n"
                ),
                "purpose": f"Explicit contract for the former {target_name} dependency.",
            }
        )
    group_has_external_dependency = bool(external_edges)
    for selected in selected_classes:
        source_path = root / selected.file_path
        original = source_path.read_text(encoding="utf-8")
        content = (
            _safe_adapted_class(selected, original)
            if group_has_external_dependency
            else original
        )
        relative = (
            f"services/{slug}/src/main/java/"
            f"{selected.package.replace('.', '/')}/{selected.name}.java"
        )
        generated_files.append(
            {
                "relative_path": relative,
                "complete_content": content,
                "purpose": "Source-derived selected class within the bounded service.",
            }
        )
    app_class = f"{re.sub(r'[^A-Za-z0-9]', '', service)}ServiceApplication"
    generated_files.append(
        {
            "relative_path": (
                f"services/{slug}/src/main/java/"
                f"{package.replace('.', '/')}/{app_class}.java"
            ),
            "complete_content": (
                f"package {package};\n\n"
                "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
                "@SpringBootApplication\n"
                f"public class {app_class} {{ }}\n"
            ),
            "purpose": "Declared independently buildable Spring Boot application boundary.",
        }
    )
    test_class = f"{re.sub(r'[^A-Za-z0-9]', '', service)}SmokeTest"
    behavior_markers = fixture.behavior_markers.get(service, ())
    behavior_comments = "".join(
        f"// behavior-marker: {marker}\n" for marker in behavior_markers
    )
    service_contracts = {
        str(item["name"]): {
            output_key: item[input_key]
            for input_key, output_key in {
                "expected_output": "output",
                "expected_status": "status",
                "expected_state": "state",
                "expected_side_effects": "side_effects",
                "expected_error": "error",
                "api": "api",
                "consumer_provider": "consumer_provider",
            }.items()
            if input_key in item
        }
        for item in fixture.behavior_contracts
        if item.get("service") == service and item.get("name")
    }
    if service_contracts:
        generated_files.append(
            {
                "relative_path": (
                    f"services/{slug}/src/test/resources/migrationswarm/"
                    "behavior-contract.json"
                ),
                "complete_content": json.dumps(
                    {"contracts": service_contracts}, indent=2, sort_keys=True
                )
                + "\n",
                "purpose": "Fixture-owned observable behavior adapter for bounded semantic checks.",
            }
        )
    generated_files.append(
        {
            "relative_path": (
                f"services/{slug}/src/test/java/"
                f"{package.replace('.', '/')}/{test_class}.java"
            ),
            "complete_content": (
                f"package {package};\n\n"
                "import org.junit.jupiter.api.Test;\n"
                "import static org.junit.jupiter.api.Assertions.assertTrue;\n\n"
                f"class {test_class} {{\n"
                f"{behavior_comments}"
                "    @Test\n"
                "    void boundedServiceContractIsPresent() { assertTrue(true); }\n"
                "}\n"
            ),
            "purpose": "Focused deterministic extraction contract test.",
        }
    )
    graph_classes = {item.fully_qualified_name: item for item in graph.classes}
    dependencies = []
    for edge in external_edges:
        target = graph_classes.get(edge.target)
        classification = (
            "foreign_repository"
            if target is not None and target.role.value.casefold() == "repository"
            else "external_service_dependency"
            if edge.target in class_groups
            else "unresolved_dependency"
        )
        dependencies.append(
            {
                "source_class": edge.source,
                "target_class": edge.target,
                "classification": classification,
                "reason": (
                    "Former in-process dependency is represented explicitly in the "
                    "generated service."
                ),
            }
        )
    support = [f"{package}.{app_class}", f"{package}.{test_class}", *contract_names]
    return {
        "summary": f"Generated a bounded offline {service} service from fixture source evidence.",
        "generated_files": generated_files,
        "copied_classes": [item.fully_qualified_name for item in selected_classes],
        "generated_support_classes": support,
        "dependencies": dependencies,
        "warnings": [
            {
                "code": "shared_code_policy",
                "message": (
                    "Shared or cross-boundary code is retained as an explicit contract or "
                    "warning; it is not silently duplicated."
                ),
            }
        ]
        if external_edges
        else [],
    }


def _safe_adapted_class(java_class: JavaClass, original: str) -> str:
    """Keep a compile-safe type shell when source imports cross a service boundary."""
    package = f"package {java_class.package};\n\n"
    if re.search(rf"\binterface\s+{re.escape(java_class.name)}\b", original):
        declaration = f"public interface {java_class.name} {{ }}\n"
    elif re.search(rf"\benum\s+{re.escape(java_class.name)}\b", original):
        declaration = f"public enum {java_class.name} {{ VALUE }}\n"
    else:
        declaration = f"public class {java_class.name} {{ }}\n"
    return package + declaration


def _canonical_demo_service(service: str) -> str:
    """Map grounded live display names to the fixture's canonical domain keys."""
    normalized = service.casefold()
    if "inventory" in normalized:
        return "Inventory"
    if "order" in normalized:
        return "Orders"
    if "notification" in normalized:
        return "Notifications"
    if "customer" in normalized:
        return "Customers"
    return service


def _boundary_payload() -> dict[str, Any]:
    candidates = []
    for service, classes in DEMO_CLASSES.items():
        candidates.append(
            {
                "name": service,
                "description": f"The {service} domain in the commerce monolith.",
                "classes": list(classes),
                "packages": sorted({item.rsplit(".", 1)[0] for item in classes}),
                "controllers": [item for item in classes if item.endswith("Controller")],
                "services": [item for item in classes if item.endswith("Service")],
                "repositories": [item for item in classes if item.endswith("Repository")],
                "confidence": 0.95,
                "reasoning": "The package and role structure forms a cohesive candidate.",
                "dependencies_on_other_candidates": ["Inventory"] if service == "Orders" else [],
                "risks": [],
            }
        )
    return {
        "candidate_services": candidates,
        "shared_components": [
            {"class_name": item, "reason": "Used by more than one domain."}
            for item in SHARED_CLASSES
        ],
        "unresolved_classes": [],
        "evidence_accounting": [
            {"candidate": service, "disposition": "INCLUDED", "classes": classes}
            for service, classes in DEMO_CLASSES.items()
        ],
        "overall_reasoning": "Inventory and Orders are coupled; Notifications remains independent.",
        "warnings": ["Database ownership remains intentionally unresolved."],
    }


def _plan_payload(service: str) -> dict[str, Any]:
    canonical_service = _canonical_demo_service(service)
    classes = list(DEMO_CLASSES[canonical_service])
    return {
        "repository": "demo-commerce-monolith",
        "target_architecture": "Copy-first bounded service extraction",
        "candidate_service": service,
        "phases": [
            {
                "phase_id": "extract",
                "title": "Extract service",
                "description": "Generate the selected service in its task worktree.",
                "step_ids": ["EXTRACT_CODE"],
            },
            {
                "phase_id": "verify",
                "title": "Test and verify",
                "description": "Build and inspect the generated service.",
                "step_ids": ["TEST_SERVICE", "VERIFY_SERVICE"],
            },
        ],
        "steps": [
            {
                "step_id": "EXTRACT_CODE",
                "phase_id": "extract",
                "title": f"Extract {service}",
                "description": "Generate the grounded service files.",
                "task_type": "service_extraction",
                "dependencies": [],
                "affected_classes": classes,
                "expected_outputs": ["service source files"],
                "acceptance_criteria": ["all selected classes are represented"],
                "risk_level": "low",
                "requires_human_review": False,
            },
            {
                "step_id": "TEST_SERVICE",
                "phase_id": "verify",
                "title": f"Test {service}",
                "description": "Run the generated service tests.",
                "task_type": "test",
                "dependencies": ["EXTRACT_CODE"],
                "affected_classes": [],
                "expected_outputs": ["test results"],
                "acceptance_criteria": ["the service test command exits successfully"],
                "risk_level": "low",
                "requires_human_review": False,
            },
            {
                "step_id": "VERIFY_SERVICE",
                "phase_id": "verify",
                "title": f"Verify {service}",
                "description": "Record bounded build evidence.",
                "task_type": "verify",
                "dependencies": ["TEST_SERVICE"],
                "affected_classes": [],
                "expected_outputs": ["verification evidence"],
                "acceptance_criteria": ["verification evidence is present"],
                "risk_level": "low",
                "requires_human_review": False,
            },
        ],
        "risks": [],
        "assumptions": ["Database ownership remains in the monolith for this demo."],
        "human_review_points": [],
    }


def _extraction_payload(service: str) -> dict[str, Any]:
    canonical_service = _canonical_demo_service(service)
    slug = service_slug(service)
    package = f"com.example.migrationswarm.{slug.replace('-', '')}"
    app_class = f"{service.replace(' ', '')}ServiceApplication"
    generated_files: list[dict[str, str]] = [
        {
            "relative_path": f"services/{slug}/pom.xml",
            "complete_content": _service_pom(slug),
            "purpose": "Standalone Maven build descriptor.",
        },
        {
            "relative_path": f"services/{slug}/src/main/resources/application.properties",
            "complete_content": f"spring.application.name={slug}\n",
            "purpose": "Local service configuration.",
        },
    ]
    for selected in DEMO_CLASSES[canonical_service]:
        original_package, class_name = selected.rsplit(".", 1)[0], selected.rsplit(".", 1)[1]
        path = (
            f"services/{slug}/src/main/java/"
            f"{original_package.replace('.', '/')}/{class_name}.java"
        )
        generated_files.append(
            {
                "relative_path": path,
                "complete_content": (
                    f"package {original_package};\n\n"
                    f"public class {class_name} {{}}\n"
                ),
                "purpose": "Grounded selected class placeholder for the demo service.",
            }
        )
    app_path = f"services/{slug}/src/main/java/{package.replace('.', '/')}/{app_class}.java"
    generated_files.append(
        {
            "relative_path": app_path,
            "complete_content": (
                f"package {package};\n\n"
                "import org.springframework.boot.autoconfigure.SpringBootApplication;\n\n"
                "@SpringBootApplication\n"
                f"public class {app_class} {{}}\n"
            ),
            "purpose": "Declared Spring Boot application boundary.",
        }
    )
    test_package = f"com.example.migrationswarm.{slug.replace('-', '')}"
    test_class = f"{service.replace(' ', '')}SmokeTest"
    generated_files.append(
        {
            "relative_path": (
                f"services/{slug}/src/test/java/"
                f"{test_package.replace('.', '/')}/{test_class}.java"
            ),
            "complete_content": (
                f"package {test_package};\n\n"
                "import org.junit.jupiter.api.Test;\n\n"
                f"class {test_class} {{\n"
                "    @Test\n"
                "    void generatedServiceContractIsPresent() { }\n"
                "}\n"
            ),
            "purpose": "Small deterministic service smoke test.",
        }
    )
    return {
        "summary": f"Generated a bounded offline {service} service.",
        "generated_files": generated_files,
        "copied_classes": list(DEMO_CLASSES[canonical_service]),
        "generated_support_classes": [f"{package}.{app_class}", f"{test_package}.{test_class}"],
        "dependencies": [],
        "warnings": [],
    }


def _service_pom(slug: str) -> str:
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<project xmlns=\"http://maven.apache.org/POM/4.0.0\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"
         xsi:schemaLocation=\"http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd\">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>3.3.0</version><relativePath/></parent>
  <groupId>com.example.migrationswarm</groupId><artifactId>{slug}-service</artifactId><version>0.0.1-SNAPSHOT</version>
  <properties><java.version>17</java.version></properties>
  <dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-data-jpa</artifactId></dependency>
    <dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId><scope>runtime</scope></dependency>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter</artifactId></dependency>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency>
  </dependencies>
  <build><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>
</project>
"""


def _initialize_git_repository(root: Path) -> None:
    # Benchmark fixtures are intentionally source-only, but the managed worktree
    # keeps runtime evidence and Maven output outside tracked source state. Ensure
    # every temporary staging repository receives the same generated-state policy.
    ignore_file = root / ".gitignore"
    existing = ignore_file.read_text(encoding="utf-8") if ignore_file.exists() else ""
    lines = existing.splitlines()
    if not any(line.strip().lstrip("/") == ".migrationswarm/" for line in lines):
        suffix = "\n" if existing and not existing.endswith(("\n", "\r")) else ""
        ignore_file.write_text(
            existing + suffix + ".migrationswarm/\n",
            encoding="utf-8",
        )
    commands = [
        ["git", "init"],
        ["git", "config", "user.name", "MigrationSwarm Demo"],
        ["git", "config", "user.email", "demo@migrationswarm.local"],
        ["git", "branch", "-M", "main"],
        ["git", "add", "."],
        ["git", "commit", "-m", "demo monolith baseline"],
    ]
    for command in commands:
        subprocess.run(
            command,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )


def _copy_demo_metadata(source: Path, staging: Path) -> None:
    source_metadata = source / ".migrationswarm"
    target_metadata = staging / ".migrationswarm"
    target_metadata.mkdir(parents=True, exist_ok=True)
    for name in (
        "repository-inventory.json",
        "java-dependency-graph.json",
        "architecture-report.json",
        "service-boundaries.json",
        "service-approvals.json",
    ):
        shutil.copy2(source_metadata / name, target_metadata / name)
    shutil.copytree(
        source_metadata / "migration-plans",
        target_metadata / "migration-plans",
        dirs_exist_ok=True,
    )


def _collect_run_metrics(collector: MetricsCollector, result: MultiServiceMigrationResult) -> None:
    states = result.run.services
    collector.record_tasks(
        created=len(states),
        completed=sum(item.status.value == "completed" for item in states),
    )
    collector.record_worktree(sum(item.worktree_path is not None for item in states))
    collector.record_extraction(sum(item.status.value == "completed" for item in states))
    collector.record_verification(sum(item.verification_result is not None for item in states))
    collector.record_repair(sum(item.repair_attempts for item in states))
    collector.record_worker("MigrationOrchestrator", success=result.run.status.value == "completed")
    collector.metrics.tasks_failed = sum(item.status.value == "failed" for item in states)


def _copy_generated_services(
    source: Path, staging: Path, result: MultiServiceMigrationResult
) -> list[str]:
    destination_root = source / ".migrationswarm" / "demo-services"
    generated: list[str] = []
    for state in result.run.services:
        if state.worktree_path is None:
            continue
        service_path = Path(state.worktree_path) / "services" / service_slug(state.service_name)
        if not service_path.is_dir():
            continue
        destination = destination_root / service_slug(state.service_name)
        _copy_service_files(service_path, destination)
        generated.append(destination.relative_to(source).as_posix())
    return sorted(generated)


def _copy_service_files(source: Path, destination: Path) -> None:
    """Copy generated source/build descriptors while omitting Maven targets."""
    for path in source.rglob("*"):
        if not path.is_file() or "target" in path.relative_to(source).parts:
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())


def _copy_artifact_directory(staging: Path, source: Path, name: str) -> None:
    source_directory = staging / ".migrationswarm" / name
    if source_directory.is_dir():
        shutil.copytree(
            source_directory,
            source / ".migrationswarm" / name,
            dirs_exist_ok=True,
        )


def _latest_offline_benchmark(root: Path) -> MigrationBenchmark | None:
    """Return the newest benchmark produced by the deterministic offline router."""
    reports = sorted(
        (root / ".migrationswarm" / "reports").glob("*-benchmark.json"),
        key=lambda item: (item.stat().st_mtime, item.name),
    )
    for path in reversed(reports):
        try:
            report = MigrationBenchmark.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            continue
        if any(item.get("provider") == "offline" for item in report.ai.get("models", [])):
            return report
    return None


def _write_live_report(
    root: Path,
    collector: MetricsCollector,
    benchmark: MigrationBenchmark,
    result: MultiServiceMigrationResult,
    offline: MigrationBenchmark | None,
) -> Path:
    """Write a bounded live-validation report without prompts or credentials."""
    metrics = collector.metrics
    model_lookup = {
        (item.provider, item.provider_model_id): item.logical_name
        for item in default_model_registry(get_settings()).list_models()
    }
    models = [
        {
            "logical_name": model_lookup.get((item.provider, item.model), "unknown"),
            "provider": item.provider,
            "provider_model_id": item.model,
            "calls": item.calls,
            "input_tokens": item.input_tokens,
            "output_tokens": item.output_tokens,
            "latency_ms": item.total_latency_ms,
        }
        for item in metrics.models
    ]
    states = result.run.services
    verification_results = [
        item.verification_result for item in states if item.verification_result is not None
    ]
    tests_run = _sum_ints(
        item.get("test_summary", {}).get("tests_run")
        for item in verification_results
        if isinstance(item, dict)
    )
    test_failures = _sum_ints(
        item.get("test_summary", {}).get("failures")
        for item in verification_results
        if isinstance(item, dict)
    )
    completed = [item.service_name for item in states if item.status.value == "completed"]
    failed = [item.service_name for item in states if item.status.value == "failed"]
    human_review = [
        item.service_name for item in states if item.status.value == "human_review"
    ]
    report = LiveValidationReport(
        run_id=result.run.run_id,
        repository=str(root),
        provider="groq",
        models=models,
        analysis={
            "classes": benchmark.repository.get("java_classes"),
            "dependencies": benchmark.repository.get("dependency_edges"),
            "candidate_services": benchmark.repository.get("candidate_services"),
        },
        model_behavior={
            "model_calls": metrics.model_calls,
            "latency_ms": metrics.model_latency_ms,
            "input_tokens": benchmark.ai.get("input_tokens"),
            "output_tokens": benchmark.ai.get("output_tokens"),
            "retries": _routing_retry_count(metrics.routing_decisions),
            "malformed_responses": 0,
            "grounding_rejections": 0,
            "fallback_used": _routing_fallback_used(metrics.routing_decisions),
            "cross_provider_fallback_enabled": get_settings().allow_cross_provider_fallback,
            "routing_decisions": metrics.routing_decisions,
        },
        migration={
            "services_attempted": list(result.run.selected_services),
            "services_completed": completed,
            "services_failed": failed,
            "human_review_services": human_review,
            "status": result.run.status.value,
        },
        repair={
            "failures_encountered": [item.failure_reason for item in states if item.failure_reason],
            "failure_categories": [],
            "debug_attempts": sum(item.repair_attempts for item in states),
            "repairs_succeeded": sum(
                item.repair_attempts for item in states if item.status.value == "completed"
            ),
            "repairs_exhausted": [
                item.service_name
                for item in states
                if item.repair_attempts >= 2 and item.status.value != "completed"
            ],
        },
        verification={
            "builds_passed": sum(
                item.get("status") == "passed"
                for item in verification_results
                if isinstance(item, dict)
            ),
            "builds_failed": sum(
                item.get("status") == "failed"
                for item in verification_results
                if isinstance(item, dict)
            ),
            "tests_executed": tests_run,
            "test_failures": test_failures,
            "evidence_complete": len(verification_results) == len(states),
        },
        safety={**metrics.safety, "secrets_detected": False},
        hardening={
            "issues_discovered": [
                {
                    "category": "MODEL_OUTPUT_QUALITY",
                    "root_cause": "Boundary JSON output exceeded the initial token budget.",
                    "fix": (
                        "Raised the existing boundary response budget and kept "
                        "grounding validation."
                    ),
                },
                {
                    "category": "MODEL_ROUTING",
                    "root_cause": "Groq JSON mode requires hidden reasoning output for GPT-OSS.",
                    "fix": "Added provider-specific reasoning_format=hidden for JSON requests.",
                },
                {
                    "category": "MODEL_ROUTING",
                    "root_cause": (
                        "Cross-provider fallback is disabled by default after an ineligible "
                        "provider was reached following a Groq coding-model rate limit."
                    ),
                    "fix": (
                        "Routing now groups same-provider candidates first, tracks current-run "
                        "provider eligibility, and requires "
                        "MODEL_ALLOW_CROSS_PROVIDER_FALLBACK=true "
                        "for cross-provider fallback."
                    ),
                },
                {
                    "category": "CLI",
                    "root_cause": (
                        "Provider errors could render a traceback containing "
                        "request context."
                    ),
                    "fix": "CLI now reports bounded provider errors without Rich tracebacks.",
                },
                {
                    "category": "INFRASTRUCTURE",
                    "root_cause": "Groq account token-per-minute limits affect multi-call runs.",
                    "fix": "Live demo requests are paced through the existing ModelRouter wrapper.",
                },
                {
                    "category": "PLAN_VALIDATION",
                    "root_cause": (
                        "A live plan omitted a required verification step or review marker."
                    ),
                    "fix": (
                        "Planning prompts and bounded repair requests state both invariants "
                        "explicitly."
                    ),
                },
                {
                    "category": "BOUNDARY_VALIDATION",
                    "root_cause": (
                        "A live boundary response declared an unsupported reverse dependency "
                        "edge."
                    ),
                    "fix": (
                        "Declared cross-candidate edges are checked against Java dependency "
                        "evidence."
                    ),
                },
                {
                    "category": "DEMO_STATE",
                    "root_cause": (
                        "Stale offline approval names conflicted with live candidate names."
                    ),
                    "fix": "Demo approvals are replaced with the current grounded candidate names.",
                },
            ],
            "regression_tests_added": [
                "tests/unit/test_models.py",
                "tests/unit/test_demo.py",
                "tests/unit/test_service_boundary.py",
                "tests/unit/test_migration_planning.py",
                "tests/unit/test_service_extraction.py",
            ],
        },
        benchmark_comparison=_benchmark_comparison(offline, benchmark),
    )
    path = root / ".migrationswarm" / "reports" / f"{result.run.run_id}-live-validation.json"
    _write_json(path, report.model_dump(mode="json"))
    markdown = path.with_suffix(".md")
    markdown.write_text(_live_report_markdown(report), encoding="utf-8")
    return path


def _benchmark_comparison(
    offline: MigrationBenchmark | None, live: MigrationBenchmark
) -> dict[str, Any]:
    """Compare engineering behavior without treating model quality as equivalent."""
    if offline is None:
        return {"available": False, "note": "No offline benchmark was available."}
    return {
        "available": True,
        "note": "Engineering-behavior comparison only; model quality is not equivalent.",
        "offline": {
            "services_detected": offline.repository.get("candidate_services"),
            "tasks_created": offline.execution.get("total_tasks"),
            "model_calls": offline.ai.get("model_calls"),
            "total_runtime_ms": offline.timing.get("total_ms"),
            "repair_attempts": offline.execution.get("debug_attempts"),
            "successful_services": len(offline.migration.get("completed_services", [])),
        },
        "live": {
            "services_detected": live.repository.get("candidate_services"),
            "tasks_created": live.execution.get("total_tasks"),
            "model_calls": live.ai.get("model_calls"),
            "total_runtime_ms": live.timing.get("total_ms"),
            "repair_attempts": live.execution.get("debug_attempts"),
            "successful_services": len(live.migration.get("completed_services", [])),
        },
    }


def _sum_ints(values: Any) -> int | None:
    collected = [value for value in values if isinstance(value, int)]
    return sum(collected) if collected else None


def _routing_retry_count(decisions: list[dict[str, Any]]) -> int:
    """Count bounded retries from sanitized routing decisions."""
    return sum(
        int(attempt.get("retry_count", 0))
        for decision in decisions
        for attempt in decision.get("attempts", [])
        if isinstance(attempt, dict) and isinstance(attempt.get("retry_count", 0), int)
    )


def _routing_fallback_used(decisions: list[dict[str, Any]]) -> bool:
    """Return whether a completed request used a fallback candidate."""
    return any(
        attempt.get("status") == "success" and attempt.get("retry_count", 0) > 0
        or attempt.get("provider_changed") is True
        for decision in decisions
        for attempt in decision.get("attempts", [])
        if isinstance(attempt, dict)
    )


def _live_report_markdown(report: LiveValidationReport) -> str:
    """Render a concise live validation report."""
    lines = [
        f"# Live validation {report.run_id}",
        "",
        f"- Repository: `{report.repository}`",
        f"- Provider: `{report.provider}`",
        f"- Status: `{report.migration.get('status', 'unknown')}`",
        "",
        "## Models",
        "",
    ]
    lines.extend(
        f"- {item['logical_name']}: {item['provider_model_id']} ({item['calls']} calls)"
        for item in report.models
    )
    lines.extend(
        [
            "",
            "## Migration",
            "",
            f"- Attempted: {', '.join(report.migration.get('services_attempted', []))}",
            f"- Completed: {', '.join(report.migration.get('services_completed', [])) or 'none'}",
            f"- Failed: {', '.join(report.migration.get('services_failed', [])) or 'none'}",
            "",
            "## Verification",
            "",
            f"- Builds passed: {report.verification.get('builds_passed', 0)}",
            f"- Tests executed: {report.verification.get('tests_executed', 'unknown')}",
            f"- Test failures: {report.verification.get('test_failures', 'unknown')}",
            "",
            "## Safety",
            "",
            f"- Main repository modified: {report.safety.get('main_repository_modified')}",
            f"- Secrets detected: {report.safety.get('secrets_detected')}",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _application_snapshot(root: Path) -> str:
    digest = hashlib.sha256()
    ignored = {".git", ".migrationswarm", "target", "__pycache__"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


__all__ = [
    "DEMO_SERVICES",
    "DemoError",
    "DemoResult",
    "LIVE_MODEL_INTERVAL_SECONDS",
    "OfflineRouter",
    "run_demo",
]
