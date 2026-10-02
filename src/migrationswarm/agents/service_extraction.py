"""Copy-first Spring Boot service extraction inside isolated task worktrees."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from migrationswarm.agents.architecture_analysis import (
    ARCHITECTURE_REPORT_ARTIFACT,
    ArchitectureReport,
)
from migrationswarm.agents.dependency_analysis import (
    JAVA_DEPENDENCY_ARTIFACT,
    JavaClass,
    JavaClassRole,
    JavaDependencyGraph,
)
from migrationswarm.agents.migration_planning import (
    MIGRATION_PLAN_JSON_ARTIFACT,
    MigrationPlan,
)
from migrationswarm.agents.service_boundary import (
    SERVICE_BOUNDARIES_JSON_ARTIFACT,
    CandidateService,
    ServiceBoundaryReport,
)
from migrationswarm.core.agents import AgentCapability, AgentContext, AgentResult, BaseAgent
from migrationswarm.core.git import GitRepository, GitRepositoryError, GitWorktreeManager
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
    PathSafetyError,
    ensure_json_artifact_healthy,
    load_json_object,
    normalize_relative_path,
    require_bytes,
    safe_join,
    write_json_atomic,
)
from migrationswarm.core.tasks import Task
from migrationswarm.core.workspaces import TaskWorkspace

logger = structlog.get_logger(__name__)

EXTRACTION_RESULTS_DIR: Final[str] = ".migrationswarm/extraction-results"
DEFAULT_TARGET_ROOT: Final[str] = "services"
_SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".java",
        ".xml",
        ".yml",
        ".yaml",
        ".properties",
        ".md",
        ".gradle",
        ".kts",
        ".json",
    }
)
_JAVA_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_JAVA_TYPE = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_]\w*)")
_SLUG_PART = re.compile(r"[^a-z0-9]+")


class ServiceExtractionError(ValueError):
    """Base exception for safe service extraction."""


class ExtractionWorkspaceError(ServiceExtractionError):
    """Raised when the task does not point to its managed worktree."""


class ExtractionEvidenceError(ServiceExtractionError):
    """Raised when required boundary or planning evidence is unavailable."""


class SelectedServiceNotFoundError(ExtractionEvidenceError):
    """Raised when the requested candidate is absent from boundary evidence."""


class ExtractionResponseError(ServiceExtractionError):
    """Raised when model output is malformed after one repair attempt."""


class ExtractionSafetyError(ServiceExtractionError):
    """Raised when generated content violates extraction safety rules."""


class ExtractionWriteError(ServiceExtractionError):
    """Raised when generated files cannot be applied atomically."""


def _dirty_worktree_message(repository: GitRepository, workspace: TaskWorkspace) -> str:
    """Describe every relevant Git status category without relaxing cleanliness."""
    porcelain = repository.status_porcelain()
    ignored_status = repository.status_porcelain(include_ignored=True)
    entries = [line for line in ignored_status.splitlines() if len(line) >= 4]
    tracked_modified: list[str] = []
    staged: list[str] = []
    untracked: list[str] = []
    ignored: list[str] = []
    for line in entries:
        index_status, worktree_status = line[0], line[1]
        path = line[3:]
        if line[:2] == "??":
            untracked.append(path)
        elif line[:2] == "!!":
            ignored.append(path)
        else:
            tracked_modified.append(path)
            if index_status != " ":
                staged.append(path)
            if worktree_status != " ":
                tracked_modified.append(path)
    return (
        "Extraction worktree must be clean at the start; "
        f"worktree_path={workspace.workspace_path}; "
        f"starting_commit={workspace.base_commit}; "
        f"current_commit={repository.head_commit()}; "
        f"git_status_porcelain={porcelain.strip() or '<clean>'!r}; "
        f"tracked_modified={sorted(set(tracked_modified))!r}; "
        f"staged={sorted(set(staged))!r}; "
        f"untracked={sorted(set(untracked))!r}; "
        f"ignored={sorted(set(ignored))!r}"
    )


def _evidence_root(context: AgentContext, workspace: TaskWorkspace) -> Path:
    """Resolve optional read-only evidence from the main repository metadata root."""
    raw_root = context.metadata.get("evidence_root")
    if raw_root is None:
        return workspace.workspace_path
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ExtractionEvidenceError("evidence_root must be a non-empty path")
    candidate = Path(raw_root).expanduser().resolve()
    if candidate != workspace.repository_root.expanduser().resolve():
        raise ExtractionEvidenceError(
            "evidence_root must be the managed worktree's repository root"
        )
    return candidate


class ExtractionDependencyKind(StrEnum):
    """Deterministic classification for dependencies outside the candidate."""

    LOCAL_IMPLEMENTATION = "local_implementation"
    CROSS_SERVICE_API = "external_service_dependency"
    EXTERNAL_SERVICE_DEPENDENCY = "external_service_dependency"
    SHARED_CODE_CANDIDATE = "shared_code_candidate"
    DATA_DEPENDENCY = "data_dependency"
    FOREIGN_REPOSITORY = "foreign_repository"
    UNRESOLVED_DEPENDENCY = "unresolved_dependency"


class ExtractionLimits(BaseModel):
    """Fixed bounds on model context and generated service size."""

    model_config = ConfigDict(extra="forbid")

    target_root: str = DEFAULT_TARGET_ROOT
    max_context_files: int = Field(
        default=16,
        ge=1,
        description="Fixed extraction context ceiling; required files are never truncated.",
    )
    max_context_bytes_per_file: int = Field(default=16_000, ge=1)
    max_total_context_bytes: int = Field(default=60_000, ge=1)
    max_generated_files: int = Field(default=20, ge=1)
    max_total_generated_bytes: int = Field(default=120_000, ge=1)

    @field_validator("target_root")
    @classmethod
    def validate_target_root(cls, value: str) -> str:
        """Keep the configurable root relative and safe."""
        normalized = _safe_relative_path(value)
        if "/" in normalized or normalized in {".", ".."}:
            raise ValueError("target_root must be one safe relative directory")
        return normalized


class ExtractionDependency(BaseModel):
    """One grounded dependency outside the selected candidate."""

    model_config = ConfigDict(extra="forbid")

    source_class: str = Field(min_length=1)
    target_class: str = Field(min_length=1)
    classification: ExtractionDependencyKind
    reason: str = Field(min_length=1)


class ExtractionWarning(BaseModel):
    """One explicit limitation or unresolved extraction concern."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ExtractionSourceFile(BaseModel):
    """Bounded source context for one selected candidate class."""

    model_config = ConfigDict(extra="forbid")

    class_name: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    package: str = Field(min_length=1)
    imports: list[str] = Field(default_factory=list)
    content: str = Field(min_length=1)


class ExtractionContextSelection(BaseModel):
    """Deterministic accounting for files selected for extraction context."""

    model_config = ConfigDict(extra="forbid")

    required_file_count: int = Field(ge=0)
    optional_file_count: int = Field(ge=0)
    selected_file_count: int = Field(ge=0)
    configured_limit: int = Field(ge=1)
    selected_files_by_reason: dict[str, list[str]] = Field(default_factory=dict)
    excluded_files_by_reason: dict[str, list[str]] = Field(default_factory=dict)


class ServiceExtractionContext(BaseModel):
    """Strict, bounded context sent to the coding model."""

    model_config = ConfigDict(extra="forbid")

    candidate_service: CandidateService
    selected_classes: list[JavaClass] = Field(min_length=1)
    source_files: list[ExtractionSourceFile] = Field(min_length=1)
    architecture_metrics: list[dict[str, Any]] = Field(default_factory=list)
    dependencies: list[ExtractionDependency] = Field(default_factory=list)
    package_structure: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    target_service_directory: str = Field(min_length=1)
    warnings: list[ExtractionWarning] = Field(default_factory=list)
    context_selection: ExtractionContextSelection

    @property
    def context_bytes(self) -> int:
        """Return the compact JSON size supplied to the model."""
        return len(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
        )


class GeneratedFile(BaseModel):
    """One complete file to create inside the new service only."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(min_length=1)
    complete_content: str
    purpose: str = Field(min_length=1)


class ServiceExtractionProposal(BaseModel):
    """Strict model output for copy-first service generation."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    generated_files: list[GeneratedFile] = Field(min_length=1)
    copied_classes: list[str] = Field(min_length=1)
    generated_support_classes: list[str] = Field(default_factory=list)
    dependencies: list[ExtractionDependency] = Field(default_factory=list)
    warnings: list[ExtractionWarning] = Field(default_factory=list)


class _ModelRouterLike(Protocol):
    """Minimal model-router contract used by tests and production routing."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate one provider-neutral response."""


class ServiceExtractionAgent(BaseAgent):
    """Generate a new Spring Boot service without deleting monolith source."""

    name = "service-extraction"
    capabilities = frozenset({AgentCapability.SERVICE_EXTRACTION})

    system_prompt = """You are MigrationSwarm's copy-first Spring Boot extraction agent.
Generate a small independently buildable service for exactly the selected candidate.
Use only the bounded source and structured evidence supplied. Copy or adapt selected
controller, service, repository, and grounded support classes; never delete or move
monolith files. Do not generate databases, schemas, SQL, Docker, Kubernetes, messaging,
deployment, or unrelated classes. Preserve unresolved and shared dependencies as explicit
warnings or dependency records. Every generated file must be under the target service
directory and contain complete UTF-8 content. The new service must be independently
buildable: always include one complete build descriptor under the target directory,
preferably pom.xml for Maven or a complete build.gradle/build.gradle.kts for Gradle.
Include a test source when the evidence and acceptance criteria call for tests.

Return only strict JSON:
{
  "summary": "short extraction summary",
  "generated_files": [{
    "relative_path": "src/main/java/package/Type.java",
    "complete_content": "complete file content",
    "purpose": "why this file belongs in the new service"
  }],
  "copied_classes": ["fully.qualified.SelectedClass"],
  "generated_support_classes": [],
  "dependencies": [{
    "source_class": "selected class",
    "target_class": "external class",
    "classification": "external_service_dependency|shared_code_candidate|unresolved_dependency",
    "reason": "grounded reason"
  }],
  "warnings": [{"code": "warning_code", "message": "warning"}]
}"""

    def __init__(
        self,
        router: _ModelRouterLike | None = None,
        *,
        limits: ExtractionLimits | None = None,
    ) -> None:
        self.router = router or self._default_router()
        self.limits = limits or ExtractionLimits()

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        """Validate evidence, generate files, apply them atomically, and report."""
        started_at = datetime.now(UTC)
        workspace = self._require_workspace(task, context)
        repository = GitRepository(workspace.workspace_path)
        if repository.is_dirty():
            raise ExtractionWorkspaceError(_dirty_worktree_message(repository, workspace))
        extraction_context = self.build_context(task, context, workspace)
        proposal, response = self._propose(extraction_context)
        normalized = self._validate_proposal(proposal, extraction_context, workspace)
        generated_paths = self._apply_files(
            workspace.workspace_path, normalized, extraction_context
        )
        changed_files = list(repository.changed_files())
        result_data = self._result_data(
            task,
            extraction_context,
            normalized,
            generated_paths,
            changed_files,
            response,
        )
        artifact = self._write_result_artifact(workspace, task.id, result_data)
        logger.info(
            "service_extraction_completed",
            candidate=extraction_context.candidate_service.name,
            generated_file_count=len(generated_paths),
        )
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=(
                f"Generated {len(generated_paths)} files for "
                f"{extraction_context.candidate_service.name}; monolith source was preserved."
            ),
            artifacts=[artifact],
            metadata={"extraction_result": result_data},
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    def dry_run(self, task: Task, context: AgentContext) -> AgentResult:
        """Validate evidence and estimate extraction without calling the model or writing."""
        started_at = datetime.now(UTC)
        workspace = self._require_workspace(task, context)
        if GitRepository(workspace.workspace_path).is_dirty():
            repository = GitRepository(workspace.workspace_path)
            raise ExtractionWorkspaceError(_dirty_worktree_message(repository, workspace))
        extraction_context = self.build_context(task, context, workspace)
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=(
                f"Dry-run selected {extraction_context.candidate_service.name}; "
                f"target is {extraction_context.target_service_directory}."
            ),
            metadata={
                "dry_run": True,
                "selected_service": extraction_context.candidate_service.name,
                "selected_classes": [
                    java_class.fully_qualified_name
                    for java_class in extraction_context.selected_classes
                ],
                "target_service_directory": extraction_context.target_service_directory,
                "context_bytes": extraction_context.context_bytes,
            },
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    def build_context(
        self,
        task: Task,
        context: AgentContext,
        workspace: TaskWorkspace | None = None,
    ) -> ServiceExtractionContext:
        """Load only the selected candidate's bounded evidence and source files."""
        managed_workspace = workspace or self._require_workspace(task, context)
        service_name = context.metadata.get("service_name")
        if not isinstance(service_name, str) or not service_name.strip():
            raise SelectedServiceNotFoundError(
                "ServiceExtractionAgent requires metadata.service_name"
            )
        root = managed_workspace.workspace_path
        evidence_root = _evidence_root(context, managed_workspace)
        boundary = self._load_artifact(
            evidence_root / SERVICE_BOUNDARIES_JSON_ARTIFACT, ServiceBoundaryReport
        )
        candidate = _select_candidate(boundary, service_name)
        if candidate is None:
            raise SelectedServiceNotFoundError(
                f"Selected candidate service is not present in boundary evidence: {service_name}"
            )
        plan_payload = context.metadata.get("migration_plan")
        plan = (
            MigrationPlan.model_validate(plan_payload)
            if isinstance(plan_payload, dict)
            else self._load_artifact(evidence_root / MIGRATION_PLAN_JSON_ARTIFACT, MigrationPlan)
        )
        if _select_candidate_name(plan.candidate_service) != _select_candidate_name(candidate.name):
            raise ExtractionEvidenceError(
                "Migration plan candidate does not match selected service: "
                f"{plan.candidate_service}"
            )
        graph = self._load_artifact(
            evidence_root / JAVA_DEPENDENCY_ARTIFACT, JavaDependencyGraph
        )
        architecture = self._load_artifact(
            evidence_root / ARCHITECTURE_REPORT_ARTIFACT, ArchitectureReport
        )
        classes_by_name = {
            java_class.fully_qualified_name: java_class for java_class in graph.classes
        }
        selected_classes: list[JavaClass] = []
        for class_name in candidate.classes:
            java_class = classes_by_name.get(class_name)
            if java_class is None:
                raise ExtractionEvidenceError(
                    f"Selected candidate class is missing from dependency evidence: {class_name}"
                )
            selected_classes.append(java_class)

        context_classes, context_selection = _select_context_classes(
            candidate, graph, boundary, classes_by_name, self.limits
        )
        required_names = set(candidate.classes)
        source_files: list[ExtractionSourceFile] = []
        total_bytes = 0
        for java_class in context_classes:
            source_path = _safe_workspace_path(java_class.file_path, root)
            if not source_path.is_file():
                raise ExtractionEvidenceError(
                    f"Selected source file does not exist: {java_class.file_path}"
                )
            content = source_path.read_text(encoding="utf-8")
            byte_count = len(content.encode("utf-8"))
            if byte_count > self.limits.max_context_bytes_per_file:
                if java_class.fully_qualified_name not in required_names:
                    _exclude_selected_file(
                        context_selection,
                        java_class.file_path,
                        "optional_file_exceeds_per_file_limit",
                    )
                    continue
                raise ExtractionEvidenceError(
                    f"Selected source file exceeds context limit: {java_class.file_path}"
                )
            if total_bytes + byte_count > self.limits.max_total_context_bytes:
                if java_class.fully_qualified_name not in required_names:
                    _exclude_selected_file(
                        context_selection,
                        java_class.file_path,
                        "optional_file_exceeds_total_byte_limit",
                    )
                    continue
                raise ExtractionEvidenceError("Selected source context exceeds total byte limit")
            total_bytes += byte_count
            source_files.append(
                ExtractionSourceFile(
                    class_name=java_class.fully_qualified_name,
                    relative_path=java_class.file_path,
                    package=java_class.package,
                    imports=list(java_class.imports),
                    content=content,
                )
            )

        selected_names = {item.fully_qualified_name for item in selected_classes}
        shared_names = {item.class_name for item in boundary.shared_components}
        dependencies = _external_dependencies(graph, selected_names, shared_names, classes_by_name)
        warnings = [
            ExtractionWarning(code="boundary_warning", message=warning)
            for warning in boundary.warnings
        ]
        if any(item.role is JavaClassRole.REPOSITORY for item in selected_classes):
            warnings.append(
                ExtractionWarning(
                    code="database_ownership_unresolved",
                    message=(
                        "Repository persistence is copied conservatively; database splitting "
                        "and new schemas are out of scope."
                    ),
                )
            )
        criteria = _acceptance_criteria(plan, selected_names)
        metrics = [
            metric.model_dump(mode="json")
            for metric in architecture.class_metrics
            if metric.fully_qualified_name in selected_names
        ]
        target_directory = f"{self.limits.target_root}/{service_slug(candidate.name)}"
        return ServiceExtractionContext(
            candidate_service=candidate,
            selected_classes=selected_classes,
            source_files=source_files,
            architecture_metrics=metrics,
            dependencies=dependencies,
            package_structure=sorted({item.package for item in selected_classes}),
            acceptance_criteria=criteria,
            target_service_directory=target_directory,
            warnings=warnings,
            context_selection=context_selection,
        )

    def _propose(
        self, extraction_context: ServiceExtractionContext
    ) -> tuple[ServiceExtractionProposal, ModelResponse]:
        request = self._request(extraction_context)
        try:
            response = self.router.generate(request)
            proposal = self._parse(response.content)
        except ExtractionResponseError as first_error:
            if len(response.content.encode("utf-8")) > MAX_MODEL_RESPONSE_BYTES:
                raise first_error
            repair_request = request.model_copy(
                update={
                    "messages": [
                        *request.messages,
                        ModelMessage(
                            role=ModelRole.USER,
                            content=(
                                "Repair the invalid extraction response. Return only strict "
                                "JSON matching the schema; keep files under the selected "
                                "service and never delete monolith source.\n"
                                f"{response.content}"
                            ),
                        ),
                    ]
                }
            )
            try:
                response = self.router.generate(repair_request)
                proposal = self._parse(response.content)
            except ExtractionResponseError as second_error:
                raise ExtractionResponseError(
                    "Model extraction response failed after one repair attempt: "
                    f"{second_error}"
                ) from first_error
        return proposal, response

    def _request(self, extraction_context: ServiceExtractionContext) -> ModelRequest:
        evidence_json = json.dumps(
            extraction_context.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return ModelRequest(
            messages=[
                ModelMessage(role=ModelRole.SYSTEM, content=self.system_prompt),
                ModelMessage(
                    role=ModelRole.USER,
                    content=(
                        "Generate the copy-first service from only this bounded context:\n"
                        f"{evidence_json}"
                    ),
                ),
            ],
            capability=ModelCapability.CODING,
            temperature=0.1,
            max_tokens=3500,
            response_format={"type": "json_object"},
            metadata={"agent": self.name},
        )

    @staticmethod
    def _parse(content: str) -> ServiceExtractionProposal:
        try:
            require_bytes(content, MAX_MODEL_RESPONSE_BYTES, label="extraction response")
            return ServiceExtractionProposal.model_validate(
                json.loads(_strip_json_fence(content))
            )
        except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as error:
            raise ExtractionResponseError("Model output was not valid extraction JSON") from error

    def _validate_proposal(
        self,
        proposal: ServiceExtractionProposal,
        extraction_context: ServiceExtractionContext,
        workspace: TaskWorkspace,
    ) -> ServiceExtractionProposal:
        if len(proposal.generated_files) > self.limits.max_generated_files:
            raise ExtractionSafetyError(
                f"Generated file count exceeds limit: {len(proposal.generated_files)}"
            )
        selected_names = {item.fully_qualified_name for item in extraction_context.selected_classes}
        if not selected_names.issubset(set(proposal.copied_classes)):
            raise ExtractionSafetyError("Proposal does not declare all selected classes as copied")
        generated_class_names: set[str] = set()
        normalized_paths: set[str] = set()
        has_build_descriptor = False
        total_bytes = 0
        for generated in proposal.generated_files:
            relative = _normalize_generated_path(
                generated.relative_path,
                extraction_context.target_service_directory,
            )
            if relative in normalized_paths:
                raise ExtractionSafetyError(f"Duplicate generated path: {relative}")
            normalized_paths.add(relative)
            if Path(relative).name in {"pom.xml", "build.gradle", "build.gradle.kts"}:
                has_build_descriptor = True
            if not generated.complete_content.strip():
                raise ExtractionSafetyError(f"Generated file is empty: {relative}")
            byte_count = len(generated.complete_content.encode("utf-8"))
            if byte_count > self.limits.max_total_generated_bytes:
                raise ExtractionSafetyError(f"Generated file exceeds byte limit: {relative}")
            total_bytes += byte_count
            if total_bytes > self.limits.max_total_generated_bytes:
                raise ExtractionSafetyError("Generated content exceeds total byte limit")
            try:
                target = safe_join(workspace.workspace_path, relative)
            except PathSafetyError as error:
                raise ExtractionSafetyError(
                    f"Generated path is protected or outside worktree: {relative}"
                ) from error
            if target.exists():
                raise ExtractionSafetyError(
                    f"Generated file would overwrite an existing file: {relative}"
                )
            if target.suffix.lower() not in _SUPPORTED_EXTENSIONS or (
                target.suffix.lower() == ".json"
                and target.name != "behavior-contract.json"
            ):
                raise ExtractionSafetyError(f"Unsupported generated file extension: {relative}")
            generated_class_names.update(
                _validate_java_package(relative, generated.complete_content)
            )
        if not has_build_descriptor:
            raise ExtractionSafetyError(
                "Generated service must include pom.xml or a Gradle build descriptor"
            )
        allowed_class_names = selected_names | set(proposal.generated_support_classes)
        undeclared = generated_class_names - allowed_class_names
        if undeclared:
            raise ExtractionSafetyError(
                "Generated support classes must be declared: "
                + ", ".join(sorted(undeclared))
            )
        missing_selected = selected_names - generated_class_names
        if missing_selected:
            raise ExtractionSafetyError(
                "Selected classes are not present in generated Java files: "
                + ", ".join(sorted(missing_selected))
            )
        missing_support = set(proposal.generated_support_classes) - generated_class_names
        if missing_support:
            raise ExtractionSafetyError(
                "Declared support classes are not present in generated Java files: "
                + ", ".join(sorted(missing_support))
            )
        return proposal

    def _apply_files(
        self,
        workspace: Path,
        proposal: ServiceExtractionProposal,
        extraction_context: ServiceExtractionContext,
    ) -> list[str]:
        originals: dict[Path, bytes | None] = {}
        created_files: list[Path] = []
        created_dirs: list[Path] = []
        try:
            paths = [
                safe_join(
                    workspace,
                    _normalize_generated_path(
                        generated.relative_path,
                        extraction_context.target_service_directory,
                    ),
                )
                for generated in proposal.generated_files
            ]
            for path in paths:
                originals[path] = path.read_bytes() if path.exists() else None
            for generated, target in zip(proposal.generated_files, paths, strict=True):
                if not target.parent.exists():
                    missing_dirs: list[Path] = []
                    current = target.parent
                    while not current.exists() and current != workspace:
                        missing_dirs.append(current)
                        current = current.parent
                    target.parent.mkdir(parents=True, exist_ok=True)
                    created_dirs.extend(reversed(missing_dirs))
                self._atomic_write(target, generated.complete_content)
                created_files.append(target)
            return [str(path.relative_to(workspace).as_posix()) for path in paths]
        except OSError as error:
            self._rollback(originals, created_files, created_dirs)
            raise ExtractionWriteError(
                f"Extraction write failed and was rolled back: {error}"
            ) from error

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary_path: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_directory = path.parent
            while len(str(temporary_directory)) > 180:
                parent = temporary_directory.parent
                if parent == temporary_directory:
                    break
                temporary_directory = parent
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=temporary_directory,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary_path = temporary.name
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and Path(temporary_path).exists():
                Path(temporary_path).unlink()

    @staticmethod
    def _rollback(
        originals: Mapping[Path, bytes | None],
        created_files: list[Path],
        created_dirs: list[Path],
    ) -> None:
        for path in reversed(created_files):
            try:
                if originals.get(path) is None and path.exists():
                    path.unlink()
                elif originals.get(path) is not None:
                    path.write_bytes(originals[path] or b"")
            except OSError:
                logger.exception("service_extraction_rollback_failed", path=str(path))
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def _write_result_artifact(
        workspace: TaskWorkspace,
        task_id: Any,
        result_data: dict[str, Any],
    ) -> str:
        artifact = workspace.repository_root / EXTRACTION_RESULTS_DIR / f"{task_id}.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        ensure_json_artifact_healthy(artifact)
        write_json_atomic(artifact, result_data)
        return str(artifact.relative_to(workspace.repository_root).as_posix())

    @staticmethod
    def _result_data(
        task: Task,
        extraction_context: ServiceExtractionContext,
        proposal: ServiceExtractionProposal,
        generated_paths: list[str],
        changed_files: list[str],
        response: ModelResponse,
    ) -> dict[str, Any]:
        dependencies = extraction_context.dependencies
        return {
            "task_id": str(task.id),
            "selected_candidate": extraction_context.candidate_service.model_dump(mode="json"),
            "target_service_directory": extraction_context.target_service_directory,
            "generated_files": [
                {
                    "relative_path": generated.relative_path,
                    "purpose": generated.purpose,
                    "content_bytes": len(generated.complete_content.encode("utf-8")),
                    "normalized_path": path,
                }
                for generated, path in zip(proposal.generated_files, generated_paths, strict=True)
            ],
            "copied_or_adapted_classes": proposal.copied_classes,
            "unresolved_dependencies": [
                dependency.model_dump(mode="json")
                for dependency in dependencies
                if dependency.classification is ExtractionDependencyKind.UNRESOLVED_DEPENDENCY
            ],
            "shared_dependency_candidates": [
                dependency.model_dump(mode="json")
                for dependency in dependencies
                if dependency.classification is ExtractionDependencyKind.SHARED_CODE_CANDIDATE
            ],
            "external_service_dependencies": [
                dependency.model_dump(mode="json")
                for dependency in dependencies
                if dependency.classification is ExtractionDependencyKind.EXTERNAL_SERVICE_DEPENDENCY
            ],
            "warnings": [
                warning.model_dump(mode="json")
                for warning in [*extraction_context.warnings, *proposal.warnings]
            ],
            "changed_files": changed_files,
            "model_provider": response.provider,
            "model_name": response.model,
        }

    def _require_workspace(self, task: Task, context: AgentContext) -> TaskWorkspace:
        if context.workspace_path is None:
            raise ExtractionWorkspaceError("ServiceExtractionAgent requires workspace_path")
        workspace_path = Path(context.workspace_path).expanduser().resolve()
        if not workspace_path.is_dir():
            raise ExtractionWorkspaceError(f"Workspace is not a directory: {workspace_path}")
        if workspace_path.name != str(task.id):
            raise ExtractionWorkspaceError("Workspace directory must match the task ID")
        worktrees = workspace_path.parent
        metadata_dir = worktrees.parent
        if worktrees.name != "worktrees" or metadata_dir.name != ".migrationswarm":
            raise ExtractionWorkspaceError(
                "Workspace must be inside .migrationswarm/worktrees/<task-id>"
            )
        try:
            repository = GitRepository.from_path(metadata_dir.parent)
            managed = GitWorktreeManager(repository).task_workspace(task.id)
            independent_root = GitRepository.from_path(workspace_path).root
        except GitRepositoryError as error:
            raise ExtractionWorkspaceError(
                f"Could not validate task worktree: {workspace_path}"
            ) from error
        if managed.workspace_path != workspace_path or independent_root != workspace_path:
            raise ExtractionWorkspaceError("Workspace is not the managed independent worktree")
        return managed

    @staticmethod
    def _load_artifact(path: Path, model_type: type[BaseModel]) -> Any:
        try:
            return model_type.model_validate(load_json_object(path))
        except (ValueError, ValidationError) as error:
            raise ExtractionEvidenceError(
                f"Could not load required evidence {path.name}: artifact is corrupt"
            ) from error

    @staticmethod
    def _default_router() -> ModelRouter:
        from migrationswarm.config import get_settings
        from migrationswarm.core.models.registry import default_model_registry
        from migrationswarm.providers import default_provider_registry

        settings = get_settings()
        return ModelRouter(default_model_registry(settings), default_provider_registry(settings))


def service_slug(name: str) -> str:
    """Convert a selected candidate name to a deterministic safe service slug."""
    normalized = _SLUG_PART.sub("-", name.strip().casefold()).strip("-")
    if not normalized or normalized in {".", ".."}:
        raise ExtractionSafetyError(f"Could not create a safe service slug from: {name}")
    return normalized


def _select_candidate(report: ServiceBoundaryReport, requested: str) -> CandidateService | None:
    normalized = _select_candidate_name(requested)
    for candidate in report.candidate_services:
        if _select_candidate_name(candidate.name) == normalized:
            return candidate
    return None


def _select_candidate_name(name: str) -> str:
    return name.strip().casefold().removesuffix(" service").strip()


def _safe_relative_path(value: str) -> str:
    try:
        return normalize_relative_path(value)
    except PathSafetyError as error:
        raise ExtractionSafetyError(str(error)) from error


def _safe_workspace_path(relative: str, workspace: Path) -> Path:
    try:
        return safe_join(workspace, relative)
    except PathSafetyError as error:
        raise ExtractionEvidenceError(
            f"Evidence source path is outside worktree: {relative}"
        ) from error


def _is_protected_or_outside(path: Path, workspace: Path) -> bool:
    try:
        relative = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return True
    return any(part.lower() in {".git", ".migrationswarm"} for part in relative.parts)


def _normalize_generated_path(value: str, target_directory: str) -> str:
    normalized = _safe_relative_path(value)
    target_prefix = f"{target_directory}/"
    if normalized.startswith("services/") and not normalized.startswith(target_prefix):
        raise ExtractionSafetyError(f"Generated path targets a different service: {value}")
    if normalized == target_directory:
        raise ExtractionSafetyError("Generated path must name a file inside the service")
    if normalized.startswith(target_prefix):
        inner = normalized[len(target_prefix) :]
    else:
        inner = normalized
    if not inner:
        raise ExtractionSafetyError(f"Generated path is empty: {value}")
    return f"{target_directory}/{inner}"


def _validate_java_package(
    relative_path: str,
    content: str,
) -> set[str]:
    if not relative_path.endswith(".java"):
        return set()
    marker = "/src/main/java/"
    test_marker = "/src/test/java/"
    source_root = (
        marker
        if marker in relative_path
        else test_marker
        if test_marker in relative_path
        else None
    )
    package_match = _JAVA_PACKAGE.search(content)
    if source_root is None or package_match is None:
        return set()
    package_path = relative_path.split(source_root, 1)[1].rsplit("/", 1)[0]
    expected = package_path.replace("/", ".")
    if package_match.group(1) != expected:
        raise ExtractionSafetyError(
            f"Java package does not match generated path: {relative_path}"
        )
    declared_types = set(_JAVA_TYPE.findall(content))
    if not declared_types:
        raise ExtractionSafetyError(f"Generated Java file declares no type: {relative_path}")
    return {f"{package_match.group(1)}.{declared_type}" for declared_type in declared_types}


_CONTEXT_REASON_ORDER: tuple[str, ...] = (
    "required_candidate_owned",
    "direct_compile_dependency",
    "required_shared_type",
    "candidate_configuration_or_resource",
    "candidate_test",
)


def _select_context_classes(
    candidate: CandidateService,
    graph: JavaDependencyGraph,
    boundary: ServiceBoundaryReport,
    classes_by_name: Mapping[str, JavaClass],
    limits: ExtractionLimits,
) -> tuple[list[JavaClass], ExtractionContextSelection]:
    """Select required and directly useful source files deterministically.

    Candidate classes are required and are never truncated. Optional context is
    restricted to one dependency edge from those classes and is ranked by
    evidence strength before the remaining file budget is applied.
    """
    required_names = set(candidate.classes)
    missing = sorted(required_names - set(classes_by_name))
    if missing:
        raise ExtractionEvidenceError(
            "Selected candidate classes are missing from dependency evidence: "
            + ", ".join(missing)
        )

    required_classes = sorted(
        (classes_by_name[name] for name in required_names),
        key=lambda item: item.fully_qualified_name,
    )
    selected_by_reason: dict[str, list[str]] = {
        reason: [] for reason in _CONTEXT_REASON_ORDER
    }
    selected_by_reason["required_candidate_owned"] = [
        item.file_path for item in required_classes
    ]
    direct_edges = [
        edge
        for edge in graph.dependencies
        if edge.source in required_names
        and edge.target not in required_names
        and edge.target in classes_by_name
    ]
    edge_counts = Counter(edge.target for edge in direct_edges)
    direct_targets = set(edge_counts)
    shared_names = {item.class_name for item in boundary.shared_components}
    optional_names = sorted(direct_targets)
    categorized: dict[str, list[JavaClass]] = {reason: [] for reason in _CONTEXT_REASON_ORDER[1:]}
    for name in optional_names:
        java_class = classes_by_name[name]
        normalized_path = java_class.file_path.replace("\\", "/")
        if name in shared_names:
            reason = "required_shared_type"
        elif java_class.role is JavaClassRole.CONFIGURATION:
            reason = "candidate_configuration_or_resource"
        elif "/src/test/" in normalized_path:
            reason = "candidate_test"
        else:
            reason = "direct_compile_dependency"
        categorized[reason].append(java_class)

    for classes in categorized.values():
        classes.sort(key=lambda item: _context_rank(item, edge_counts, required_classes))

    optional_classes = [
        item for reason in _CONTEXT_REASON_ORDER[1:] for item in categorized[reason]
    ]
    capacity = max(0, limits.max_context_files - len(required_classes))
    selected_optional = optional_classes[:capacity]
    for reason in _CONTEXT_REASON_ORDER[1:]:
        paths = [
            item.file_path
            for item in selected_optional
            if _context_reason(item, categorized) == reason
        ]
        selected_by_reason[reason] = sorted(paths)

    transitive_names = {
        edge.target
        for edge in graph.dependencies
        if edge.source in direct_targets and edge.target not in required_names
    }
    excluded_by_reason: dict[str, list[str]] = {}
    selected_optional_names = {item.fully_qualified_name for item in selected_optional}
    for item in optional_classes:
        if item.fully_qualified_name not in selected_optional_names:
            excluded_by_reason.setdefault("optional_context_limit", []).append(item.file_path)
    for item in sorted(classes_by_name.values(), key=lambda value: value.fully_qualified_name):
        if (
            item.fully_qualified_name in required_names
            or item.fully_qualified_name in direct_targets
        ):
            continue
        if item.fully_qualified_name in transitive_names:
            excluded_by_reason.setdefault("transitive_dependency", []).append(item.file_path)

    for values in selected_by_reason.values():
        values.sort()
    for values in excluded_by_reason.values():
        values.sort()
    selection = ExtractionContextSelection(
        required_file_count=len(required_classes),
        optional_file_count=len(optional_classes),
        selected_file_count=len(required_classes) + len(selected_optional),
        configured_limit=limits.max_context_files,
        selected_files_by_reason=selected_by_reason,
        excluded_files_by_reason=excluded_by_reason,
    )
    if selection.required_file_count > limits.max_context_files:
        raise ExtractionEvidenceError(_context_file_limit_message(candidate, selection))
    ordered_classes = required_classes + selected_optional
    return ordered_classes, selection


def _context_rank(
    java_class: JavaClass,
    edge_counts: Counter[str],
    required_classes: list[JavaClass],
) -> tuple[int, int, str]:
    """Rank optional files by edge frequency, package distance, then name."""
    candidate_packages = [item.package for item in required_classes]
    distance = min(
        _package_distance(package, java_class.package) for package in candidate_packages
    )
    return (
        -edge_counts[java_class.fully_qualified_name],
        distance,
        java_class.fully_qualified_name,
    )


def _package_distance(left: str, right: str) -> int:
    """Return a stable segment distance between two Java packages."""
    left_parts = left.split(".")
    right_parts = right.split(".")
    common = 0
    for left_part, right_part in zip(left_parts, right_parts, strict=False):
        if left_part != right_part:
            break
        common += 1
    return len(left_parts) + len(right_parts) - (2 * common)


def _context_reason(
    java_class: JavaClass,
    categorized: Mapping[str, list[JavaClass]],
) -> str:
    """Return the deterministic reason assigned to one optional class."""
    for reason, values in categorized.items():
        if any(item.fully_qualified_name == java_class.fully_qualified_name for item in values):
            return reason
    raise AssertionError(
        f"Uncategorized extraction context class: {java_class.fully_qualified_name}"
    )


def _exclude_selected_file(
    selection: ExtractionContextSelection,
    relative_path: str,
    reason: str,
) -> None:
    """Move an optional selected path into deterministic exclusion diagnostics."""
    for paths in selection.selected_files_by_reason.values():
        if relative_path in paths:
            paths.remove(relative_path)
            break
    selection.excluded_files_by_reason.setdefault(reason, []).append(relative_path)
    selection.excluded_files_by_reason[reason].sort()
    selection.selected_file_count -= 1


def _context_file_limit_message(
    candidate: CandidateService,
    selection: ExtractionContextSelection,
) -> str:
    """Format an actionable required-file overflow diagnostic."""
    details = json.dumps(selection.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return (
        "Selected source context exceeds file count limit: "
        f"candidate={candidate.name}; required files cannot be truncated; "
        f"required_file_count={selection.required_file_count}; "
        f"optional_file_count={selection.optional_file_count}; "
        f"selected_file_count={selection.selected_file_count}; "
        f"configured_limit={selection.configured_limit}; selection={details}"
    )


def _external_dependencies(
    graph: JavaDependencyGraph,
    selected_names: set[str],
    shared_names: set[str],
    classes_by_name: Mapping[str, JavaClass],
) -> list[ExtractionDependency]:
    dependencies: dict[tuple[str, str, ExtractionDependencyKind], ExtractionDependency] = {}
    for edge in graph.dependencies:
        if edge.source not in selected_names or edge.target in selected_names:
            continue
        target = classes_by_name.get(edge.target)
        if target is None:
            classification = ExtractionDependencyKind.UNRESOLVED_DEPENDENCY
            reason = "Dependency target is absent from local class evidence."
        elif edge.target in shared_names or target.role in {
            JavaClassRole.APPLICATION,
            JavaClassRole.COMPONENT,
            JavaClassRole.CONFIGURATION,
        }:
            classification = ExtractionDependencyKind.SHARED_CODE_CANDIDATE
            reason = "Known local class is outside the selected candidate and may be shared."
        else:
            classification = ExtractionDependencyKind.EXTERNAL_SERVICE_DEPENDENCY
            reason = "Known local class is outside the selected candidate boundary."
        item = ExtractionDependency(
            source_class=edge.source,
            target_class=edge.target,
            classification=classification,
            reason=reason,
        )
        dependencies[(item.source_class, item.target_class, item.classification)] = item
    return [dependencies[key] for key in sorted(dependencies, key=str)]


def _acceptance_criteria(plan: MigrationPlan, selected_names: set[str]) -> list[str]:
    criteria: list[str] = []
    for step in plan.steps:
        if not set(step.affected_classes).isdisjoint(selected_names) or step.task_type.value in {
            "code_refactor",
            "test",
            "verify",
        }:
            for criterion in step.acceptance_criteria:
                if criterion not in criteria:
                    criteria.append(criterion)
    return criteria or ["The generated service remains grounded in the selected candidate."]


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        return stripped[7:-3].strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped[3:-3].strip()
    return stripped


__all__ = [
    "EXTRACTION_RESULTS_DIR",
    "ExtractionDependency",
    "ExtractionDependencyKind",
    "ExtractionContextSelection",
    "ExtractionEvidenceError",
    "ExtractionLimits",
    "ExtractionResponseError",
    "ExtractionSafetyError",
    "ExtractionWarning",
    "ExtractionWorkspaceError",
    "GeneratedFile",
    "SelectedServiceNotFoundError",
    "ServiceExtractionAgent",
    "ServiceExtractionContext",
    "ServiceExtractionError",
    "ServiceExtractionProposal",
    "service_slug",
]
