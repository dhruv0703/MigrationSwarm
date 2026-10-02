"""AI-assisted but strongly grounded service-boundary proposals."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from migrationswarm.agents.architecture_analysis import (
    ARCHITECTURE_REPORT_ARTIFACT,
    ArchitectureAnalysisAgent,
    ArchitectureReport,
)
from migrationswarm.agents.dependency_analysis import (
    JAVA_DEPENDENCY_ARTIFACT,
    DependencyAnalysisAgent,
    JavaDependencyGraph,
)
from migrationswarm.core.agents.base import BaseAgent
from migrationswarm.core.agents.capabilities import AgentCapability
from migrationswarm.core.agents.models import AgentContext, AgentResult
from migrationswarm.core.models import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelRouter,
)
from migrationswarm.core.security import (
    MAX_MODEL_RESPONSE_BYTES,
    ArtifactCorruptionError,
    ensure_json_artifact_healthy,
    load_json_object,
    require_bytes,
    write_json_atomic,
    write_text_atomic,
)
from migrationswarm.core.tasks.enums import TaskStatus, TaskType
from migrationswarm.core.tasks.models import Task

SERVICE_BOUNDARIES_JSON_ARTIFACT = ".migrationswarm/service-boundaries.json"
SERVICE_BOUNDARIES_MARKDOWN_ARTIFACT = ".migrationswarm/service-boundaries.md"

logger = structlog.get_logger(__name__)


class ServiceBoundaryAnalysisError(ValueError):
    """Base exception for boundary proposal and grounding failures."""


class BoundaryResponseError(ServiceBoundaryAnalysisError):
    """Raised when model output cannot be parsed or validated after repair."""


class BoundaryGroundingError(ServiceBoundaryAnalysisError):
    """Raised when a proposal references architecture data incorrectly."""


class BoundaryDisposition(StrEnum):
    """How a deterministic architecture candidate was accounted for."""

    INCLUDED = "INCLUDED"
    MERGED = "MERGED"
    SHARED = "SHARED"
    EXCLUDED_WITH_GROUNDED_REASON = "EXCLUDED_WITH_GROUNDED_REASON"


class BoundaryReason(BaseModel):
    """A structured explanation for a proposed grouping or risk."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)


class BoundaryRisk(BaseModel):
    """A risk attached to a candidate service proposal."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    severity: str = Field(default="medium", min_length=1)


class SharedDependency(BaseModel):
    """A real class that should remain explicitly shared or unresolved."""

    model_config = ConfigDict(extra="forbid")

    class_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EvidenceAccounting(BaseModel):
    """Explicit disposition for one deterministic architecture candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate: str = Field(min_length=1)
    disposition: BoundaryDisposition
    classes: list[str] = Field(default_factory=list)
    merged_into: str | None = None
    reason: str | None = None


class CandidateService(BaseModel):
    """A proposed structural candidate, never a guaranteed microservice boundary."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    classes: list[str] = Field(min_length=1)
    packages: list[str]
    controllers: list[str]
    services: list[str]
    repositories: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)
    dependencies_on_other_candidates: list[str]
    risks: list[BoundaryRisk]


class ServiceBoundaryReport(BaseModel):
    """Validated and grounded service-boundary proposal report."""

    model_config = ConfigDict(extra="forbid")

    candidate_services: list[CandidateService]
    shared_components: list[SharedDependency]
    unresolved_classes: list[str]
    evidence_accounting: list[EvidenceAccounting] = Field(default_factory=list)
    overall_reasoning: str = Field(min_length=1)
    warnings: list[str]
    model_provider: str
    model_name: str


class _ModelRouterLike(Protocol):
    """Small protocol allowing deterministic fake routers in tests."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate one model response."""


class ServiceBoundaryAgent(BaseAgent):
    """Propose candidate service boundaries from structured architecture evidence."""

    name = "service-boundary-analysis"
    capabilities = frozenset({AgentCapability.SERVICE_BOUNDARY_ANALYSIS})

    system_prompt = """You analyze a Java Spring Boot monolith after deterministic
architecture analysis.
Your task is to propose a small number of candidate architectural components that may later inform
service-boundary work. These are proposals, never guaranteed microservice boundaries.

Prefer highly cohesive groups, minimize unnecessary cross-candidate coupling, and avoid splitting
controller/service/repository chains without strong evidence. Identify shared components and
ambiguous classes explicitly. Do not create one service per class. Do not invent classes,
packages, dependencies, or relationships absent from the supplied JSON. A declared
cross-candidate dependency MUST be supported by at least one supplied dependency edge
between classes assigned to those two candidates. You must account for every deterministic
candidate/domain supplied in the evidence. A domain may be included, merged, shared, or
explicitly excluded with a grounded reason, but it may not disappear silently. Explain
uncertainty. Candidate dependencies are further restricted to the explicit
allowed_candidate_dependencies set in the evidence. Emit only edges from that set;
do not infer dependencies from names, domain intuition, controllers, or likely business
interactions. If the set is empty, emit no candidate dependencies. For every candidate
with INCLUDED or MERGED accounting, assign every class in the explicit
required_classes_by_candidate mapping. Do not omit, invent, or move those classes;
implementation classes merged into a parent remain owned by that parent candidate.

Return ONLY JSON matching the requested schema:
{
  "candidate_services": [{
    "name": "non-empty name",
    "description": "short description",
    "classes": ["fully.qualified.Class"],
    "packages": ["package.name"],
    "controllers": ["fully.qualified.Controller"],
    "services": ["fully.qualified.Service"],
    "repositories": ["fully.qualified.Repository"],
    "confidence": 0.0,
    "reasoning": "evidence-based reasoning",
    "dependencies_on_other_candidates": ["other candidate name"],
    "risks": [{"description": "risk", "severity": "low|medium|high"}]
  }],
  "shared_components": [{"class_name": "fully.qualified.Class", "reason": "why shared"}],
  "unresolved_classes": ["fully.qualified.Class"],
  "evidence_accounting": [{
    "candidate": "deterministic candidate/domain name",
    "disposition": "INCLUDED|MERGED|SHARED|EXCLUDED_WITH_GROUNDED_REASON",
    "classes": ["fully.qualified.Class"],
    "merged_into": "target candidate name when disposition is MERGED",
    "reason": "required for SHARED, MERGED, or EXCLUDED_WITH_GROUNDED_REASON"
  }],
  "overall_reasoning": "overall explanation",
  "warnings": ["uncertainty or limitation"]
}"""

    def __init__(self, router: _ModelRouterLike | None = None) -> None:
        self.router = router or self._default_router()

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        """Prepare evidence, ask the router once, repair once if needed, and write artifacts."""
        started_at = datetime.now(UTC)
        if context.workspace_path is None:
            raise ServiceBoundaryAnalysisError("Service boundary analysis requires workspace_path")

        evidence = self.prepare_evidence(context.workspace_path)
        report = self._propose(evidence)
        root = Path(context.workspace_path).expanduser().resolve()
        self._write_artifacts(root, report)
        logger.info(
            "service_boundary_proposal_completed",
            provider=report.model_provider,
            model=report.model_name,
            candidate_count=len(report.candidate_services),
            unresolved_count=len(report.unresolved_classes),
        )
        report_data = report.model_dump(mode="json")
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=(
                f"Proposed {len(report.candidate_services)} candidate services with "
                f"{len(report.unresolved_classes)} unresolved classes."
            ),
            artifacts=[SERVICE_BOUNDARIES_JSON_ARTIFACT, SERVICE_BOUNDARIES_MARKDOWN_ARTIFACT],
            metadata={"service_boundary_report": report_data},
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    def prepare_evidence(self, workspace: str | Path) -> dict[str, Any]:
        """Ensure deterministic artifacts exist and return compact structured evidence."""
        root = Path(workspace).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ServiceBoundaryAnalysisError(f"Workspace is not a directory: {root}")

        architecture_path = root / ARCHITECTURE_REPORT_ARTIFACT
        dependency_path = root / JAVA_DEPENDENCY_ARTIFACT
        if not architecture_path.is_file():
            if not dependency_path.is_file():
                self._run_dependency_analysis(root)
            self._run_architecture_analysis(root)

        try:
            architecture = ArchitectureReport.model_validate(load_json_object(architecture_path))
        except (ArtifactCorruptionError, ValidationError, ValueError) as error:
            raise ServiceBoundaryAnalysisError(
                "Could not load architecture evidence: artifact is corrupt"
            ) from error

        dependencies: list[dict[str, str]] = []
        if dependency_path.is_file():
            try:
                dependency_graph = JavaDependencyGraph.model_validate(
                    load_json_object(dependency_path)
                )
                dependencies = [
                    {
                        "source": edge.source,
                        "target": edge.target,
                        "relationship": edge.relationship.value,
                    }
                    for edge in dependency_graph.dependencies
                ]
            except (ArtifactCorruptionError, ValidationError, ValueError) as error:
                raise ServiceBoundaryAnalysisError(
                    "Could not load dependency evidence: artifact is corrupt"
                ) from error

        evidence = {
            "classes": [metric.model_dump(mode="json") for metric in architecture.class_metrics],
            "packages": [metric.model_dump(mode="json") for metric in architecture.package_metrics],
            "candidate_components": [
                component.model_dump(mode="json") for component in architecture.components
            ],
            "risks": [risk.model_dump(mode="json") for risk in architecture.risks],
            "dependencies": dependencies,
            "connected_component_count": architecture.connected_component_count,
            "strongly_connected_component_count": architecture.strongly_connected_component_count,
            "cycle_count": architecture.cycle_count,
            "controller_service_relationships": architecture.controller_service_relationships,
            "service_repository_relationships": architecture.service_repository_relationships,
        }
        logger.info(
            "service_boundary_evidence_prepared",
            class_count=len(architecture.class_metrics),
            dependency_count=len(dependencies),
            input_bytes=self.input_size(evidence),
        )
        return evidence

    @staticmethod
    def input_size(evidence: dict[str, Any]) -> int:
        """Return the UTF-8 JSON size used as a compact input-size estimate."""
        return len(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def _propose(self, evidence: dict[str, Any]) -> ServiceBoundaryReport:
        allowed_dependencies = _allowed_candidate_dependencies(evidence)
        required_classes = _required_classes_by_candidate(evidence)
        request = self._request(evidence)
        try:
            response = self.router.generate(request)
            output = self._parse_and_ground(response.content, evidence)
        except (BoundaryResponseError, BoundaryGroundingError) as first_error:
            repair_request = self._repair_request(
                request, str(first_error), allowed_dependencies, required_classes
            )
            try:
                repaired = self.router.generate(repair_request)
                output = self._parse_and_ground(repaired.content, evidence)
                response = repaired
            except (BoundaryResponseError, BoundaryGroundingError) as second_error:
                raise BoundaryResponseError(
                    "Model boundary output failed validation after one repair attempt: "
                    f"{second_error}"
                ) from first_error

        return ServiceBoundaryReport(
            **output.model_dump(),
            model_provider=response.provider,
            model_name=response.model,
        )

    def _parse_and_ground(
        self, content: str, evidence: dict[str, Any]
    ) -> _BoundaryModelOutput:
        try:
            require_bytes(content, MAX_MODEL_RESPONSE_BYTES, label="boundary response")
            data = json.loads(_strip_json_fence(content))
            output = _BoundaryModelOutput.model_validate(data)
        except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as error:
            raise BoundaryResponseError("Model output was not valid boundary JSON") from error
        self._validate_grounding(output, evidence)
        return output

    @staticmethod
    def _validate_grounding(output: _BoundaryModelOutput, evidence: dict[str, Any]) -> None:
        known_classes = {item["fully_qualified_name"] for item in evidence["classes"]}
        shared_classes = {item.class_name for item in output.shared_components}
        candidate_names = {candidate.name for candidate in output.candidate_services}
        assigned: dict[str, str] = {}

        for candidate in output.candidate_services:
            for class_name in candidate.classes:
                _require_known(class_name, known_classes, "candidate class")
                previous = assigned.get(class_name)
                if previous is not None and class_name not in shared_classes:
                    raise BoundaryGroundingError(
                        "Class assigned to multiple candidates: "
                        f"{class_name} ({previous}, {candidate.name})"
                    )
                assigned[class_name] = candidate.name
            for role_classes in (candidate.controllers, candidate.services, candidate.repositories):
                for class_name in role_classes:
                    _require_known(class_name, known_classes, "role class")
                    if class_name not in candidate.classes:
                        raise BoundaryGroundingError(
                            "Role class is not included in candidate "
                            f"{candidate.name}: {class_name}"
                        )
            for dependency in candidate.dependencies_on_other_candidates:
                if dependency not in candidate_names:
                    raise BoundaryGroundingError(
                        f"Candidate dependency does not exist: {candidate.name} -> {dependency}"
                    )

        supported_dependencies = {
            (item["source"], item["target"])
            for item in _allowed_candidate_dependencies(evidence)
        }
        for candidate in output.candidate_services:
            for dependency in candidate.dependencies_on_other_candidates:
                if (candidate.name, dependency) not in supported_dependencies:
                    raise BoundaryGroundingError(
                        "Candidate dependency is not supported by Java dependency evidence: "
                        f"{candidate.name} -> {dependency}"
                    )

        for shared in output.shared_components:
            _require_known(shared.class_name, known_classes, "shared class")
        for class_name in output.unresolved_classes:
            _require_known(class_name, known_classes, "unresolved class")

        ServiceBoundaryAgent._validate_coverage(output, evidence, assigned)

    @staticmethod
    def _validate_coverage(
        output: _BoundaryModelOutput,
        evidence: dict[str, Any],
        assigned: dict[str, str],
    ) -> None:
        """Require deterministic candidate and architecture-relevant class accounting."""
        deterministic = _deterministic_candidates(evidence)
        if not deterministic:
            return
        required_names = {item["name"] for item in deterministic}
        accounting_by_name: dict[str, EvidenceAccounting] = {}
        for item in output.evidence_accounting:
            if item.candidate in accounting_by_name:
                raise BoundaryGroundingError(
                    f"INCOMPLETE_BOUNDARY_COVERAGE: duplicate evidence accounting for "
                    f"{item.candidate}"
                )
            accounting_by_name[item.candidate] = item

        missing = sorted(required_names - set(accounting_by_name))
        unknown = sorted(set(accounting_by_name) - required_names)
        if missing or unknown:
            details = []
            if missing:
                details.append("omitted candidates=" + ",".join(missing))
            if unknown:
                details.append("unknown candidates=" + ",".join(unknown))
            raise BoundaryGroundingError(
                "INCOMPLETE_BOUNDARY_COVERAGE: " + "; ".join(details)
            )

        candidate_by_name = {
            _normal_name(candidate.name): candidate for candidate in output.candidate_services
        }
        shared_classes = {item.class_name for item in output.shared_components}
        unresolved_classes = set(output.unresolved_classes)
        component_by_name = {_normal_name(item["name"]): item for item in deterministic}

        for name in sorted(required_names):
            accounting = accounting_by_name[name]
            component = component_by_name[_normal_name(name)]
            source_classes = set(component["classes"])
            accounted_classes = set(accounting.classes) or source_classes
            if not source_classes <= accounted_classes:
                omitted = sorted(source_classes - accounted_classes)
                raise BoundaryGroundingError(
                    "INCOMPLETE_BOUNDARY_COVERAGE: accounting omits classes from "
                    f"{name}: {', '.join(omitted)}"
                )

            if accounting.disposition is BoundaryDisposition.INCLUDED:
                target = candidate_by_name.get(_normal_name(name))
                missing_classes = (
                    sorted(source_classes - set(target.classes))
                    if target is not None
                    else sorted(source_classes)
                )
                if missing_classes:
                    raise BoundaryGroundingError(
                        "INCOMPLETE_BOUNDARY_COVERAGE: missing class assignments for "
                        f"{name}: {', '.join(missing_classes)}"
                    )
            elif accounting.disposition is BoundaryDisposition.MERGED:
                if not accounting.merged_into:
                    raise BoundaryGroundingError(
                        "INCOMPLETE_BOUNDARY_COVERAGE: merged candidate requires merged_into "
                        f"for {name}"
                    )
                target = candidate_by_name.get(_normal_name(accounting.merged_into))
                if target is None:
                    raise BoundaryGroundingError(
                        "INCOMPLETE_BOUNDARY_COVERAGE: merge target does not exist for "
                        f"{name}: {accounting.merged_into}"
                    )
                if not source_classes <= set(target.classes):
                    missing_classes = sorted(source_classes - set(target.classes))
                    raise BoundaryGroundingError(
                        "INCOMPLETE_BOUNDARY_COVERAGE: merged source classes disappear for "
                        f"{name}: {', '.join(missing_classes)}"
                    )
                target_classes = set(target.classes) - source_classes
                if not _has_grouping_evidence(source_classes, target_classes, evidence):
                    raise BoundaryGroundingError(
                        "INCOMPLETE_BOUNDARY_COVERAGE: merge lacks dependency evidence for "
                        f"{name} -> {accounting.merged_into}"
                    )
                _require_grounded_reason(accounting, name)
            elif accounting.disposition is BoundaryDisposition.SHARED:
                if not source_classes <= shared_classes:
                    raise BoundaryGroundingError(
                        "INCOMPLETE_BOUNDARY_COVERAGE: shared candidate classes are not "
                        f"explicitly shared for {name}"
                    )
                _require_grounded_reason(accounting, name)
            else:
                if not source_classes <= unresolved_classes:
                    raise BoundaryGroundingError(
                        "INCOMPLETE_BOUNDARY_COVERAGE: excluded candidate classes are not "
                        f"explicitly unresolved for {name}"
                    )
                _require_grounded_reason(accounting, name)

        relevant_classes = {
            class_name for item in deterministic for class_name in item["classes"]
        }
        for class_name in sorted(relevant_classes):
            candidate_count = int(class_name in assigned)
            shared_count = int(class_name in shared_classes)
            unresolved_count = int(class_name in unresolved_classes)
            if candidate_count + shared_count + unresolved_count != 1:
                raise BoundaryGroundingError(
                    "INCOMPLETE_BOUNDARY_COVERAGE: missing or ambiguous class assignment "
                    f"for {class_name}"
                )

        for candidate in output.candidate_services:
            if not candidate.classes:
                raise BoundaryGroundingError(
                    f"Candidate has no class assignment: {candidate.name}"
                )

    def _request(self, evidence: dict[str, Any]) -> ModelRequest:
        evidence_json = json.dumps(
            self._compact_evidence(evidence), sort_keys=True, separators=(",", ":")
        )
        return ModelRequest(
            messages=[
                ModelMessage(role=ModelRole.SYSTEM, content=self.system_prompt),
                ModelMessage(
                    role=ModelRole.USER,
                    content=(
                        "Using only this structured architecture evidence, propose candidate "
                        f"service boundaries:\n{evidence_json}"
                    ),
                ),
            ],
            capability=ModelCapability.ARCHITECTURE,
            temperature=0.1,
            max_tokens=4000,
            response_format={"type": "json_object"},
            metadata={"agent": self.name},
        )

    @staticmethod
    def _compact_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
        """Keep model context small while retaining all boundary-relevant evidence."""
        return {
            "classes": [
                {
                    key: item[key]
                    for key in ("fully_qualified_name", "package", "role")
                }
                for item in evidence["classes"]
            ],
            "dependencies": evidence.get("dependencies", []),
            "allowed_candidate_dependencies": _allowed_candidate_dependencies(evidence),
            "required_classes_by_candidate": _required_classes_by_candidate(evidence),
            "candidate_domains": [
                {
                    "name": item["name"],
                    "classes": item["classes"],
                    "packages": item["packages"],
                    "merged_packages": item.get("merged_packages", []),
                    "merged_classes": item.get("merged_classes", []),
                    "controller_count": item["controller_count"],
                    "service_count": item["service_count"],
                    "repository_count": item["repository_count"],
                }
                for item in _deterministic_candidates(evidence)
            ],
            "connected_component_count": evidence.get("connected_component_count", 0),
            "strongly_connected_component_count": evidence.get(
                "strongly_connected_component_count", 0
            ),
            "cycle_count": evidence.get("cycle_count", 0),
            "controller_service_relationships": evidence.get(
                "controller_service_relationships", 0
            ),
            "service_repository_relationships": evidence.get(
                "service_repository_relationships", 0
            ),
        }

    @staticmethod
    def _repair_request(
        request: ModelRequest,
        invalid_output: str,
        allowed_dependencies: list[dict[str, str]],
        required_classes: list[dict[str, Any]],
    ) -> ModelRequest:
        allowed_json = json.dumps(
            allowed_dependencies, sort_keys=True, separators=(",", ":")
        )
        required_classes_json = json.dumps(
            required_classes, sort_keys=True, separators=(",", ":")
        )
        return request.model_copy(
            update={
                "messages": [
                    *request.messages,
                    ModelMessage(
                        role=ModelRole.USER,
                        content=(
                            "Repair the following invalid response. Return only valid JSON "
                            "matching the requested schema. Account for every deterministic "
                            "candidate and do not add classes or dependencies. Candidate "
                            "dependencies may use only this deterministic allowlist "
                            "(source -> target):\n"
                            f"{allowed_json}\n"
                            "Remove or replace every unsupported emitted edge using only "
                            "that allowlist. If it is empty, emit no candidate dependencies. "
                            "Do not infer edges from names or domain intuition. The prior "
                            "validation error was:\n"
                            f"{invalid_output}\n"
                            "Required deterministic required_classes_by_candidate "
                            "ownership (candidate -> all required classes) is:\n"
                            f"{required_classes_json}\n"
                            "Assign every exact class named in the validation feedback "
                            "to its deterministic candidate or grounded merge target.\n"
                        ),
                    ),
                ]
            }
        )

    @staticmethod
    def _default_router() -> ModelRouter:
        from migrationswarm.config import get_settings
        from migrationswarm.core.models.registry import default_model_registry
        from migrationswarm.providers import default_provider_registry

        settings = get_settings()
        return ModelRouter(
            default_model_registry(settings), default_provider_registry(settings)
        )

    @staticmethod
    def _run_dependency_analysis(root: Path) -> None:
        task = _analysis_task(TaskType.DEPENDENCY_ANALYSIS, "dependency-analysis")
        context = AgentContext(project_id=task.project_id, task=task, workspace_path=str(root))
        DependencyAnalysisAgent().execute(task, context)

    @staticmethod
    def _run_architecture_analysis(root: Path) -> None:
        task = _analysis_task(TaskType.ARCHITECTURE_ANALYSIS, "architecture-analysis")
        context = AgentContext(project_id=task.project_id, task=task, workspace_path=str(root))
        ArchitectureAnalysisAgent().execute(task, context)

    @staticmethod
    def _write_artifacts(root: Path, report: ServiceBoundaryReport) -> None:
        ensure_json_artifact_healthy(root / SERVICE_BOUNDARIES_JSON_ARTIFACT)
        write_json_atomic(
            root / SERVICE_BOUNDARIES_JSON_ARTIFACT,
            report.model_dump(mode="json"),
        )
        write_text_atomic(root / SERVICE_BOUNDARIES_MARKDOWN_ARTIFACT, _markdown_report(report))


class _BoundaryModelOutput(BaseModel):
    """Strict model-facing schema without runtime provider metadata."""

    model_config = ConfigDict(extra="forbid")

    candidate_services: list[CandidateService]
    shared_components: list[SharedDependency]
    unresolved_classes: list[str]
    evidence_accounting: list[EvidenceAccounting] = Field(default_factory=list)
    overall_reasoning: str = Field(min_length=1)
    warnings: list[str]


_IMPLEMENTATION_PACKAGE_SEGMENTS = frozenset(
    {
        "adapter",
        "adapters",
        "detail",
        "details",
        "impl",
        "implementation",
        "infra",
        "infrastructure",
        "internal",
    }
)


def _deterministic_candidates(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Return strong architecture components that can require a boundary disposition."""
    class_roles = {
        item["fully_qualified_name"]: item.get("role", "")
        for item in evidence.get("classes", [])
    }
    raw_components: list[tuple[list[str], dict[str, Any]]] = []
    all_packages: set[str] = set()
    for component in evidence.get("candidate_components", []):
        packages = list(component.get("packages", []))
        if not packages:
            continue
        raw_components.append((packages, component))
        all_packages.update(packages)

    application_root = _common_package_prefix(all_packages)
    identified_parent_packages = {
        packages[0]
        for packages, component in raw_components
        if any(
            _is_boundary_relevant_class(class_name, class_roles.get(class_name, ""))
            for class_name in component.get("classes", [])
        )
    }
    strong_packages = {
        packages[0]
        for packages, component in raw_components
        if all(
            component.get(key, 0) >= 1
            for key in ("controller_count", "service_count", "repository_count")
        )
    }
    grouped: dict[str, dict[str, Any]] = {}
    for packages, component in raw_components:
        primary_package = packages[0]
        group_key = _implementation_parent_package(
            primary_package,
            application_root=application_root,
            identified_parent_packages=identified_parent_packages,
            strong_packages=strong_packages,
        )
        group = grouped.setdefault(
            group_key,
            {
                "classes": set(),
                "packages": set(),
                "merged_packages": set(),
                "merged_classes": set(),
                "controller_count": 0,
                "service_count": 0,
                "repository_count": 0,
            },
        )
        group["classes"].update(component.get("classes", []))
        group["packages"].update(packages)
        if group_key != primary_package:
            group["merged_packages"].add(primary_package)
            group["merged_classes"].update(component.get("classes", []))
        for key in ("controller_count", "service_count", "repository_count"):
            group[key] += component.get(key, 0)

    result: list[dict[str, Any]] = []
    for package, component in sorted(grouped.items()):
        if not all(
            component.get(key, 0) >= 1
            for key in ("controller_count", "service_count", "repository_count")
        ):
            continue
        packages = sorted(component["packages"])
        classes = sorted(
            class_name
            for class_name in component["classes"]
            if _is_boundary_relevant_class(class_name, class_roles.get(class_name, ""))
        )
        if not classes:
            continue
        merged_classes = sorted(
            class_name
            for class_name in component["merged_classes"]
            if class_name in classes
        )
        domain = package.rsplit(".", 1)[-1]
        if domain in {"", "example", "main"}:
            services = [
                item.rsplit(".", 1)[-1]
                for item in component["classes"]
                if item.rsplit(".", 1)[-1].endswith("Service")
            ]
            domain = services[0][:-len("Service")] if services else package
        name = domain.replace("_", " ").replace("-", " ").title().replace(" ", "")
        result.append(
            {
                "name": name,
                "classes": classes,
                "packages": sorted(packages),
                "merged_packages": sorted(component["merged_packages"]),
                "merged_classes": merged_classes,
                "controller_count": component.get("controller_count", 0),
                "service_count": component.get("service_count", 0),
                "repository_count": component.get("repository_count", 0),
            }
        )
    return sorted(result, key=lambda item: item["name"])


def _required_classes_by_candidate(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic class ownership required for complete boundary accounting."""
    return [
        {
            "candidate": item["name"],
            "required_classes": item["classes"],
            "merged_classes": item.get("merged_classes", []),
        }
        for item in _deterministic_candidates(evidence)
    ]


def _allowed_candidate_dependencies(evidence: dict[str, Any]) -> list[dict[str, str]]:
    """Return candidate edges directly supported by deterministic class edges.

    Ownership comes from the deterministic candidate accounting above, which already
    folds nested implementation packages into their structural parent.  Model-proposed
    groupings cannot expand this set; an edge is allowed only when a supplied Java edge
    crosses two distinct deterministic candidates.
    """
    class_to_candidate = {
        class_name: candidate["name"]
        for candidate in _deterministic_candidates(evidence)
        for class_name in candidate["classes"]
    }
    allowed = {
        (class_to_candidate[source], class_to_candidate[target])
        for edge in evidence.get("dependencies", [])
        if (source := edge.get("source")) in class_to_candidate
        and (target := edge.get("target")) in class_to_candidate
        and class_to_candidate[source] != class_to_candidate[target]
    }
    return [
        {"source": source, "target": target}
        for source, target in sorted(allowed)
    ]


def _common_package_prefix(packages: set[str]) -> str:
    """Return the common package prefix used to distinguish modules from root packages."""
    if not packages:
        return ""
    parts = [package.split(".") for package in sorted(packages)]
    prefix: list[str] = []
    for values in zip(*parts, strict=False):
        if len(set(values)) != 1:
            break
        prefix.append(values[0])
    return ".".join(prefix)


def _implementation_parent_package(
    package: str,
    *,
    application_root: str,
    identified_parent_packages: set[str],
    strong_packages: set[str],
) -> str:
    """Merge nested implementation packages into their structural parent when grounded."""
    parts = package.split(".")
    if len(parts) < 2 or parts[-1].casefold() not in _IMPLEMENTATION_PACKAGE_SEGMENTS:
        return package

    parent = ".".join(parts[:-1])
    root_parts = application_root.split(".") if application_root else []
    relative_depth = len(parts) - len(root_parts)
    parent_is_root = parent == application_root
    nested_module = relative_depth >= 2 and not parent_is_root
    if parent in identified_parent_packages or parent in strong_packages or nested_module:
        return parent
    return package


def _is_boundary_relevant_class(class_name: str, role: str) -> bool:
    """Exclude application, configuration, and test infrastructure from domain coverage."""
    simple_name = class_name.rsplit(".", 1)[-1].casefold()
    lowered_role = role.casefold()
    return (
        lowered_role not in {"application", "configuration"}
        and ".test." not in class_name.casefold()
        and not simple_name.endswith(("test", "tests"))
    )


def _normal_name(value: str) -> str:
    """Normalize candidate names for deterministic matching without fuzzy guessing."""
    return "".join(character.casefold() for character in value if character.isalnum())


def _has_grouping_evidence(
    source_classes: set[str], target_classes: set[str], evidence: dict[str, Any]
) -> bool:
    """Require at least one supplied dependency edge across a merge."""
    if not target_classes:
        return False
    return any(
        (
            edge["source"] in source_classes
            and edge["target"] in target_classes
        )
        or (
            edge["target"] in source_classes
            and edge["source"] in target_classes
        )
        for edge in evidence.get("dependencies", [])
    )


def _require_grounded_reason(accounting: EvidenceAccounting, candidate: str) -> None:
    """Require a bounded reason tied to the named deterministic candidate."""
    if not accounting.reason or not any(
        token in accounting.reason.casefold()
        for token in (candidate.casefold(), *candidate.casefold().split())
        if len(token) >= 4
    ):
        raise BoundaryGroundingError(
            "INCOMPLETE_BOUNDARY_COVERAGE: disposition reason is not grounded for "
            f"{candidate}"
        )


def _analysis_task(task_type: TaskType, agent_name: str) -> Task:
    """Create a ready internal task for deterministic prerequisite analysis."""
    return Task(
        project_id=uuid4(),
        task_type=task_type,
        title=task_type.value.replace("_", " ").title(),
        description="Prepare structured evidence for service-boundary analysis.",
        status=TaskStatus.READY,
        assigned_agent=agent_name,
    )


def _require_known(class_name: str, known_classes: set[str], label: str) -> None:
    if class_name not in known_classes:
        raise BoundaryGroundingError(f"Unknown {label}: {class_name}")


def _strip_json_fence(content: str) -> str:
    """Accept a common fenced JSON response while rejecting other prose."""
    stripped = content.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        return stripped[7:-3].strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped[3:-3].strip()
    return stripped


def _markdown_report(report: ServiceBoundaryReport) -> str:
    lines = [
        "# Service Boundary Proposals",
        "",
        "These are evidence-based proposals, not guaranteed microservice boundaries.",
        "",
        f"Overall reasoning: {report.overall_reasoning}",
        "",
        "## Candidate services",
        "",
    ]
    for candidate in report.candidate_services:
        lines.extend(
            [
                f"### {candidate.name}",
                "",
                f"- Confidence: {candidate.confidence:.2f}",
                f"- Description: {candidate.description}",
                f"- Classes: {', '.join(candidate.classes)}",
                f"- Packages: {', '.join(candidate.packages) or 'None'}",
                f"- Cross-candidate dependencies: "
                f"{', '.join(candidate.dependencies_on_other_candidates) or 'None'}",
                f"- Reasoning: {candidate.reasoning}",
                "- Risks:",
            ]
        )
        lines.extend(
            f"  - {risk.severity}: {risk.description}" for risk in candidate.risks
        )
        if not candidate.risks:
            lines.append("  - None")
        lines.append("")
    lines.append("## Shared components")
    lines.append("")
    lines.extend(
        f"- {item.class_name}: {item.reason}" for item in report.shared_components
    )
    if not report.shared_components:
        lines.append("- None")
    lines.extend(["", "## Evidence accounting", ""])
    lines.extend(
        f"- {item.candidate}: {item.disposition.value}"
        + (f" -> {item.merged_into}" if item.merged_into else "")
        for item in report.evidence_accounting
    )
    if not report.evidence_accounting:
        lines.append("- None")
    lines.extend(["", "## Unresolved classes", ""])
    lines.extend(f"- {class_name}" for class_name in report.unresolved_classes)
    if not report.unresolved_classes:
        lines.append("- None")
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {warning}" for warning in report.warnings)
    if not report.warnings:
        lines.append("- None")
    return "\n".join(lines) + "\n"


__all__ = [
    "BoundaryGroundingError",
    "BoundaryDisposition",
    "BoundaryReason",
    "BoundaryResponseError",
    "BoundaryRisk",
    "CandidateService",
    "EvidenceAccounting",
    "ServiceBoundaryAgent",
    "ServiceBoundaryAnalysisError",
    "ServiceBoundaryReport",
    "SharedDependency",
    "SERVICE_BOUNDARIES_JSON_ARTIFACT",
    "SERVICE_BOUNDARIES_MARKDOWN_ARTIFACT",
]
