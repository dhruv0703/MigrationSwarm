"""Bounded model-assisted repair inside an isolated service worktree."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from migrationswarm.core.agents import AgentCapability, AgentContext, AgentResult, BaseAgent
from migrationswarm.core.git import GitRepository, GitWorktreeManager
from migrationswarm.core.models import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from migrationswarm.core.models.router import ModelRouter
from migrationswarm.core.security import (
    MAX_MODEL_RESPONSE_BYTES,
    PathSafetyError,
    ensure_json_artifact_healthy,
    normalize_relative_path,
    require_bytes,
    safe_join,
    write_json_atomic,
)
from migrationswarm.core.tasks import Task
from migrationswarm.core.workspaces import TaskWorkspace

logger = structlog.get_logger(__name__)

DEBUG_RESULTS_DIR = ".migrationswarm/debug-results"
SUPPORTED_DEBUG_EXTENSIONS = frozenset(
    {".java", ".xml", ".yml", ".yaml", ".properties", ".json", ".md", ".txt"}
)


class _ModelRouterLike(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...


class DebugError(ValueError):
    """Base exception for bounded debugging."""


class DebugWorkspaceError(DebugError):
    """Raised when a debug task does not point to a managed worktree."""


class DebugSafetyError(DebugError):
    """Raised when a repair proposal is outside the grounded service scope."""


class DebugResponseError(DebugError):
    """Raised when a model response is not a valid debug proposal."""


class DebugWriteError(DebugError):
    """Raised when an atomic repair cannot be applied safely."""


class DebugChangeType(StrEnum):
    MODIFY = "modify"
    CREATE = "create"


class DebugDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    likely_cause: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1, max_length=8)
    proposed_fix_summary: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class DebugFileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    change_type: DebugChangeType
    complete_new_content: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)


class DebugProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnosis: DebugDiagnosis
    changes: list[DebugFileChange] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


class DebugLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_changed_files: int = Field(default=5, ge=1, le=20)
    max_generated_bytes: int = Field(default=60_000, ge=1_000)
    max_context_bytes: int = Field(default=60_000, ge=1_000)
    max_evidence_chars: int = Field(default=4_000, ge=200)


class DebugAgent(BaseAgent):
    """Diagnose and apply a small complete-file repair in a service worktree."""

    name = "debug"
    capabilities = frozenset({AgentCapability.DEBUGGING})
    system_prompt = """You are MigrationSwarm's bounded debugging agent.
Use only the supplied failure evidence and extracted-service files. Propose the smallest
safe repair. Return complete UTF-8 file contents, only MODIFY or CREATE changes, and
never delete files, run commands, change unrelated monolith code, or add dependencies.
Return only strict JSON matching the requested schema."""

    def __init__(
        self,
        router: _ModelRouterLike | None = None,
        *,
        limits: DebugLimits | None = None,
    ) -> None:
        self.router = router or self._default_router()
        self.limits = limits or DebugLimits()

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        started_at = datetime.now(UTC)
        workspace = self._require_workspace(task, context)
        attempt = self._attempt(context)
        target_root = self._target_root(workspace.workspace_path, context)
        request_context = self._build_context(
            task, context, workspace.workspace_path, target_root, attempt
        )
        proposal, response = self._propose(request_context)
        self._validate_proposal(proposal, workspace.workspace_path, target_root)
        changed = [self._normalize(change.path) for change in proposal.changes]
        self._apply_changes(workspace.workspace_path, proposal)
        artifact = self._write_artifact(
            workspace,
            task,
            attempt,
            proposal,
            changed,
            request_context["failure_evidence"],
            response,
        )
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=f"Applied bounded debug repair affecting {len(changed)} files.",
            artifacts=[artifact],
            metadata={
                "attempt": attempt,
                "changed_files": changed,
                "diagnosis": proposal.diagnosis.model_dump(mode="json"),
                "model_provider": response.provider,
                "model_name": response.model,
            },
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    def _build_context(
        self,
        task: Task,
        context: AgentContext,
        workspace: Path,
        target_root: Path,
        attempt: int,
    ) -> dict[str, Any]:
        failure = context.metadata.get("failure_evidence", {})
        if not isinstance(failure, dict):
            raise DebugSafetyError("failure_evidence must be a mapping")
        bounded_failure = json.loads(json.dumps(failure, default=str))
        failure_text = json.dumps(bounded_failure, sort_keys=True)
        if len(failure_text) > self.limits.max_evidence_chars:
            raise DebugSafetyError("failure evidence exceeds the bounded context limit")
        values = context.metadata.get("relevant_files", context.metadata.get("changed_files", []))
        if not isinstance(values, list):
            raise DebugSafetyError("relevant_files must be a list")
        files: dict[str, str] = {}
        total = 0
        for value in values:
            if not isinstance(value, str):
                raise DebugSafetyError("relevant file paths must be strings")
            relative = self._normalize(value)
            path = self._resolve(workspace, relative)
            try:
                path.relative_to(target_root)
            except ValueError as error:
                raise DebugSafetyError(
                    f"Debug context escapes extracted service: {relative}"
                ) from error
            if path.is_file() and path.suffix.lower() in SUPPORTED_DEBUG_EXTENSIONS:
                content = path.read_text(encoding="utf-8")
                total += len(content.encode("utf-8"))
                if total > self.limits.max_context_bytes:
                    raise DebugSafetyError("Debug context exceeds the bounded context limit")
                files[relative] = content
        return {
            "task_id": str(task.id),
            "attempt": attempt,
            "failure_evidence": bounded_failure,
            "target_service_directory": target_root.relative_to(workspace).as_posix(),
            "files": files,
        }

    def _propose(self, request_context: dict[str, Any]) -> tuple[DebugProposal, ModelResponse]:
        request = self._request(request_context)
        try:
            response = self.router.generate(request)
            return self._parse(response.content), response
        except DebugResponseError as first_error:
            if len(response.content.encode("utf-8")) > MAX_MODEL_RESPONSE_BYTES:
                raise first_error
            try:
                response = self.router.generate(
                    request.model_copy(
                        update={
                            "messages": [
                                *request.messages,
                                ModelMessage(
                                    role=ModelRole.USER,
                                    content=(
                                        "Repair the response. Return only valid JSON with "
                                        "MODIFY or CREATE changes."
                                    ),
                                ),
                            ]
                        }
                    )
                )
                return self._parse(response.content), response
            except DebugResponseError:
                raise DebugResponseError(
                    "Debug response failed after one repair attempt"
                ) from first_error
            except Exception as error:
                raise DebugResponseError(
                    "Debug response failed after one repair attempt"
                ) from error
        except Exception:
            raise

    def _request(self, request_context: dict[str, Any]) -> ModelRequest:
        payload = json.dumps(request_context, sort_keys=True, separators=(",", ":"))
        return ModelRequest(
            messages=[
                ModelMessage(role=ModelRole.SYSTEM, content=self.system_prompt),
                ModelMessage(
                    role=ModelRole.USER,
                    content=f"Repair this bounded failure:\n{payload}",
                ),
            ],
            capability=ModelCapability.CODING,
            temperature=0.1,
            max_tokens=2600,
            response_format={"type": "json_object"},
            metadata={"agent": self.name},
        )

    @staticmethod
    def _parse(content: str) -> DebugProposal:
        try:
            value = content.strip()
            if value.startswith("```json") and value.endswith("```"):
                value = value[7:-3].strip()
            require_bytes(value, MAX_MODEL_RESPONSE_BYTES, label="debug response")
            return DebugProposal.model_validate(json.loads(value))
        except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as error:
            raise DebugResponseError("Model output was not valid debug JSON") from error

    def _validate_proposal(
        self, proposal: DebugProposal, workspace: Path, target_root: Path
    ) -> None:
        if len(proposal.changes) > self.limits.max_changed_files:
            raise DebugSafetyError("Debug proposal exceeds the changed-file limit")
        total = 0
        seen: set[str] = set()
        for change in proposal.changes:
            relative = self._normalize(change.path)
            if relative in seen:
                raise DebugSafetyError(f"Duplicate debug path: {relative}")
            seen.add(relative)
            target = self._resolve(workspace, relative)
            try:
                target.relative_to(target_root)
            except ValueError as error:
                raise DebugSafetyError(
                    f"Debug path is outside extracted service: {relative}"
                ) from error
            if target.suffix.lower() not in SUPPORTED_DEBUG_EXTENSIONS:
                raise DebugSafetyError(f"Unsupported debug file extension: {relative}")
            if not change.complete_new_content.strip():
                raise DebugSafetyError(f"Debug content is empty: {relative}")
            total += len(change.complete_new_content.encode("utf-8"))
            if total > self.limits.max_generated_bytes:
                raise DebugSafetyError("Debug proposal exceeds the generated-byte limit")
            if change.change_type is DebugChangeType.MODIFY and not target.is_file():
                raise DebugSafetyError(f"MODIFY target does not exist: {relative}")
            if change.change_type is DebugChangeType.CREATE and target.exists():
                raise DebugSafetyError(f"CREATE target already exists: {relative}")

    def _apply_changes(self, workspace: Path, proposal: DebugProposal) -> None:
        originals: dict[Path, bytes | None] = {}
        created_dirs: list[Path] = []
        try:
            for change in proposal.changes:
                target = self._resolve(workspace, self._normalize(change.path))
                originals[target] = target.read_bytes() if target.exists() else None
            for change in proposal.changes:
                target = self._resolve(workspace, self._normalize(change.path))
                if not target.parent.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    created_dirs.append(target.parent)
                temporary: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=target.parent, delete=False
                    ) as handle:
                        handle.write(change.complete_new_content)
                        temporary = handle.name
                    os.replace(temporary, target)
                finally:
                    if temporary and Path(temporary).exists():
                        Path(temporary).unlink()
        except OSError as error:
            for path, content in originals.items():
                if content is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_bytes(content)
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise DebugWriteError(f"Debug write failed and was rolled back: {error}") from error

    @staticmethod
    def _write_artifact(
        workspace: TaskWorkspace,
        task: Task,
        attempt: int,
        proposal: DebugProposal,
        changed: list[str],
        failure_evidence: Any,
        response: ModelResponse,
    ) -> str:
        artifact = (
            workspace.repository_root / DEBUG_RESULTS_DIR / str(task.id) / f"attempt-{attempt}.json"
        )
        artifact.parent.mkdir(parents=True, exist_ok=True)
        bounded_evidence = {
            key: (
                value[:800]
                if isinstance(value, str) and key in {"stdout_summary", "stderr_summary"}
                else value
            )
            for key, value in failure_evidence.items()
            if key
            in {
                "build_status",
                "build_exit_code",
                "tests_run",
                "test_failures",
                "test_errors",
                "warnings",
                "stdout_summary",
                "stderr_summary",
            }
        }
        payload = {
            "task_id": str(task.id),
            "attempt": attempt,
            "diagnosis": proposal.diagnosis.model_dump(mode="json"),
            "changed_files": changed,
            "failure_evidence": bounded_evidence,
            "model_provider": response.provider,
            "model_name": response.model,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        ensure_json_artifact_healthy(artifact)
        write_json_atomic(artifact, payload)
        return artifact.relative_to(workspace.repository_root).as_posix()

    @staticmethod
    def _attempt(context: AgentContext) -> int:
        value = context.metadata.get("debug_attempt")
        if not isinstance(value, int) or value < 1:
            raise DebugSafetyError("debug_attempt must be a positive integer")
        return value

    @staticmethod
    def _target_root(workspace: Path, context: AgentContext) -> Path:
        value = context.metadata.get("target_service_directory")
        if not isinstance(value, str) or not value.strip():
            raise DebugSafetyError("target_service_directory is required")
        relative = DebugAgent._normalize(value)
        target = DebugAgent._resolve(workspace, relative)
        if not target.is_dir():
            raise DebugSafetyError(f"Target service directory does not exist: {relative}")
        return target

    @staticmethod
    def _normalize(value: str) -> str:
        try:
            return normalize_relative_path(value)
        except PathSafetyError as error:
            raise DebugSafetyError(str(error)) from error

    @staticmethod
    def _resolve(workspace: Path, relative: str) -> Path:
        try:
            return safe_join(workspace, relative)
        except PathSafetyError as error:
            raise DebugSafetyError(f"Path escapes worktree: {relative}") from error

    @staticmethod
    def _require_workspace(task: Task, context: AgentContext) -> TaskWorkspace:
        if context.workspace_path is None:
            raise DebugWorkspaceError("DebugAgent requires an isolated workspace_path")
        path = Path(context.workspace_path).expanduser().resolve()
        worktree_task_id = context.metadata.get("worktree_task_id", task.id)
        if not isinstance(worktree_task_id, str):
            worktree_task_id = str(task.id)
        if not path.is_dir() or path.name != worktree_task_id:
            raise DebugWorkspaceError("Workspace must be the task worktree directory")
        if path.parent.name != "worktrees" or path.parent.parent.name != ".migrationswarm":
            raise DebugWorkspaceError(
                "Workspace must be inside .migrationswarm/worktrees/<task-id>"
            )
        root = path.parent.parent.parent
        try:
            managed = GitWorktreeManager(GitRepository.from_path(root))
            from uuid import UUID

            workspace = managed.task_workspace(UUID(worktree_task_id))
            if workspace.workspace_path != path or GitRepository.from_path(path).root != path:
                raise DebugWorkspaceError("Workspace is not the managed independent worktree")
            return workspace
        except DebugError:
            raise
        except Exception as error:
            raise DebugWorkspaceError("Could not validate task worktree") from error

    @staticmethod
    def _default_router() -> ModelRouter:
        from migrationswarm.config import get_settings
        from migrationswarm.core.models.registry import default_model_registry
        from migrationswarm.providers import default_provider_registry

        settings = get_settings()
        return ModelRouter(default_model_registry(settings), default_provider_registry(settings))


__all__ = [
    "DEBUG_RESULTS_DIR",
    "SUPPORTED_DEBUG_EXTENSIONS",
    "DebugAgent",
    "DebugChangeType",
    "DebugDiagnosis",
    "DebugError",
    "DebugFileChange",
    "DebugLimits",
    "DebugProposal",
    "DebugResponseError",
    "DebugSafetyError",
    "DebugWorkspaceError",
    "DebugWriteError",
]
