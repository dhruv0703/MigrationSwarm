"""A bounded, worktree-only Java refactoring agent."""

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from migrationswarm.core.agents import AgentCapability, AgentContext, AgentResult, BaseAgent
from migrationswarm.core.git import GitRepository, GitWorktreeManager
from migrationswarm.core.models import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelRouter,
)
from migrationswarm.core.security import (
    MAX_ARTIFACT_BYTES,
    MAX_MODEL_RESPONSE_BYTES,
    PathSafetyError,
    ensure_json_artifact_healthy,
    normalize_relative_path,
    require_bytes,
    safe_join,
)
from migrationswarm.core.tasks import Task
from migrationswarm.core.workspaces import TaskWorkspace

logger = structlog.get_logger(__name__)

REFACTOR_RESULTS_DIR: Final[str] = ".migrationswarm/refactor-results"
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".java", ".xml", ".properties", ".yml", ".yaml", ".json", ".md"}
)
_CLASS_TOKEN = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*(?:Controller|Service|Repository|Facade|Config|Test)?\b"
)
_JAVA_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_JAVA_IMPORT = re.compile(r"^\s*import\s+([\w.]+)\s*;", re.MULTILINE)


class RefactorError(ValueError):
    """Base exception for safe refactor preparation and execution."""


class RefactorWorkspaceError(RefactorError):
    """Raised when a task does not point to its isolated worktree."""


class RefactorContextError(RefactorError):
    """Raised when bounded source context cannot be constructed."""


class RefactorResponseError(RefactorError):
    """Raised when model output is not valid structured refactor data."""


class RefactorGroundingError(RefactorError):
    """Raised when a proposal violates file or task scope."""


class RefactorWriteError(RefactorError):
    """Raised when a validated file change cannot be applied safely."""


class DirtyWorkspaceError(RefactorWorkspaceError):
    """Raised when a refactor starts with a dirty task worktree."""


class ChangeType(StrEnum):
    """Supported full-file change operations."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class RefactorLimits(BaseModel):
    """Explicit context and write limits for one refactor execution."""

    model_config = ConfigDict(extra="forbid")

    max_files: int = Field(default=8, ge=1)
    max_bytes_per_file: int = Field(default=12_000, ge=1)
    max_total_source_context: int = Field(default=40_000, ge=1)
    max_changed_files: int = Field(default=4, ge=1)


class SourceFileContext(BaseModel):
    """One bounded source file supplied to the model."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    content: str = Field(min_length=1)
    package: str | None = None
    imports: list[str] = Field(default_factory=list)


class RefactorContext(BaseModel):
    """Minimal structured context for a source refactor proposal."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1)
    affected_files: list[str] = Field(min_length=1)
    expected_outputs: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    files: list[SourceFileContext] = Field(min_length=1)
    allowed_paths: list[str] = Field(default_factory=list)
    allowed_directories: list[str] = Field(default_factory=list)

    @field_validator(
        "affected_files",
        "expected_outputs",
        "acceptance_criteria",
        "allowed_paths",
        "allowed_directories",
    )
    @classmethod
    def validate_text_items(cls, values: list[str]) -> list[str]:
        """Reject blank context entries."""
        if any(not value.strip() for value in values):
            raise ValueError("context entries must be non-empty")
        return values


class FileChange(BaseModel):
    """One complete-file proposal from the model."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    change_type: ChangeType
    complete_new_content: str
    reasoning: str = Field(min_length=1)


class RefactorProposal(BaseModel):
    """Strict model response containing complete file contents."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    changes: list[FileChange] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


class _ModelRouterLike(Protocol):
    """Minimal model-router contract used by the agent and test fakes."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a provider-neutral response."""


class RefactorContextBuilder:
    """Build bounded context from task scope and files in one task worktree."""

    def __init__(self, limits: RefactorLimits | None = None) -> None:
        self.limits = limits or RefactorLimits()

    def build(
        self,
        task: Task,
        workspace: TaskWorkspace,
        metadata: Mapping[str, Any] | None = None,
    ) -> RefactorContext:
        """Build context without reading outside the isolated worktree."""
        values = metadata or {}
        objective = self._text(values.get("instruction"), task.description)
        migration_step = values.get("migration_step")
        step_values = migration_step if isinstance(migration_step, Mapping) else {}
        expected_outputs = self._list(
            values.get("expected_outputs", step_values.get("expected_outputs")),
            ["Requested source refactor output"],
        )
        acceptance_criteria = self._list(
            values.get("acceptance_criteria", step_values.get("acceptance_criteria")),
            ["The implementation matches the task objective."],
        )
        explicit_files = self._list(values.get("affected_files"), [])
        if not explicit_files:
            explicit_files = self._infer_files(workspace.workspace_path, objective)
        if not explicit_files:
            raise RefactorContextError(
                "No affected files supplied or inferable from the task objective"
            )

        affected_paths = [
            self._resolve_relative(path, workspace.workspace_path)
            for path in explicit_files
        ]
        existing_paths = [path for path in affected_paths if path.is_file()]
        package_paths = self._nearby_package_files(affected_paths)
        files = sorted(set(existing_paths) | set(package_paths), key=lambda path: str(path))
        if len(files) > self.limits.max_files:
            raise RefactorContextError(
                f"Refactor context needs {len(files)} files, limit is {self.limits.max_files}"
            )
        if not files:
            raise RefactorContextError("No readable affected source files were found")

        source_context: list[SourceFileContext] = []
        total_bytes = 0
        for path in files:
            relative = path.relative_to(workspace.workspace_path).as_posix()
            content = path.read_text(encoding="utf-8")
            byte_count = len(content.encode("utf-8"))
            if byte_count > self.limits.max_bytes_per_file:
                raise RefactorContextError(
                    f"Source context file exceeds limit: {relative} ({byte_count} bytes)"
                )
            total_bytes += byte_count
            if total_bytes > self.limits.max_total_source_context:
                raise RefactorContextError(
                    f"Refactor source context exceeds limit: {total_bytes} bytes"
                )
            source_context.append(
                SourceFileContext(
                    path=relative,
                    content=content,
                    package=self._package(content),
                    imports=_JAVA_IMPORT.findall(content),
                )
            )

        allowed_paths = self._list(values.get("allowed_paths"), [])
        allowed_directories = self._list(values.get("allowed_directories"), [])
        normalized_affected = [
            path.relative_to(workspace.workspace_path).as_posix() for path in affected_paths
        ]
        if not allowed_paths:
            allowed_paths = normalized_affected
        if not allowed_directories:
            allowed_directories = sorted(
                {str(Path(path).parent).replace("\\", "/") for path in normalized_affected}
            )
        return RefactorContext(
            objective=objective,
            affected_files=normalized_affected,
            expected_outputs=expected_outputs,
            acceptance_criteria=acceptance_criteria,
            files=source_context,
            allowed_paths=allowed_paths,
            allowed_directories=allowed_directories,
        )

    @staticmethod
    def _text(value: Any, fallback: str) -> str:
        return value.strip() if isinstance(value, str) and value.strip() else fallback.strip()

    @staticmethod
    def _list(value: Any, fallback: list[str]) -> list[str]:
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, list) and all(
            isinstance(item, str) and item.strip() for item in value
        ):
            return [item.strip() for item in value]
        return list(fallback)

    @staticmethod
    def _package(content: str) -> str | None:
        match = _JAVA_PACKAGE.search(content)
        return match.group(1) if match else None

    def _nearby_package_files(self, existing_paths: Iterable[Path]) -> list[Path]:
        nearby: set[Path] = set()
        for path in existing_paths:
            if not path.parent.is_dir():
                continue
            for sibling in path.parent.iterdir():
                if sibling.is_file() and sibling.suffix.lower() in SUPPORTED_EXTENSIONS:
                    nearby.add(sibling)
        return sorted(nearby, key=lambda path: str(path))

    def _infer_files(self, workspace: Path, objective: str) -> list[str]:
        tokens = set(_CLASS_TOKEN.findall(objective))
        candidates = [
            path
            for path in workspace.rglob("*.java")
            if path.is_file() and path.stem in tokens
        ]
        return [path.relative_to(workspace).as_posix() for path in sorted(candidates)]

    @staticmethod
    def _resolve_relative(value: str, workspace: Path) -> Path:
        try:
            return safe_join(workspace, value)
        except PathSafetyError as error:
            raise RefactorContextError(f"Context path is unsafe: {value}") from error


class RefactorAgent(BaseAgent):
    """Apply small, grounded complete-file changes only inside a task worktree."""

    name = "refactor"
    capabilities = frozenset({AgentCapability.CODE_REFACTOR})

    system_prompt = """You are MigrationSwarm's bounded code refactoring agent.
Propose only a narrow, mechanical Java/source change for the supplied task objective.
Use only the supplied context. Do not invent unrelated files, dependencies, databases,
build changes, shell commands, or broad architecture changes. Return complete file
contents, never patch fragments. Use CREATE for a new file and MODIFY for an existing
file. DELETE is not allowed. Keep changes within the supplied task scope and supported
source formats. Do not run tests or build tools.

Return only strict JSON:
{
  "summary": "short summary",
  "changes": [{
    "path": "relative/path.ext",
    "change_type": "create|modify|delete",
    "complete_new_content": "complete UTF-8 file content",
    "reasoning": "why this narrow change is needed"
  }],
  "warnings": ["optional warning"]
}"""

    def __init__(
        self,
        router: _ModelRouterLike | None = None,
        *,
        limits: RefactorLimits | None = None,
    ) -> None:
        self.router = router or self._default_router()
        self.limits = limits or RefactorLimits()
        self.context_builder = RefactorContextBuilder(self.limits)

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        """Validate, propose, apply, and report a safe refactor."""
        return self._run(task, context, apply_changes=True)

    def dry_run(self, task: Task, context: AgentContext) -> AgentResult:
        """Validate and propose a change without writing source or artifacts."""
        return self._run(task, context, apply_changes=False)

    def build_context(self, task: Task, context: AgentContext) -> RefactorContext:
        """Validate the isolated workspace and build bounded model context."""
        workspace = self._require_workspace(task, context)
        return self.context_builder.build(task, workspace, context.metadata)

    def _run(
        self, task: Task, context: AgentContext, *, apply_changes: bool
    ) -> AgentResult:
        started_at = datetime.now(UTC)
        workspace = self._require_workspace(task, context)
        repository = GitRepository(workspace.workspace_path)
        if repository.is_dirty():
            raise DirtyWorkspaceError(
                "Refactor worktree must be clean before execution"
            )
        refactor_context = self.context_builder.build(task, workspace, context.metadata)
        proposal, response = self._propose(refactor_context)
        validated = self._validate_proposal(proposal, refactor_context, workspace.workspace_path)
        intended_paths = [change.path for change in validated.changes]
        if not apply_changes:
            return AgentResult(
                task_id=task.id,
                agent_name=self.name,
                success=True,
                summary=f"Validated dry-run refactor affecting {len(intended_paths)} files.",
                metadata={
                    "dry_run": True,
                    "intended_files": intended_paths,
                    "proposal": self._public_proposal(validated),
                    "model_provider": response.provider,
                    "model_name": response.model,
                },
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )

        try:
            self._apply_changes(workspace.workspace_path, validated)
            changed_files = list(repository.changed_files())
            diff = repository.diff()
            require_bytes(diff, MAX_ARTIFACT_BYTES, label="refactor diff")
            artifact = self._write_result_artifact(
                workspace,
                task,
                validated,
                changed_files,
                diff,
                response,
            )
        except RefactorError:
            raise
        except OSError as error:
            raise RefactorWriteError(f"Could not apply refactor safely: {error}") from error

        logger.info(
            "refactor_completed",
            provider=response.provider,
            model=response.model,
            changed_file_count=len(changed_files),
        )
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=(
                f"Applied refactor affecting {len(changed_files)} files; task remains "
                "for verification."
            ),
            artifacts=[artifact],
            metadata={
                "changed_files": changed_files,
                "diff": diff,
                "proposal": self._public_proposal(validated),
                "workspace_path": str(workspace.workspace_path),
                "model_provider": response.provider,
                "model_name": response.model,
            },
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    def _require_workspace(self, task: Task, context: AgentContext) -> TaskWorkspace:
        if context.workspace_path is None:
            raise RefactorWorkspaceError("RefactorAgent requires an isolated workspace_path")
        workspace_path = Path(context.workspace_path).expanduser().resolve()
        if not workspace_path.is_dir():
            raise RefactorWorkspaceError(f"Workspace is not a directory: {workspace_path}")
        if workspace_path.name != str(task.id):
            raise RefactorWorkspaceError("Workspace directory must match the task ID")
        worktrees = workspace_path.parent
        metadata_dir = worktrees.parent
        if worktrees.name != "worktrees" or metadata_dir.name != ".migrationswarm":
            raise RefactorWorkspaceError(
                "Workspace must be inside .migrationswarm/worktrees/<task-id>"
            )
        repository_root = metadata_dir.parent
        try:
            repository = GitRepository.from_path(repository_root)
            manager = GitWorktreeManager(repository)
            workspace = manager.task_workspace(task.id)
        except Exception as error:
            if isinstance(error, RefactorError):
                raise
            raise RefactorWorkspaceError(
                f"Could not validate task worktree: {workspace_path}"
            ) from error
        if workspace.workspace_path != workspace_path:
            raise RefactorWorkspaceError("Workspace does not match the managed task worktree")
        if GitRepository.from_path(workspace_path).root != workspace_path:
            raise RefactorWorkspaceError("Workspace is not an independent Git worktree")
        return workspace

    def _propose(self, refactor_context: RefactorContext) -> tuple[RefactorProposal, ModelResponse]:
        request = self._request(refactor_context)
        try:
            response = self.router.generate(request)
            proposal = self._parse(response.content)
        except RefactorResponseError as first_error:
            if len(response.content.encode("utf-8")) > MAX_MODEL_RESPONSE_BYTES:
                raise first_error
            repair_request = self._repair_request(request, response.content)
            try:
                response = self.router.generate(repair_request)
                proposal = self._parse(response.content)
            except RefactorResponseError as second_error:
                raise RefactorResponseError(
                    "Model refactor response failed after one repair attempt: "
                    f"{second_error}"
                ) from first_error
        return proposal, response

    def _request(self, refactor_context: RefactorContext) -> ModelRequest:
        context_json = json.dumps(
            refactor_context.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return ModelRequest(
            messages=[
                ModelMessage(role=ModelRole.SYSTEM, content=self.system_prompt),
                ModelMessage(
                    role=ModelRole.USER,
                    content=(
                        "Create the narrow refactor proposal from this context:\n"
                        f"{context_json}"
                    ),
                ),
            ],
            capability=ModelCapability.CODING,
            temperature=0.1,
            max_tokens=2600,
            response_format={"type": "json_object"},
            metadata={"agent": self.name},
        )

    @staticmethod
    def _repair_request(request: ModelRequest, invalid_output: str) -> ModelRequest:
        return request.model_copy(
            update={
                "messages": [
                    *request.messages,
                    ModelMessage(
                        role=ModelRole.USER,
                        content=(
                            "Repair the invalid response. Return only strict JSON with "
                            "complete file contents, no patches, no deletes, and no "
                            f"out-of-scope paths.\n{invalid_output}"
                        ),
                    ),
                ]
            }
        )

    def _parse(self, content: str) -> RefactorProposal:
        try:
            require_bytes(content, MAX_MODEL_RESPONSE_BYTES, label="refactor response")
            return RefactorProposal.model_validate(json.loads(_strip_json_fence(content)))
        except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as error:
            raise RefactorResponseError("Model output was not valid refactor JSON") from error

    def _validate_proposal(
        self,
        proposal: RefactorProposal,
        refactor_context: RefactorContext,
        workspace_path: Path,
    ) -> RefactorProposal:
        if len(proposal.changes) > self.limits.max_changed_files:
            raise RefactorGroundingError(
                f"Proposal changes {len(proposal.changes)} files, limit is "
                f"{self.limits.max_changed_files}"
            )
        seen: set[str] = set()
        for change in proposal.changes:
            relative = _safe_relative_path(change.path)
            if relative in seen:
                raise RefactorGroundingError(f"Duplicate proposed path: {relative}")
            seen.add(relative)
            if not _is_in_scope(relative, refactor_context):
                raise RefactorGroundingError(f"Proposed path is outside task scope: {relative}")
            try:
                target = safe_join(workspace_path, relative)
            except PathSafetyError as error:
                raise RefactorGroundingError(f"Unsafe proposed path: {relative}") from error
            if _is_protected_path(target, workspace_path):
                raise RefactorGroundingError(f"Protected path cannot be changed: {relative}")
            if target.suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise RefactorGroundingError(f"Unsupported file extension: {relative}")
            if change.change_type is ChangeType.DELETE:
                raise RefactorGroundingError("DELETE changes are disabled for RefactorAgent")
            if not change.complete_new_content.strip():
                raise RefactorGroundingError(f"Generated content is empty: {relative}")
            if len(change.complete_new_content.encode("utf-8")) > self.limits.max_bytes_per_file:
                raise RefactorGroundingError(f"Generated content exceeds limit: {relative}")
            if change.change_type is ChangeType.MODIFY and not target.is_file():
                raise RefactorGroundingError(f"MODIFY target does not exist: {relative}")
            if change.change_type is ChangeType.CREATE and target.exists():
                raise RefactorGroundingError(f"CREATE target already exists: {relative}")
        return proposal

    def _apply_changes(self, workspace_path: Path, proposal: RefactorProposal) -> None:
        originals: dict[Path, bytes | None] = {}
        created_dirs: list[Path] = []
        try:
            for change in proposal.changes:
                target = safe_join(workspace_path, change.path)
                originals[target] = target.read_bytes() if target.exists() else None
            for change in proposal.changes:
                target = safe_join(workspace_path, change.path)
                if not target.parent.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    created_dirs.append(target.parent)
                self._atomic_write(target, change.complete_new_content)
        except OSError as error:
            self._rollback(originals, created_dirs)
            raise RefactorWriteError(
                f"Refactor write failed and was rolled back: {error}"
            ) from error

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
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
    def _rollback(originals: Mapping[Path, bytes | None], created_dirs: list[Path]) -> None:
        for path, content in originals.items():
            try:
                if content is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_bytes(content)
            except OSError:
                logger.exception("refactor_rollback_failed", path=str(path))
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def _public_proposal(proposal: RefactorProposal) -> dict[str, Any]:
        """Return proposal metadata without persisting complete source contents."""
        payload = proposal.model_dump(mode="json")
        for change in payload["changes"]:
            content = change.pop("complete_new_content", "")
            change["content_bytes"] = len(str(content).encode("utf-8"))
        return payload

    @staticmethod
    def _write_result_artifact(
        workspace: TaskWorkspace,
        task: Task,
        proposal: RefactorProposal,
        changed_files: list[str],
        diff: str,
        response: ModelResponse,
    ) -> str:
        artifact = workspace.repository_root / REFACTOR_RESULTS_DIR / f"{task.id}.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": str(task.id),
            "workspace_path": str(workspace.workspace_path),
            "branch_name": workspace.branch_name,
            "base_commit": workspace.base_commit,
            "changed_files": changed_files,
            "diff": diff,
            "proposal": RefactorAgent._public_proposal(proposal),
            "model_provider": response.provider,
            "model_name": response.model,
        }
        ensure_json_artifact_healthy(artifact)
        RefactorAgent._atomic_write(
            artifact,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )
        return str(artifact.relative_to(workspace.repository_root).as_posix())

    @staticmethod
    def _default_router() -> ModelRouter:
        from migrationswarm.config import get_settings
        from migrationswarm.core.models.registry import default_model_registry
        from migrationswarm.providers import default_provider_registry

        settings = get_settings()
        return ModelRouter(
            default_model_registry(settings), default_provider_registry(settings)
        )


def _safe_relative_path(value: str) -> str:
    """Normalize a response path while rejecting absolute and traversal paths."""
    try:
        return normalize_relative_path(value)
    except PathSafetyError as error:
        raise RefactorGroundingError(str(error)) from error


def _is_protected_path(path: Path, workspace: Path) -> bool:
    """Return whether a resolved path targets protected metadata."""
    try:
        relative = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return True
    return any(part.lower() in {".git", ".migrationswarm"} for part in relative.parts)


def _is_in_scope(path: str, context: RefactorContext) -> bool:
    """Check exact allowed paths and explicitly allowed directory prefixes."""
    if path in context.allowed_paths:
        return True
    return any(
        path == directory or path.startswith(f"{directory.rstrip('/')}/")
        for directory in context.allowed_directories
    )


def _strip_json_fence(content: str) -> str:
    """Accept a common fenced JSON response."""
    stripped = content.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        return stripped[7:-3].strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped[3:-3].strip()
    return stripped


__all__ = [
    "ChangeType",
    "DirtyWorkspaceError",
    "FileChange",
    "RefactorAgent",
    "RefactorContext",
    "RefactorContextBuilder",
    "RefactorContextError",
    "RefactorError",
    "RefactorGroundingError",
    "RefactorLimits",
    "RefactorProposal",
    "RefactorResponseError",
    "RefactorWorkspaceError",
    "RefactorWriteError",
    "REFACTOR_RESULTS_DIR",
    "SUPPORTED_EXTENSIONS",
]
