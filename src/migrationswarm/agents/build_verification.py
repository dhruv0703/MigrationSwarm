"""Deterministic build and test verification inside task worktrees."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field

from migrationswarm.core.agents import AgentCapability, AgentContext, AgentResult, BaseAgent
from migrationswarm.core.git import GitRepository, GitRepositoryError, GitWorktreeManager
from migrationswarm.core.security import (
    MAX_LOG_BYTES,
    PathSafetyError,
    ensure_contained,
    ensure_json_artifact_healthy,
    redact_text,
    safe_join,
    write_json_atomic,
)
from migrationswarm.core.tasks import Task
from migrationswarm.core.workspaces import TaskWorkspace

logger = structlog.get_logger(__name__)

VERIFICATION_RESULTS_DIR: Final[str] = ".migrationswarm/verification-results"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 300.0
DEFAULT_MAX_SUMMARY_CHARS: Final[int] = 4_000

_MAVEN_TEST_SUMMARY = re.compile(
    r"Tests\s+run:\s*(?P<run>\d+)\s*,\s*"
    r"Failures:\s*(?P<failures>\d+)\s*,\s*"
    r"Errors:\s*(?P<errors>\d+)\s*,\s*"
    r"Skipped:\s*(?P<skipped>\d+)",
    re.IGNORECASE,
)
_GRADLE_TEST_SUMMARY = re.compile(
    r"(?P<run>\d+)\s+tests?\s+completed"
    r"(?:,\s*(?P<failures>\d+)\s+failed)?"
    r"(?:,\s*(?P<skipped>\d+)\s+skipped)?",
    re.IGNORECASE,
)


class BuildVerificationError(ValueError):
    """Base exception for deterministic build verification."""


class VerificationWorkspaceError(BuildVerificationError):
    """Raised when a task does not point to a managed isolated worktree."""


class UnsafeVerificationRootError(VerificationWorkspaceError):
    """Raised when a requested verification subproject is unsafe."""


class UnsupportedBuildProjectError(BuildVerificationError):
    """Raised when no supported Maven or Gradle marker is present."""


class BuildSystem(StrEnum):
    """Build systems supported by the verifier."""

    MAVEN = "maven"
    GRADLE = "gradle"


class VerificationStatus(StrEnum):
    """Outcome of one deterministic verification command."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class TestSummary(BaseModel):
    """Best-effort normalized test counts extracted from build output."""

    model_config = ConfigDict(extra="forbid")

    tests_run: int | None = Field(default=None, ge=0)
    failures: int | None = Field(default=None, ge=0)
    errors: int | None = Field(default=None, ge=0)
    skipped: int | None = Field(default=None, ge=0)


class BuildCommandResult(BaseModel):
    """Bounded evidence for one allowlisted command execution."""

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(min_length=1)
    status: VerificationStatus
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    stdout_summary: str = ""
    stderr_summary: str = ""
    timed_out: bool = False


class BuildVerificationResult(BaseModel):
    """Structured build and test evidence returned by the verifier."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    workspace_path: str
    build_system: BuildSystem
    commands_run: list[BuildCommandResult] = Field(min_length=1)
    status: VerificationStatus
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    stdout_summary: str = ""
    stderr_summary: str = ""
    test_summary: TestSummary = Field(default_factory=TestSummary)
    changed_files: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VerificationLimits(BaseModel):
    """Safe execution and output bounds for one verification."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)
    max_summary_chars: int = Field(default=DEFAULT_MAX_SUMMARY_CHARS, ge=100)
    max_log_bytes: int = Field(default=MAX_LOG_BYTES, ge=1_000)


@dataclass(frozen=True)
class _CommandExecution:
    """Raw command evidence retained only until artifacts are written."""

    result: BuildCommandResult
    stdout: str
    stderr: str
    warning: str | None = None


class BuildVerificationAgent(BaseAgent):
    """Run only the selected project's safe test command in a task worktree."""

    name = "build_verification"
    capabilities = frozenset({AgentCapability.VERIFICATION})

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
    ) -> None:
        self.limits = VerificationLimits(
            timeout_seconds=timeout_seconds,
            max_summary_chars=max_summary_chars,
        )

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        """Detect, execute, summarize, and archive one safe test verification."""
        started_at = datetime.now(UTC)
        workspace = self._require_workspace(task, context)
        verification_root = self._verification_root(task, context, workspace)
        build_system = self.detect_build_system(verification_root)
        command = self.command_for(build_system, verification_root)
        execution = self._run_command(command, verification_root)
        combined_output = f"{execution.stdout}\n{execution.stderr}"
        test_summary, parser_warning = parse_test_summary(build_system, combined_output)
        warnings = [warning for warning in (execution.warning, parser_warning) if warning]
        repository = GitRepository(workspace.workspace_path)
        changed_files = list(repository.changed_files())
        result = BuildVerificationResult(
            task_id=task.id,
            workspace_path=str(verification_root),
            build_system=build_system,
            commands_run=[execution.result],
            status=execution.result.status,
            exit_code=execution.result.exit_code,
            duration_ms=execution.result.duration_ms,
            stdout_summary=execution.result.stdout_summary,
            stderr_summary=execution.result.stderr_summary,
            test_summary=test_summary,
            changed_files=changed_files,
            warnings=warnings,
        )
        json_artifact, log_artifact = self._write_artifacts(
            workspace,
            task.task_type.value,
            task.id,
            result,
            execution.stdout,
            execution.stderr,
        )
        logger.info(
            "build_verification_completed",
            build_system=build_system.value,
            status=result.status.value,
            duration_ms=result.duration_ms,
        )
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=result.status is VerificationStatus.PASSED,
            summary=(
                f"{build_system.value} test verification {result.status.value} "
                f"in {result.duration_ms} ms."
            ),
            artifacts=[json_artifact, log_artifact],
            metadata={"verification_result": result.model_dump(mode="json")},
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    @staticmethod
    def detect_build_system(workspace_path: Path) -> BuildSystem:
        """Detect Maven before Gradle using only repository marker files."""
        if any(
            (workspace_path / marker).is_file()
            for marker in ("pom.xml", "mvnw", "mvnw.cmd")
        ):
            return BuildSystem.MAVEN
        if any(
            (workspace_path / marker).is_file()
            for marker in ("build.gradle", "build.gradle.kts", "gradlew", "gradlew.bat")
        ):
            return BuildSystem.GRADLE
        raise UnsupportedBuildProjectError(
            "No supported Maven or Gradle project marker found in the worktree"
        )

    @staticmethod
    def command_for(build_system: BuildSystem, workspace_path: Path) -> list[str]:
        """Return the one fixed test command allowed for a detected build system."""
        if build_system is BuildSystem.MAVEN:
            wrapper_cmd = workspace_path / "mvnw.cmd"
            wrapper_unix = workspace_path / "mvnw"
            if os.name == "nt" and wrapper_cmd.is_file():
                return ["mvnw.cmd", "test"]
            if wrapper_unix.is_file():
                return ["./mvnw", "test"] if os.name != "nt" else ["mvnw", "test"]
            if wrapper_cmd.is_file():
                return ["./mvnw.cmd", "test"]
            return ["mvn", "test"]

        wrapper_cmd = workspace_path / "gradlew.bat"
        wrapper_unix = workspace_path / "gradlew"
        if os.name == "nt" and wrapper_cmd.is_file():
            return ["gradlew.bat", "test"]
        if wrapper_unix.is_file():
            return ["./gradlew", "test"] if os.name != "nt" else ["gradlew", "test"]
        if wrapper_cmd.is_file():
            return ["./gradlew.bat", "test"]
        return ["gradle", "test"]

    def _require_workspace(self, task: Task, context: AgentContext) -> TaskWorkspace:
        if context.workspace_path is None:
            raise VerificationWorkspaceError(
                "BuildVerificationAgent requires an isolated workspace_path"
            )
        workspace_path = Path(context.workspace_path).expanduser().resolve()
        if not workspace_path.is_dir():
            raise VerificationWorkspaceError(f"Workspace is not a directory: {workspace_path}")
        worktree_task_id = context.metadata.get("worktree_task_id", task.id)
        if not isinstance(worktree_task_id, str):
            worktree_task_id = str(task.id)
        if workspace_path.name != worktree_task_id:
            raise VerificationWorkspaceError("Workspace directory must match the task ID")
        worktrees = workspace_path.parent
        metadata_dir = worktrees.parent
        if worktrees.name != "worktrees" or metadata_dir.name != ".migrationswarm":
            raise VerificationWorkspaceError(
                "Workspace must be inside .migrationswarm/worktrees/<task-id>"
            )
        repository_root = metadata_dir.parent
        try:
            repository = GitRepository.from_path(repository_root)
            managed_workspace = GitWorktreeManager(repository).task_workspace(
                UUID(worktree_task_id)
            )
            independent_root = GitRepository.from_path(workspace_path).root
        except GitRepositoryError as error:
            raise VerificationWorkspaceError(
                f"Could not validate task worktree: {workspace_path}"
            ) from error
        if managed_workspace.workspace_path != workspace_path:
            raise VerificationWorkspaceError("Workspace is not the managed task worktree")
        if independent_root != workspace_path:
            raise VerificationWorkspaceError("Workspace is not an independent Git worktree")
        return managed_workspace

    @staticmethod
    def _verification_root(
        task: Task,
        context: AgentContext,
        workspace: TaskWorkspace,
    ) -> Path:
        """Resolve an optional relative project directory inside the worktree."""
        del task
        requested = context.metadata.get("verification_root")
        if requested is None:
            return workspace.workspace_path
        if not isinstance(requested, str) or not requested.strip():
            raise UnsafeVerificationRootError(
                "verification_root must be a non-empty relative path"
            )
        try:
            raw_requested = requested.strip()
            resolved = (
                ensure_contained(workspace.workspace_path, raw_requested)
                if Path(raw_requested).expanduser().is_absolute()
                else safe_join(workspace.workspace_path, raw_requested)
            )
        except PathSafetyError as error:
            raise UnsafeVerificationRootError(
                "verification_root must be a safe relative directory inside the worktree"
            ) from error
        if not resolved.is_dir():
            raise UnsafeVerificationRootError(
                f"verification_root is not a directory: {requested}"
            )
        return resolved

    def _run_command(self, command: list[str], workspace_path: Path) -> _CommandExecution:
        """Execute one internal command with no shell and bounded timeout."""
        started = time.perf_counter()
        execution_commands = [command]
        fallback = self._execution_command(command)
        if fallback != command:
            execution_commands.append(fallback)
        completed: subprocess.CompletedProcess[str] | None = None
        for execution_command in execution_commands:
            try:
                completed = subprocess.run(
                    execution_command,
                    cwd=workspace_path,
                    capture_output=True,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    timeout=self.limits.timeout_seconds,
                )
                break
            except subprocess.TimeoutExpired as error:
                duration_ms = _elapsed_ms(started)
                stdout = _decode_output(error.output)
                stderr = _decode_output(error.stderr)
                stdout, stdout_warning = _bound_output(stdout, self.limits.max_log_bytes)
                stderr, stderr_warning = _bound_output(stderr, self.limits.max_log_bytes)
                warnings = [warning for warning in (stdout_warning, stderr_warning) if warning]
                warnings.append(
                    f"Verification command timed out after {self.limits.timeout_seconds:g} seconds"
                )
                return _CommandExecution(
                    result=self._command_result(
                        command,
                        VerificationStatus.ERROR,
                        None,
                        duration_ms,
                        stdout,
                        stderr,
                        timed_out=True,
                    ),
                    stdout=stdout,
                    stderr=stderr,
                    warning=" ".join(warnings),
                )
            except (FileNotFoundError, OSError) as error:
                if execution_command != execution_commands[-1]:
                    continue
                duration_ms = _elapsed_ms(started)
                warning = f"Could not execute verification command: {error}"
                return _CommandExecution(
                    result=self._command_result(
                        command,
                        VerificationStatus.ERROR,
                        None,
                        duration_ms,
                        "",
                        "",
                    ),
                    stdout="",
                    stderr="",
                    warning=warning,
                )
        if completed is None:
            raise RuntimeError("Verification command did not produce a result")

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        stdout, stdout_warning = _bound_output(stdout, self.limits.max_log_bytes)
        stderr, stderr_warning = _bound_output(stderr, self.limits.max_log_bytes)
        output_warnings = [warning for warning in (stdout_warning, stderr_warning) if warning]
        status = (
            VerificationStatus.PASSED
            if completed.returncode == 0
            else VerificationStatus.FAILED
        )
        return _CommandExecution(
            result=self._command_result(
                command,
                status,
                completed.returncode,
                _elapsed_ms(started),
                stdout,
                stderr,
            ),
            stdout=stdout,
            stderr=stderr,
            warning=" ".join(output_warnings) if output_warnings else None,
        )

    @staticmethod
    def _execution_command(command: list[str]) -> list[str]:
        """Launch Windows wrapper scripts through cmd.exe without enabling shell mode."""
        if os.name == "nt":
            executable = command[0]
            if not executable.lower().endswith((".cmd", ".bat")):
                resolved = shutil.which(executable)
                if resolved is not None:
                    executable = resolved
            if executable.lower().endswith((".cmd", ".bat")):
                return [
                    os.environ.get("COMSPEC", "cmd.exe"),
                    "/d",
                    "/s",
                    "/c",
                    executable,
                    *command[1:],
                ]
        return command

    def _command_result(
        self,
        command: list[str],
        status: VerificationStatus,
        exit_code: int | None,
        duration_ms: int,
        stdout: str,
        stderr: str,
        *,
        timed_out: bool = False,
    ) -> BuildCommandResult:
        return BuildCommandResult(
            command=list(command),
            status=status,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_summary=_summarize(stdout, self.limits.max_summary_chars),
            stderr_summary=_summarize(stderr, self.limits.max_summary_chars),
            timed_out=timed_out,
        )

    @staticmethod
    def _write_artifacts(
        workspace: TaskWorkspace,
        task_type: str,
        task_id: UUID,
        result: BuildVerificationResult,
        stdout: str,
        stderr: str,
    ) -> tuple[str, str]:
        """Write normalized JSON and complete logs in the main repository metadata."""
        artifact_dir = workspace.repository_root / VERIFICATION_RESULTS_DIR
        artifact_dir.mkdir(parents=True, exist_ok=True)
        json_path = artifact_dir / f"{task_id}.json"
        log_path = artifact_dir / f"{task_id}.log"
        json_payload = result.model_dump(mode="json")
        json_payload["task_type"] = task_type
        json_payload["artifacts"] = {
            "json": str(json_path.relative_to(workspace.repository_root).as_posix()),
            "log": str(log_path.relative_to(workspace.repository_root).as_posix()),
        }
        ensure_json_artifact_healthy(json_path)
        write_json_atomic(json_path, json_payload)
        command = " ".join(result.commands_run[0].command)
        log_text = (
            f"COMMAND: {command}\n"
            f"STATUS: {result.status.value}\n"
            f"EXIT_CODE: {result.exit_code}\n\n"
            f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
        )
        log_text, _ = _bound_output(log_text, MAX_LOG_BYTES)
        _atomic_write_text(log_path, redact_text(log_text))
        return (
            str(json_path.relative_to(workspace.repository_root).as_posix()),
            str(log_path.relative_to(workspace.repository_root).as_posix()),
        )


def parse_test_summary(
    build_system: BuildSystem,
    output: str,
) -> tuple[TestSummary, str | None]:
    """Parse common Maven or Gradle summaries without failing verification."""
    if build_system is BuildSystem.MAVEN:
        matches = list(_MAVEN_TEST_SUMMARY.finditer(output))
        if not matches:
            return TestSummary(), "No recognizable Maven test summary found"
        return (
            TestSummary(
                tests_run=sum(int(match.group("run")) for match in matches),
                failures=sum(int(match.group("failures")) for match in matches),
                errors=sum(int(match.group("errors")) for match in matches),
                skipped=sum(int(match.group("skipped")) for match in matches),
            ),
            None,
        )

    matches = list(_GRADLE_TEST_SUMMARY.finditer(output))
    if not matches:
        return TestSummary(), "No recognizable Gradle test summary found"
    return (
        TestSummary(
            tests_run=sum(int(match.group("run")) for match in matches),
            failures=sum(int(match.group("failures") or 0) for match in matches),
            errors=0,
            skipped=sum(int(match.group("skipped") or 0) for match in matches),
        ),
        None,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _summarize(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    removed = len(value) - max_chars
    return f"{value[:max_chars]}\n...[truncated {removed} characters]"


def _bound_output(value: str, limit: int) -> tuple[str, str | None]:
    """Bound retained build logs and make truncation explicit."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, None
    bounded = encoded[:limit].decode("utf-8", errors="ignore")
    return bounded, f"Build output exceeded {limit} bytes and was truncated."


def _atomic_write_text(path: Path, content: str) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False
        ) as handle:
            handle.write(content)
            temporary = handle.name
        os.replace(temporary, path)
    finally:
        if temporary is not None and Path(temporary).exists():
            Path(temporary).unlink()


__all__ = [
    "BuildCommandResult",
    "BuildSystem",
    "BuildVerificationAgent",
    "BuildVerificationError",
    "BuildVerificationResult",
    "DEFAULT_MAX_SUMMARY_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "TestSummary",
    "UnsupportedBuildProjectError",
    "UnsafeVerificationRootError",
    "VerificationLimits",
    "VerificationStatus",
    "VerificationWorkspaceError",
    "VERIFICATION_RESULTS_DIR",
    "parse_test_summary",
]
