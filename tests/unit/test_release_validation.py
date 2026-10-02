"""Release-candidate consistency, restart, and interruption checks."""

import json
from pathlib import Path
from uuid import uuid4

import fakeredis
import pytest
from scripts.validate_release_candidate import _release_environment
from typer.testing import CliRunner

from migrationswarm.cli.main import app
from migrationswarm.core.security import write_json_atomic
from migrationswarm.core.tasks import Task, TaskStatus, TaskType
from migrationswarm.core.validation import ArtifactConsistencyValidator
from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.repositories import ProjectRepository, TaskRepository
from migrationswarm.persistence.mapping import Project
from migrationswarm.persistence.redis import ReadyTaskQueue

RUNNER = CliRunner()


def test_release_subprocess_environment_is_resource_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAVEN_OPTS", "-Xmx2g")
    environment = _release_environment()

    assert environment["MAVEN_OPTS"] == "-Xmx256m"
    assert environment["MIGRATIONSWARM_RELEASE_SERVICE_WORKERS"] == "1"


def _write_consistent_artifacts(root: Path) -> tuple[str, str]:
    task_id = uuid4()
    run_id = uuid4()
    metadata = root / ".migrationswarm"
    write_json_atomic(
        metadata / "multi-runs" / f"{run_id}.json",
        {
            "run_id": str(run_id),
            "selected_services": ["Inventory Service"],
            "services": [{"service_name": "Inventory Service", "task_ids": [str(task_id)]}],
        },
    )
    write_json_atomic(
        metadata / "extraction-results" / f"{task_id}.json",
        {"task_id": str(task_id), "selected_candidate": {"name": "Inventory Service"}},
    )
    return str(run_id), str(task_id)


def test_artifact_consistency_validator_accepts_lineage(tmp_path: Path) -> None:
    run_id, _ = _write_consistent_artifacts(tmp_path)
    report = ArtifactConsistencyValidator().validate(tmp_path)
    assert report.valid
    assert str(report.run_id) == run_id


def test_artifact_consistency_validator_rejects_corruption_and_traversal(tmp_path: Path) -> None:
    metadata = tmp_path / ".migrationswarm"
    metadata.mkdir()
    (metadata / "broken.json").write_text("not-json", encoding="utf-8")
    write_json_atomic(metadata / "unsafe.json", {"artifact_path": "../outside.json"})
    report = ArtifactConsistencyValidator().validate(tmp_path)
    assert not report.valid
    assert {finding.category for finding in report.findings} >= {"artifact", "path_containment"}


def test_validate_run_cli_is_read_only_and_concise(tmp_path: Path) -> None:
    _write_consistent_artifacts(tmp_path)
    before = json.dumps(sorted(path.as_posix() for path in tmp_path.rglob("*")))
    result = RUNNER.invoke(app, ["validate-run", str(tmp_path)])
    after = json.dumps(sorted(path.as_posix() for path in tmp_path.rglob("*")))
    assert result.exit_code == 0
    assert "status=ok" in result.stdout
    assert before == after


def test_sqlite_state_survives_database_reconstruction(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    project = Project(name="restart", repository_path=tmp_path)
    task = Task(
        project_id=project.id,
        task_type=TaskType.TEST,
        title="restart",
        description="restart",
        status=TaskStatus.READY,
    )
    first = Database(f"sqlite:///{database_path}")
    first.create_all_for_tests()
    ProjectRepository(first.session_factory).create(project)
    TaskRepository(first.session_factory).create(task)
    first.dispose()
    second = Database(f"sqlite:///{database_path}")
    assert TaskRepository(second.session_factory).get_required(task.id).status is TaskStatus.READY
    second.dispose()


def test_redis_loss_does_not_remove_durable_ready_task(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'durable.db'}")
    database.create_all_for_tests()
    project = Project(name="redis-loss", repository_path=tmp_path)
    ProjectRepository(database.session_factory).create(project)
    task = Task(
        project_id=project.id,
        task_type=TaskType.TEST,
        title="durable",
        description="durable",
        status=TaskStatus.READY,
    )
    TaskRepository(database.session_factory).create(task)
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = ReadyTaskQueue(client)
    queue.enqueue(task.id)
    client.flushall()
    assert TaskRepository(database.session_factory).get_required(task.id).status is TaskStatus.READY
    database.dispose()


def test_atomic_write_preserves_previous_artifact_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.json"
    write_json_atomic(path, {"version": 1})

    def fail_replace(source: str, target: Path) -> None:
        del source, target
        raise OSError("interrupted replace")

    monkeypatch.setattr("migrationswarm.core.security.artifacts.os.replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        write_json_atomic(path, {"version": 2})
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
