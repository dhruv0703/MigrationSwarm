"""Focused failure-injection and hostile-input coverage for security boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import redis
from typer.testing import CliRunner

from migrationswarm.agents.architecture_analysis import (
    ArchitectureAnalysisAgent,
    ArchitectureAnalysisError,
)
from migrationswarm.agents.repository_analysis import RepositoryAnalysisAgent
from migrationswarm.agents.service_boundary import (
    ServiceBoundaryAgent,
    ServiceBoundaryAnalysisError,
)
from migrationswarm.cli.main import app
from migrationswarm.core.models import ModelCapability, ModelMessage, ModelRequest, ModelRole
from migrationswarm.core.recovery import RecoveryInspector, RecoveryRecommendation
from migrationswarm.core.security import (
    ArtifactCorruptionError,
    PathSafetyError,
    load_json_object,
    normalize_relative_path,
    redact_secrets,
    redact_text,
    safe_join,
    write_json_atomic,
)
from migrationswarm.core.tasks import Task, TaskStatus, TaskType
from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.repositories import TaskRepository
from migrationswarm.persistence.exceptions import RedisCoordinationError
from migrationswarm.persistence.mapping import secret_free
from migrationswarm.persistence.redis import AgentHeartbeat, ReadyTaskQueue, TaskLock
from migrationswarm.persistence.service import MigrationStateService
from migrationswarm.providers.groq import GroqProvider

RUNNER = CliRunner()
PROJECT_ID = UUID(int=7001)


def task(status: TaskStatus = TaskStatus.RUNNING) -> Task:
    """Create a small task for pure recovery tests."""
    return Task(
        project_id=PROJECT_ID,
        task_type=TaskType.TEST,
        title="Security test",
        description="Inspect failure behavior.",
        status=status,
    )


@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",
        r"\\server\share\secret.txt",
        r"C:\Windows\system.ini",
        "C:/Windows/system.ini",
        "..\\outside.txt",
        "../outside.txt",
        "safe/../../outside.txt",
        ".git/config",
        ".migrationswarm/results.json",
        "safe\x00name.txt",
        "",
    ],
)
def test_normalize_rejects_hostile_paths(value: str) -> None:
    with pytest.raises(PathSafetyError):
        normalize_relative_path(value)


@pytest.mark.parametrize("value", ["src/main/App.java", "src\\main\\App.java", "README.md"])
def test_normalize_accepts_safe_paths(value: str) -> None:
    assert normalize_relative_path(value).startswith(("src/", "README"))


def test_safe_join_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    with pytest.raises(PathSafetyError):
        safe_join(root, "link/secret.txt")


@pytest.mark.parametrize(
    "value",
    [
        "GROQ_API_KEY=super-secret",
        "api_key: super-secret",
        "Authorization: Bearer abc.def",
        "password=hidden",
        "token: xyz",
        "https://user:password@example.invalid/path",
        "secret=one; token=two",
        "Bearer abc.def.ghi",
    ],
)
def test_redact_text_removes_credential_shapes(value: str) -> None:
    result = redact_text(value, ["super-secret", "hidden", "xyz"])
    assert all(secret not in result for secret in ("super-secret", "hidden", "xyz"))
    assert "[redacted]" in result


def test_redact_structured_metadata_and_persistence() -> None:
    value = {"api_key": "top-secret", "nested": {"password": "pw"}, "text": "token=pw"}
    redacted = redact_secrets(value)
    persisted = secret_free(value)
    assert "top-secret" not in json.dumps(redacted)
    assert "pw" not in json.dumps(persisted)


def test_atomic_json_write_redacts_and_loads(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    write_json_atomic(path, {"api_key": "top-secret", "status": "ok"})
    assert "top-secret" not in path.read_text(encoding="utf-8")
    assert load_json_object(path)["status"] == "ok"


@pytest.mark.parametrize(
    "raw",
    ["not-json", "[]", "null", "{\"unterminated\":", "\xff"],
)
def test_corrupt_artifact_fails_closed(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(raw.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(ArtifactCorruptionError):
        load_json_object(path)


def test_oversized_artifact_fails_before_parse(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    path.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(ArtifactCorruptionError):
        load_json_object(path, max_bytes=10)


def test_corrupt_architecture_artifact_is_not_regenerated(tmp_path: Path) -> None:
    artifact = tmp_path / ".migrationswarm" / "java-dependency-graph.json"
    artifact.parent.mkdir()
    artifact.write_text("not-json", encoding="utf-8")
    with pytest.raises(ArchitectureAnalysisError):
        ArchitectureAnalysisAgent().analyze(tmp_path)
    assert artifact.read_text(encoding="utf-8") == "not-json"


def test_corrupt_boundary_artifact_is_not_accepted(tmp_path: Path) -> None:
    artifact = tmp_path / ".migrationswarm" / "architecture-report.json"
    artifact.parent.mkdir()
    artifact.write_text("not-json", encoding="utf-8")
    with pytest.raises(ServiceBoundaryAnalysisError):
        ServiceBoundaryAgent().prepare_evidence(tmp_path)
    assert artifact.read_text(encoding="utf-8") == "not-json"


def test_malicious_repository_fixture_is_scanned_as_data() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "malicious repository"
    inventory = RepositoryAnalysisAgent().analyze(fixture)
    assert inventory.total_file_count == 2


def test_recovery_marks_orphaned_running_task_for_human_review() -> None:
    current = task()
    findings = RecoveryInspector().inspect([current], preserved_worktree_ids=[current.id])
    assert findings[0].recommendation is RecoveryRecommendation.HUMAN_REVIEW
    assert findings[0].worktree_preserved


def test_recovery_marks_live_task_active() -> None:
    current = task()
    findings = RecoveryInspector().inspect([current], active_task_ids=[current.id])
    assert findings[0].recommendation is RecoveryRecommendation.ACTIVE


def test_recovery_keeps_verification_manual() -> None:
    current = task(TaskStatus.VERIFYING)
    findings = RecoveryInspector().inspect([current])
    assert findings[0].recommendation is RecoveryRecommendation.HUMAN_REVIEW


class FailingRedis:
    """Failure-injection fake for transient coordination wrappers."""

    def __getattr__(self, name: str) -> object:
        def fail(*args: object, **kwargs: object) -> object:
            raise redis.RedisError(f"injected {name} failure")

        return fail


@pytest.mark.parametrize("operation", ["queue", "lock", "heartbeat"])
def test_redis_failures_are_domain_errors(operation: str) -> None:
    client = FailingRedis()
    task_id = uuid4()
    with pytest.raises(RedisCoordinationError, match="Could not"):
        if operation == "queue":
            ReadyTaskQueue(client).enqueue(task_id)
        elif operation == "lock":
            TaskLock(client).acquire(task_id)
        else:
            AgentHeartbeat(client).set_heartbeat("worker", ttl_seconds=5)


def test_persistence_failure_is_propagated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'failure.db'}")
    repository = TaskRepository(database.session_factory)
    monkeypatch.setattr(repository, "get", lambda task_id: None)

    def fail_create(value: Task) -> Task:
        del value
        raise RuntimeError("injected database failure")

    monkeypatch.setattr(repository, "create", fail_create)
    service = MigrationStateService(
        cast(Any, object()),
        repository,
        cast(Any, object()),
        cast(Any, object()),
    )
    with pytest.raises(RuntimeError, match="injected database failure"):
        service.persist_task(task(TaskStatus.PENDING))
    database.dispose()


def test_provider_rejects_oversized_response_without_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"x" * 1_000_001)

    provider = GroqProvider(
        api_key="secret-key",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://test/"),
    )
    with pytest.raises(Exception, match="size limit"):
        provider.generate(
            request=ModelRequest(
                messages=[ModelMessage(role=ModelRole.USER, content="ping")],
                capability=ModelCapability.VERIFICATION,
                max_tokens=1,
            ),
            model="openai/gpt-oss-20b",
        )


def test_security_check_is_secret_free() -> None:
    result = RUNNER.invoke(app, ["security-check"])
    assert result.exit_code == 0
    assert "status=ok" in result.stdout
    assert "top-secret" not in result.stdout
