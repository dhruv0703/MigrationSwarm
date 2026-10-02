"""Grounded, model-assisted migration planning without source changes."""

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID, uuid4, uuid5

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
from migrationswarm.agents.service_boundary import (
    SERVICE_BOUNDARIES_JSON_ARTIFACT,
    ServiceBoundaryAgent,
    ServiceBoundaryReport,
)
from migrationswarm.core.agents import AgentCapability, AgentContext, AgentResult, BaseAgent
from migrationswarm.core.models import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelRouter,
)
from migrationswarm.core.scheduler import TaskGraph
from migrationswarm.core.security import (
    MAX_MODEL_RESPONSE_BYTES,
    ArtifactCorruptionError,
    ensure_json_artifact_healthy,
    load_json_object,
    require_bytes,
    write_json_atomic,
    write_text_atomic,
)
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

logger = structlog.get_logger(__name__)

MIGRATION_PLAN_JSON_ARTIFACT: Final[str] = ".migrationswarm/migration-plan.json"
MIGRATION_PLAN_MARKDOWN_ARTIFACT: Final[str] = ".migrationswarm/migration-plan.md"


class MigrationPlanningError(ValueError):
    """Base exception for migration plan preparation or validation failures."""


class MissingMigrationEvidenceError(MigrationPlanningError):
    """Raised when required planning evidence is unavailable."""


class SelectedServiceError(MigrationPlanningError):
    """Raised when the requested candidate service is not in the boundary report."""


class MigrationPlanResponseError(MigrationPlanningError):
    """Raised when model output is not valid structured plan data."""


class MigrationPlanGroundingError(MigrationPlanningError):
    """Raised when a plan refers to unsupported classes or graph nodes."""


class MigrationPlanDagError(MigrationPlanGroundingError):
    """Raised when the generated migration-step DAG is invalid."""


class RiskLevel(StrEnum):
    """Qualitative migration risk levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AcceptanceCriteria(BaseModel):
    """One human-readable condition that proves a migration step is complete."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)


class MigrationDependency(BaseModel):
    """A named dependency between migration steps."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1)
    depends_on: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class MigrationRisk(BaseModel):
    """A qualitative risk attached to the migration plan."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    risk_level: RiskLevel
    affected_steps: list[str] = Field(default_factory=list)

    @field_validator("affected_steps")
    @classmethod
    def validate_step_references(cls, values: list[str]) -> list[str]:
        """Reject blank risk step references."""
        if any(not value.strip() for value in values):
            raise ValueError("affected_steps entries must be non-empty")
        return values


class MigrationPhase(BaseModel):
    """A named group of migration steps."""

    model_config = ConfigDict(extra="forbid")

    phase_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    step_ids: list[str] = Field(min_length=1)

    @field_validator("step_ids")
    @classmethod
    def validate_step_ids(cls, values: list[str]) -> list[str]:
        """Reject blank step references in a phase."""
        if any(not value.strip() for value in values):
            raise ValueError("step_ids entries must be non-empty")
        return values


class MigrationStep(BaseModel):
    """One independently executable step in a migration plan."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1)
    phase_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    task_type: TaskType
    dependencies: list[str] = Field(default_factory=list)
    affected_classes: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    risk_level: RiskLevel
    requires_human_review: bool = False

    @field_validator(
        "dependencies",
        "affected_classes",
        "expected_outputs",
        "acceptance_criteria",
    )
    @classmethod
    def validate_text_items(cls, values: list[str]) -> list[str]:
        """Reject blank strings in all step-list fields."""
        if any(not value.strip() for value in values):
            raise ValueError("step list entries must be non-empty")
        return values


class MigrationPlan(BaseModel):
    """A validated, dependency-aware plan for one candidate service."""

    model_config = ConfigDict(extra="forbid")

    plan_id: UUID = Field(default_factory=uuid4)
    repository: str = Field(min_length=1)
    target_architecture: str = Field(min_length=1)
    candidate_service: str = Field(min_length=1)
    phases: list[MigrationPhase] = Field(min_length=1)
    steps: list[MigrationStep] = Field(min_length=1)
    risks: list[MigrationRisk] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    human_review_points: list[str] = Field(default_factory=list)
    estimated_task_count: int = Field(ge=0)
    model_provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)

    @field_validator("assumptions", "human_review_points")
    @classmethod
    def validate_optional_text_items(cls, values: list[str]) -> list[str]:
        """Reject blank assumptions and review points."""
        if any(not value.strip() for value in values):
            raise ValueError("text list entries must be non-empty")
        return values


class _ModelRouterLike(Protocol):
    """Minimal router protocol used for deterministic test doubles."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate one provider-neutral model response."""


class _ModelPlanOutput(BaseModel):
    """Strict model-facing schema; runtime metadata is assigned locally."""

    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1)
    target_architecture: str = Field(min_length=1)
    candidate_service: str = Field(min_length=1)
    phases: list[MigrationPhase] = Field(min_length=1)
    steps: list[MigrationStep] = Field(min_length=1)
    risks: list[MigrationRisk] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    human_review_points: list[str] = Field(default_factory=list)


class MigrationPlanningAgent(BaseAgent):
    """Create grounded migration plans without modifying application source."""

    name = "migration-planning"
    capabilities = frozenset({AgentCapability.MIGRATION_PLANNING})

    system_prompt = """You are MigrationSwarm's migration planning agent.
Create a concise executable software migration plan of roughly 6–10 steps for exactly
one selected candidate service.
Use only the supplied structured repository evidence. Never invent classes, services,
databases, APIs, or dependencies. Every affected class must exist in the supplied
architecture evidence, and every dependency must reference a real step_id.

Make steps small enough for specialized future agents to execute independently. Keep
independent work parallelizable where safe. Include explicit verification and testing.
Identify rollback-sensitive changes and require human review for risky architectural
decisions. Every step with risk_level "high" MUST set requires_human_review to true,
and any plan containing a high-risk step MUST include at least one human_review_points
entry. Do not assume database separation when no database evidence exists.
Planning must not generate code or modify application source; code changes happen in a
later phase. Use only these task types for executable steps: service_boundary_analysis,
dependency_analysis, architecture_analysis, code_refactor, test, debug, verify.

Return only JSON matching this schema:
{
  "repository": "repository path",
  "target_architecture": "target architecture summary",
  "candidate_service": "exact selected candidate name",
  "phases": [{"phase_id": "...", "title": "...", "description": "...", "step_ids": ["..."]}],
  "steps": [{
    "step_id": "unique id", "phase_id": "phase id", "title": "...",
    "description": "...", "task_type": "one allowed task type",
    "dependencies": ["real step ids"], "affected_classes": ["known fully qualified classes"],
    "expected_outputs": ["non-empty output"], "acceptance_criteria": ["non-empty criterion"],
    "risk_level": "low|medium|high", "requires_human_review": false
  }],
  "risks": [{"description": "...", "risk_level": "low|medium|high",
             "affected_steps": ["step ids"]}],
  "assumptions": ["non-empty assumption"],
  "human_review_points": ["required decisions or empty list"]
}
Phase step_ids must be copied exactly from the step objects; never invent a phase
step ID. Every step must appear in exactly one phase. Include both a testing step and
a final verification step before returning the JSON. The final verification step MUST
have task_type exactly "verify" (not "verification"), and at least one step in the
returned steps array must use that exact enum value.
"""

    def __init__(
        self,
        router: _ModelRouterLike | None = None,
        boundary_agent: ServiceBoundaryAgent | None = None,
    ) -> None:
        self.router = router or self._default_router()
        self.boundary_agent = boundary_agent or ServiceBoundaryAgent(self.router)

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        """Prepare evidence, generate one plan, validate it, and write artifacts."""
        started_at = datetime.now(UTC)
        if context.workspace_path is None:
            raise MigrationPlanningError("Migration planning requires workspace_path")
        service_name = context.metadata.get("candidate_service")
        if not isinstance(service_name, str) or not service_name.strip():
            raise SelectedServiceError(
                "Migration planning requires context.metadata['candidate_service']"
            )

        evidence = self.prepare_evidence(context.workspace_path, service_name)
        selected_name = evidence["selected_service"]["name"]
        plan = self._propose(evidence, selected_name)
        self._write_artifacts(Path(context.workspace_path).expanduser().resolve(), plan)
        tasks = self.to_tasks(plan)
        logger.info(
            "migration_plan_completed",
            provider=plan.model_provider,
            model=plan.model_name,
            candidate_service=plan.candidate_service,
            step_count=len(plan.steps),
        )
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=(
                f"Created migration plan for {plan.candidate_service} with "
                f"{len(plan.steps)} task steps."
            ),
            artifacts=[MIGRATION_PLAN_JSON_ARTIFACT, MIGRATION_PLAN_MARKDOWN_ARTIFACT],
            metadata={
                "migration_plan": plan.model_dump(mode="json"),
                "tasks": [item.model_dump(mode="json") for item in tasks],
            },
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    def prepare_evidence(
        self,
        workspace: str | Path,
        candidate_service: str,
        *,
        allow_model: bool = True,
    ) -> dict[str, Any]:
        """Load compact planning evidence and prepare deterministic prerequisites."""
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise MigrationPlanningError(f"Workspace is not a directory: {root}")
        boundary_path = root / SERVICE_BOUNDARIES_JSON_ARTIFACT
        if not boundary_path.is_file():
            if not allow_model:
                raise MissingMigrationEvidenceError(
                    f"Missing {SERVICE_BOUNDARIES_JSON_ARTIFACT}; dry-run cannot call a model"
                )
            self._run_service_boundary(root, candidate_service)

        boundary = self._load_model(boundary_path, ServiceBoundaryReport, "service boundary")
        selected = _select_candidate(boundary, candidate_service)
        if selected is None:
            raise SelectedServiceError(
                f"Selected candidate service is not present in boundary report: {candidate_service}"
            )

        architecture_path = root / ARCHITECTURE_REPORT_ARTIFACT
        dependency_path = root / JAVA_DEPENDENCY_ARTIFACT
        if not architecture_path.is_file():
            if not dependency_path.is_file():
                self._run_dependency_analysis(root)
            self._run_architecture_analysis(root)
        elif not dependency_path.is_file():
            self._run_dependency_analysis(root)

        architecture = self._load_model(
            architecture_path, ArchitectureReport, "architecture"
        )
        dependency_graph = self._load_model(
            dependency_path, JavaDependencyGraph, "dependency graph"
        )
        evidence = {
            "selected_service": selected.model_dump(mode="json"),
            "boundary_context": {
                "candidate_services": [
                    item.name for item in boundary.candidate_services
                ],
                "shared_components": [
                    item.model_dump(mode="json") for item in boundary.shared_components
                ],
                "unresolved_classes": boundary.unresolved_classes,
                "warnings": boundary.warnings,
            },
            "architecture": {
                "repository_root": architecture.repository_root,
                "classes": [
                    {
                        "fully_qualified_name": metric.fully_qualified_name,
                        "name": metric.name,
                        "package": metric.package,
                        "role": metric.role.value,
                        "fan_in": metric.fan_in,
                        "fan_out": metric.fan_out,
                    }
                    for metric in architecture.class_metrics
                ],
                "components": [
                    component.model_dump(mode="json")
                    for component in architecture.components
                ],
                "risks": [risk.model_dump(mode="json") for risk in architecture.risks],
                "cycle_count": architecture.cycle_count,
                "total_classes": architecture.total_classes,
                "total_dependencies": architecture.total_dependencies,
            },
            "dependencies": [
                {
                    "source": dependency.source,
                    "target": dependency.target,
                    "relationship": dependency.relationship.value,
                }
                for dependency in dependency_graph.dependencies
            ],
        }
        logger.info(
            "migration_planning_evidence_prepared",
            class_count=len(architecture.class_metrics),
            dependency_count=len(dependency_graph.dependencies),
            input_bytes=self.input_size(evidence),
        )
        return evidence

    @staticmethod
    def input_size(evidence: dict[str, Any]) -> int:
        """Return the compact UTF-8 JSON input size estimate."""
        return len(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    @staticmethod
    def to_tasks(plan: MigrationPlan) -> list[Task]:
        """Convert validated migration steps to pending tasks with stable UUIDs."""
        _validate_plan_structure(plan)
        project_id = uuid5(plan.plan_id, "migration-project")
        task_ids = {
            step.step_id: uuid5(plan.plan_id, f"migration-step:{step.step_id}")
            for step in plan.steps
        }
        tasks = [
            Task(
                id=task_ids[step.step_id],
                project_id=project_id,
                task_type=step.task_type,
                title=step.title,
                description=step.description,
                status=TaskStatus.PENDING,
                dependencies=[task_ids[dependency] for dependency in step.dependencies],
            )
            for step in plan.steps
        ]
        TaskGraph(tasks)
        return tasks

    @classmethod
    def render_task_dag(cls, plan: MigrationPlan) -> str:
        """Render the validated task graph as deterministic text."""
        tasks = cls.to_tasks(plan)
        graph = TaskGraph(tasks)
        by_id = {task.id: plan.steps[index] for index, task in enumerate(tasks)}
        lines = ["Migration Task DAG"]
        for task in graph.topological_order():
            lines.append(by_id[task.id].step_id)
            dependents = graph.get_direct_dependents(task.id)
            if dependents:
                lines.extend(f"    -> {by_id[item.id].step_id}" for item in dependents)
            else:
                lines.append("    (terminal)")
        return "\n".join(lines)

    def _propose(self, evidence: dict[str, Any], candidate_service: str) -> MigrationPlan:
        request = self._request(evidence)
        try:
            response = self.router.generate(request)
            output = self._parse_and_validate(response.content, evidence, candidate_service)
        except (MigrationPlanResponseError, MigrationPlanGroundingError) as first_error:
            repair_request = self._repair_request(
                request, response.content, validation_error=str(first_error)
            )
            try:
                repaired = self.router.generate(repair_request)
                output = self._parse_and_validate(
                    repaired.content, evidence, candidate_service
                )
                response = repaired
            except (MigrationPlanResponseError, MigrationPlanGroundingError) as second_error:
                raise MigrationPlanResponseError(
                    "Model migration plan failed validation after one repair attempt: "
                    f"{second_error}"
                ) from first_error
        return _build_plan(output, response)

    def _parse_and_validate(
        self,
        content: str,
        evidence: dict[str, Any],
        candidate_service: str,
    ) -> _ModelPlanOutput:
        try:
            require_bytes(content, MAX_MODEL_RESPONSE_BYTES, label="migration plan response")
            output = _ModelPlanOutput.model_validate(json.loads(_strip_json_fence(content)))
        except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as error:
            raise MigrationPlanResponseError(
                "Model output was not valid migration plan JSON"
            ) from error
        output = _normalize_phases(output)
        _validate_plan_output(output, evidence, candidate_service)
        return output

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
                        "Create a migration plan from only this structured evidence. "
                        f"{evidence_json}"
                    ),
                ),
            ],
            capability=ModelCapability.REASONING,
            temperature=0.1,
            max_tokens=3200,
            response_format={"type": "json_object"},
            metadata={"agent": self.name},
        )

    @staticmethod
    def _compact_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
        """Keep the planning prompt focused while preserving grounding context."""
        selected = evidence["selected_service"]
        selected_classes = set(selected["classes"])
        relevant_dependencies = [
            dependency
            for dependency in evidence["dependencies"]
            if dependency["source"] in selected_classes
            or dependency["target"] in selected_classes
        ]
        architecture = evidence["architecture"]
        selected_metrics = [
            metric
            for metric in architecture["classes"]
            if metric["fully_qualified_name"] in selected_classes
        ]
        return {
            "selected_service": selected,
            "boundary_context": evidence["boundary_context"],
            "selected_class_metrics": selected_metrics,
            "available_class_names": [
                metric["fully_qualified_name"] for metric in architecture["classes"]
            ],
            "dependencies": relevant_dependencies,
            "architecture_summary": {
                key: architecture[key]
                for key in (
                    "total_classes",
                    "total_dependencies",
                    "cycle_count",
                )
            },
        }

    @staticmethod
    def _repair_request(
        request: ModelRequest,
        invalid_output: str,
        *,
        validation_error: str,
    ) -> ModelRequest:
        return request.model_copy(
            update={
                "messages": [
                    *request.messages,
                    ModelMessage(
                        role=ModelRole.USER,
                        content=(
                            "Repair the following invalid migration plan. Return only JSON "
                            "matching the schema, using only supplied evidence. Fix this "
                            f"validation error: {validation_error} Ensure the returned "
                            'steps array includes a final step with task_type exactly "verify" '
                            "and includes a testing step. For every step whose risk_level is "
                            '"high", set requires_human_review to true and include at least '
                            "one non-empty human_review_points entry whenever any high-risk "
                            "step exists. Check these invariants before returning JSON.\n"
                            f"{invalid_output}"
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

    def _run_service_boundary(self, root: Path, candidate_service: str) -> None:
        task = _analysis_task(TaskType.SERVICE_BOUNDARY_ANALYSIS, self.boundary_agent.name)
        context = AgentContext(
            project_id=task.project_id,
            task=task,
            workspace_path=str(root),
        )
        self.boundary_agent.execute(task, context)
        if not (root / SERVICE_BOUNDARIES_JSON_ARTIFACT).is_file():
            raise MissingMigrationEvidenceError(
                f"Service boundary agent did not create {SERVICE_BOUNDARIES_JSON_ARTIFACT} "
                f"for {candidate_service}"
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
    def _load_model(path: Path, model_type: type[BaseModel], label: str) -> Any:
        try:
            return model_type.model_validate(load_json_object(path))
        except (ArtifactCorruptionError, ValidationError, ValueError) as error:
            raise MissingMigrationEvidenceError(
                f"Could not load {label} evidence: artifact is corrupt"
            ) from error

    @staticmethod
    def _write_artifacts(root: Path, plan: MigrationPlan) -> None:
        ensure_json_artifact_healthy(root / MIGRATION_PLAN_JSON_ARTIFACT)
        write_json_atomic(root / MIGRATION_PLAN_JSON_ARTIFACT, plan.model_dump(mode="json"))
        write_text_atomic(root / MIGRATION_PLAN_MARKDOWN_ARTIFACT, _markdown_report(plan))


def _build_plan(output: _ModelPlanOutput, response: ModelResponse) -> MigrationPlan:
    """Assign local plan identity and model metadata after validation."""
    return MigrationPlan(
        **output.model_dump(),
        estimated_task_count=len(output.steps),
        model_provider=response.provider,
        model_name=response.model,
    )


def _select_candidate(
    boundary: ServiceBoundaryReport, requested_name: str
) -> Any | None:
    """Resolve an exact candidate name and a small human-friendly alias."""
    requested = requested_name.strip().casefold()
    for candidate in boundary.candidate_services:
        normalized = candidate.name.casefold()
        if normalized == requested or normalized.removesuffix(" service") == requested:
            return candidate
    return None


def _validate_plan_output(
    output: _ModelPlanOutput,
    evidence: dict[str, Any],
    candidate_service: str,
) -> None:
    """Validate model output against repository evidence and the task DAG contract."""
    if output.candidate_service != candidate_service:
        raise MigrationPlanGroundingError(
            f"Plan candidate does not match selected service: {output.candidate_service}"
        )
    known_classes = {
        item["fully_qualified_name"] for item in evidence["architecture"]["classes"]
    }
    for step in output.steps:
        for class_name in step.affected_classes:
            if class_name not in known_classes:
                raise MigrationPlanGroundingError(f"Unknown affected class: {class_name}")
    plan = MigrationPlan(
        **output.model_dump(),
        estimated_task_count=len(output.steps),
        model_provider="validation",
        model_name="validation",
    )
    _validate_plan_structure(plan)
    if not any(step.task_type is TaskType.VERIFY for step in plan.steps):
        raise MigrationPlanGroundingError("Migration plan must include a verification step")
    if any(step.task_type is TaskType.CODE_REFACTOR for step in plan.steps) and not any(
        step.task_type is TaskType.TEST for step in plan.steps
    ):
        raise MigrationPlanGroundingError(
            "Migration plan with code-changing work must include a testing step"
        )
    high_risk_steps = [step for step in plan.steps if step.risk_level is RiskLevel.HIGH]
    if high_risk_steps and (
        not plan.human_review_points
        or any(not step.requires_human_review for step in high_risk_steps)
    ):
        raise MigrationPlanGroundingError(
            "Every HIGH-risk step must require human review and have review points"
        )


def _normalize_phases(output: _ModelPlanOutput) -> _ModelPlanOutput:
    """Reconcile phase membership from the authoritative validated step objects."""
    step_ids = {step.step_id for step in output.steps}
    steps_by_phase: dict[str, list[str]] = {}
    for step in output.steps:
        steps_by_phase.setdefault(step.phase_id, []).append(step.step_id)

    phases: list[MigrationPhase] = []
    represented: set[str] = set()
    for phase in output.phases:
        members = [step_id for step_id in phase.step_ids if step_id in step_ids]
        members.extend(
            step_id
            for step_id in steps_by_phase.get(phase.phase_id, [])
            if step_id not in members
        )
        if members:
            phases.append(phase.model_copy(update={"step_ids": members}))
            represented.update(members)

    for phase_id, members in steps_by_phase.items():
        if any(step_id not in represented for step_id in members):
            phases.append(
                MigrationPhase(
                    phase_id=phase_id,
                    title=phase_id.replace("_", " ").title(),
                    description="Phase derived from validated migration steps.",
                    step_ids=[step_id for step_id in members if step_id not in represented],
                )
            )
    return output.model_copy(update={"phases": phases})


def _validate_plan_structure(plan: MigrationPlan) -> None:
    """Validate IDs, phase references, and the shared task DAG."""
    step_ids = [step.step_id for step in plan.steps]
    if len(step_ids) != len(set(step_ids)):
        raise MigrationPlanDagError("Migration step IDs must be unique")
    step_by_id = {step.step_id: step for step in plan.steps}
    phase_ids = [phase.phase_id for phase in plan.phases]
    if len(phase_ids) != len(set(phase_ids)):
        raise MigrationPlanDagError("Migration phase IDs must be unique")
    assigned_steps: set[str] = set()
    for phase in plan.phases:
        for step_id in phase.step_ids:
            if step_id not in step_by_id:
                raise MigrationPlanDagError(f"Phase references unknown step: {step_id}")
            if step_id in assigned_steps:
                raise MigrationPlanDagError(f"Step assigned to multiple phases: {step_id}")
            assigned_steps.add(step_id)
    if assigned_steps != set(step_ids):
        raise MigrationPlanDagError("Every migration step must belong to a phase")
    for step in plan.steps:
        if step.phase_id not in set(phase_ids):
            raise MigrationPlanDagError(f"Step references unknown phase: {step.phase_id}")
        unknown = set(step.dependencies) - set(step_ids)
        if unknown:
            raise MigrationPlanDagError(
                f"Step {step.step_id} references unknown dependencies: {sorted(unknown)}"
            )
        if step.step_id in step.dependencies:
            raise MigrationPlanDagError(f"Step cannot depend on itself: {step.step_id}")
    if plan.estimated_task_count != len(plan.steps):
        raise MigrationPlanDagError("estimated_task_count must equal the number of steps")
    project_id = uuid5(plan.plan_id, "migration-project")
    tasks = [
        Task(
            id=uuid5(plan.plan_id, f"migration-step:{step.step_id}"),
            project_id=project_id,
            task_type=step.task_type,
            title=step.title,
            description=step.description,
            dependencies=[
                uuid5(plan.plan_id, f"migration-step:{dependency}")
                for dependency in step.dependencies
            ],
        )
        for step in plan.steps
    ]
    try:
        TaskGraph(tasks)
    except ValueError as error:
        raise MigrationPlanDagError(
            f"Migration step dependency graph is invalid: {error}"
        ) from error


def _analysis_task(task_type: TaskType, agent_name: str) -> Task:
    """Create a ready internal prerequisite task."""
    return Task(
        project_id=uuid4(),
        task_type=task_type,
        title=task_type.value.replace("_", " ").title(),
        description="Prepare structured evidence for migration planning.",
        status=TaskStatus.READY,
        assigned_agent=agent_name,
    )


def _strip_json_fence(content: str) -> str:
    """Accept a common fenced JSON response."""
    stripped = content.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        return stripped[7:-3].strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped[3:-3].strip()
    return stripped


def _markdown_report(plan: MigrationPlan) -> str:
    """Render a concise human-readable migration plan."""
    lines = [
        "# Migration Plan",
        "",
        f"- Repository: {plan.repository}",
        f"- Candidate service: {plan.candidate_service}",
        f"- Target architecture: {plan.target_architecture}",
        f"- Estimated tasks: {plan.estimated_task_count}",
        "",
        "## Phases",
        "",
    ]
    for phase in plan.phases:
        lines.extend([f"### {phase.title} ({phase.phase_id})", "", phase.description, ""])
        for step_id in phase.step_ids:
            lines.append(f"- {step_id}")
    lines.extend(["## Ordered steps", ""])
    for step in plan.steps:
        dependencies = ", ".join(step.dependencies) or "None"
        classes = ", ".join(step.affected_classes) or "None"
        lines.extend(
            [
                f"### {step.step_id}: {step.title}",
                "",
                f"- Task type: {step.task_type.value}",
                f"- Dependencies: {dependencies}",
                f"- Affected classes: {classes}",
                f"- Risk: {step.risk_level.value}",
                f"- Human review: {'yes' if step.requires_human_review else 'no'}",
                f"- Description: {step.description}",
                "- Expected outputs:",
                *(f"  - {item}" for item in step.expected_outputs),
                "- Acceptance criteria:",
                *(f"  - {item}" for item in step.acceptance_criteria),
                "",
            ]
        )
    lines.extend(["## Risks", ""])
    lines.extend(
        f"- {risk.risk_level.value}: {risk.description}" for risk in plan.risks
    )
    if not plan.risks:
        lines.append("- None")
    lines.extend(["", "## Human review points", ""])
    lines.extend(f"- {point}" for point in plan.human_review_points)
    if not plan.human_review_points:
        lines.append("- None")
    return "\n".join(lines) + "\n"


__all__ = [
    "AcceptanceCriteria",
    "MigrationDependency",
    "MigrationPhase",
    "MigrationPlan",
    "MigrationPlanDagError",
    "MigrationPlanGroundingError",
    "MigrationPlanResponseError",
    "MigrationPlanningAgent",
    "MigrationPlanningError",
    "MigrationRisk",
    "MigrationStep",
    "MIGRATION_PLAN_JSON_ARTIFACT",
    "MIGRATION_PLAN_MARKDOWN_ARTIFACT",
    "MissingMigrationEvidenceError",
    "RiskLevel",
    "SelectedServiceError",
]
