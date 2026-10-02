"""Tests for the copy-first Spring Boot service extraction workflow."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from migrationswarm.agents.service_extraction import (
    ExtractionEvidenceError,
    ExtractionLimits,
    ExtractionResponseError,
    ExtractionSafetyError,
    ExtractionWorkspaceError,
    ServiceExtractionAgent,
    ServiceExtractionError,
    service_slug,
)
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext, AgentRegistry, WorkerRuntime
from migrationswarm.core.git import GitWorktreeManager
from migrationswarm.core.models import ModelRequest, ModelResponse
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")

RUNNER = CliRunner()
PROJECT_ID = UUID(int=12000)
SAMPLE_ROOT = Path(__file__).parents[2] / "examples" / "sample-spring-monolith"
SELECTED_CLASSES = [
    "com.example.monolith.GreetingController",
    "com.example.monolith.GreetingService",
    "com.example.monolith.GreetingRepository",
]


def git(root: Path, *args: str) -> str:
    """Run a Git command in a temporary repository."""
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    return result.stdout.strip()


class FakeRouter:
    """Provider-independent scripted model router."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            provider="fake",
            model="fake-coder",
            content=self.contents.pop(0),
            latency_ms=1.0,
        )


def valid_proposal() -> dict[str, Any]:
    """Return a bounded, complete proposal for the checked-in sample candidate."""
    package = "package com.example.monolith;\n\n"
    files = [
        {
            "relative_path": "services/greeting-service/pom.xml",
            "complete_content": "<project><artifactId>greeting-service</artifactId></project>\n",
            "purpose": "Standalone service build descriptor.",
        },
        {
            "relative_path": "services/greeting-service/src/main/resources/application.yml",
            "complete_content": "spring:\n  application:\n    name: greeting-service\n",
            "purpose": "Minimal service configuration.",
        },
        {
            "relative_path": (
                "services/greeting-service/src/main/java/com/example/monolith/"
                "GreetingController.java"
            ),
            "complete_content": package + "public class GreetingController {}\n",
            "purpose": "Copied selected controller.",
        },
        {
            "relative_path": (
                "services/greeting-service/src/main/java/com/example/monolith/"
                "GreetingService.java"
            ),
            "complete_content": package + "public class GreetingService {}\n",
            "purpose": "Copied selected service.",
        },
        {
            "relative_path": (
                "services/greeting-service/src/main/java/com/example/monolith/"
                "GreetingRepository.java"
            ),
            "complete_content": package + "public class GreetingRepository {}\n",
            "purpose": "Copied selected repository.",
        },
        {
            "relative_path": (
                "services/greeting-service/src/main/java/com/example/monolith/"
                "GreetingApplication.java"
            ),
            "complete_content": package + "public class GreetingApplication {}\n",
            "purpose": "Declared Spring Boot bootstrap support class.",
        },
        {
            "relative_path": (
                "services/greeting-service/src/test/java/com/example/monolith/"
                "GreetingServiceTest.java"
            ),
            "complete_content": package + "public class GreetingServiceTest {}\n",
            "purpose": "Initial service test scaffold.",
        },
    ]
    return {
        "summary": "Copy the grounded Greeting candidate into an isolated service.",
        "generated_files": files,
        "copied_classes": SELECTED_CLASSES,
        "generated_support_classes": [
            "com.example.monolith.GreetingApplication",
            "com.example.monolith.GreetingServiceTest",
        ],
        "dependencies": [],
        "warnings": [],
    }


def proposal_json() -> str:
    """Serialize the valid fake response."""
    return json.dumps(valid_proposal())


def make_context(task: Task, workspace: Path, **metadata: Any) -> AgentContext:
    """Build an extraction context selecting the sample candidate."""
    values: dict[str, Any] = {"service_name": "Greeting Service"}
    values.update(metadata)
    return AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(workspace),
        metadata=values,
    )


@pytest.fixture
def extraction_task(tmp_path: Path) -> tuple[Path, Task, Path]:
    """Create a clean Git repository, evidence set, and managed worktree."""
    root = tmp_path / "repository"
    root.mkdir()
    shutil.copytree(SAMPLE_ROOT / "src", root / "src")
    metadata = root / ".migrationswarm"
    metadata.mkdir()
    for filename in (
        "service-boundaries.json",
        "migration-plan.json",
        "java-dependency-graph.json",
        "architecture-report.json",
    ):
        shutil.copy(SAMPLE_ROOT / ".migrationswarm" / filename, metadata / filename)
    git(root, "init")
    git(root, "config", "user.name", "MigrationSwarm Tests")
    git(root, "config", "user.email", "tests@migrationswarm.local")
    git(root, "branch", "-M", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial extraction fixture")
    task = Task(
        id=uuid4(),
        project_id=PROJECT_ID,
        task_type=TaskType.SERVICE_EXTRACTION,
        title="Extract Greeting Service",
        description="Copy the approved Greeting candidate.",
        status=TaskStatus.READY,
        assigned_agent=ServiceExtractionAgent.name,
    )
    worktree = GitWorktreeManager(root).create_worktree(task)
    return root, task, worktree.path


def run_agent(
    task: Task,
    workspace: Path,
    router: FakeRouter | None = None,
    **metadata: Any,
) -> tuple[Any, FakeRouter]:
    """Execute the agent with a fake router."""
    selected_router = router or FakeRouter([proposal_json()])
    result = ServiceExtractionAgent(selected_router).execute(
        task, make_context(task, workspace, **metadata)
    )
    return result, selected_router


def test_service_slug_is_deterministic() -> None:
    assert service_slug("Greeting Service") == "greeting-service"
    assert service_slug("  Payments/API  ") == "payments-api"


def test_missing_workspace_is_rejected(extraction_task: tuple[Path, Task, Path]) -> None:
    _, task, _ = extraction_task
    with pytest.raises(ExtractionWorkspaceError, match="workspace_path"):
        ServiceExtractionAgent(FakeRouter([])).execute(
            task,
            AgentContext(project_id=task.project_id, task=task),
        )


def test_main_repository_is_rejected(extraction_task: tuple[Path, Task, Path]) -> None:
    root, task, _ = extraction_task
    with pytest.raises(ExtractionWorkspaceError):
        ServiceExtractionAgent(FakeRouter([])).execute(task, make_context(task, root))


def test_workspace_outside_managed_root_is_rejected(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    root, task, _ = extraction_task
    outside = root.parent / str(task.id)
    outside.mkdir()
    with pytest.raises(ExtractionWorkspaceError, match="worktrees"):
        ServiceExtractionAgent(FakeRouter([])).execute(task, make_context(task, outside))


def test_context_is_grounded_and_bounded(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    router = FakeRouter([proposal_json()])
    agent = ServiceExtractionAgent(router)
    built = agent.build_context(task, make_context(task, workspace))
    assert built.target_service_directory == "services/greeting-service"
    assert [item.class_name for item in built.source_files] == sorted(SELECTED_CLASSES)
    assert all("SampleApplication" not in item.content for item in built.source_files)
    assert built.acceptance_criteria
    assert built.context_bytes < 60_000


def test_model_request_uses_coding_capability_and_no_model_id(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    router = FakeRouter([proposal_json()])
    ServiceExtractionAgent(router).execute(task, make_context(task, workspace))
    request = router.requests[0]
    assert request.capability.value == "coding"
    assert request.max_tokens == 3500
    assert request.metadata == {"agent": "service-extraction"}
    assert "SampleApplication" not in request.messages[-1].content


def test_dry_run_does_not_call_model_or_write(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    root, task, workspace = extraction_task
    router = FakeRouter([])
    result = ServiceExtractionAgent(router).dry_run(task, make_context(task, workspace))
    assert result.success is True
    assert result.metadata["dry_run"] is True
    assert router.requests == []
    assert not (workspace / "services").exists()
    assert not (root / ".migrationswarm" / "extraction-results").exists()


def test_successful_execution_writes_only_target_and_artifact(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    root, task, workspace = extraction_task
    result, router = run_agent(task, workspace)
    assert result.success is True
    assert len(router.requests) == 1
    assert (workspace / "services/greeting-service/pom.xml").is_file()
    assert (
        workspace
        / "services/greeting-service/src/main/java/com/example/monolith/GreetingService.java"
    ).is_file()
    assert not (root / "services").exists()
    artifact = root / ".migrationswarm" / "extraction-results" / f"{task.id}.json"
    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["target_service_directory"] == "services/greeting-service"
    assert payload["model_provider"] == "fake"


def test_generated_service_requires_build_descriptor(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    """Extraction fails closed before verification when no build descriptor is returned."""
    _, task, workspace = extraction_task
    payload = valid_proposal()
    payload["generated_files"] = [
        item
        for item in payload["generated_files"]
        if not str(item["relative_path"]).endswith("pom.xml")
    ]

    with pytest.raises(ExtractionSafetyError, match="build descriptor"):
        run_agent(task, workspace, FakeRouter([json.dumps(payload)]))


def test_generated_support_classes_must_be_declared(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    payload = valid_proposal()
    payload["generated_support_classes"] = []
    with pytest.raises(ExtractionSafetyError, match="declared"):
        run_agent(task, workspace, FakeRouter([json.dumps(payload)]))


def test_selected_classes_must_be_generated(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    payload = valid_proposal()
    payload["generated_files"] = [payload["generated_files"][0]]
    with pytest.raises(ExtractionSafetyError, match="Selected classes"):
        run_agent(task, workspace, FakeRouter([json.dumps(payload)]))


@pytest.mark.parametrize("bad_path", ["../escape.java", "/absolute.java", "services/other/a.java"])
def test_generated_paths_cannot_escape_selected_service(
    extraction_task: tuple[Path, Task, Path], bad_path: str
) -> None:
    _, task, workspace = extraction_task
    payload = valid_proposal()
    payload["generated_files"][2]["relative_path"] = bad_path
    with pytest.raises(ExtractionSafetyError):
        run_agent(task, workspace, FakeRouter([json.dumps(payload)]))


def test_generated_java_package_must_match_path(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    payload = valid_proposal()
    payload["generated_files"][2]["complete_content"] = (
        "package wrong.package;\n\npublic class GreetingController {}"
    )
    with pytest.raises(ExtractionSafetyError, match="package"):
        run_agent(task, workspace, FakeRouter([json.dumps(payload)]))


def test_generated_file_limits_are_enforced(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    agent = ServiceExtractionAgent(
        FakeRouter([proposal_json()]), limits=ExtractionLimits(max_generated_files=2)
    )
    with pytest.raises(ExtractionSafetyError, match="file count"):
        agent.execute(task, make_context(task, workspace))


def test_context_limits_are_enforced(extraction_task: tuple[Path, Task, Path]) -> None:
    _, task, workspace = extraction_task
    agent = ServiceExtractionAgent(
        FakeRouter([]), limits=ExtractionLimits(max_context_files=2)
    )
    with pytest.raises(ExtractionEvidenceError, match="file count"):
        agent.dry_run(task, make_context(task, workspace))


def test_malformed_response_gets_one_repair_attempt(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    router = FakeRouter(["not-json", proposal_json()])
    result, _ = run_agent(task, workspace, router)
    assert result.success is True
    assert len(router.requests) == 2


def test_malformed_response_after_repair_is_rejected(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    with pytest.raises(ExtractionResponseError, match="repair attempt"):
        run_agent(task, workspace, FakeRouter(["bad", "still bad"]))


def test_missing_candidate_is_rejected(extraction_task: tuple[Path, Task, Path]) -> None:
    _, task, workspace = extraction_task
    with pytest.raises(ServiceExtractionError, match="not present"):
        ServiceExtractionAgent(FakeRouter([])).dry_run(
            task, make_context(task, workspace, service_name="Unknown Service")
        )


def test_atomic_write_rolls_back_partial_generation(
    extraction_task: tuple[Path, Task, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, task, workspace = extraction_task
    agent = ServiceExtractionAgent(FakeRouter([proposal_json()]))
    calls = 0

    def fail_on_second(path: Path, content: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated disk failure")
        path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(agent, "_atomic_write", fail_on_second)
    with pytest.raises(ServiceExtractionError, match="rolled back"):
        agent.execute(task, make_context(task, workspace))
    assert not (workspace / "services").exists()


def test_worker_runtime_executes_extraction_agent_to_verifying(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    graph = TaskGraph()
    graph.add_task(task)
    scheduler = TaskScheduler(graph)
    scheduler.promote_ready_tasks()
    registry = AgentRegistry()
    registry.register(ServiceExtractionAgent(FakeRouter([proposal_json()])))
    result = WorkerRuntime(scheduler, registry).execute(
        task, make_context(task, workspace)
    )
    assert result.success is True
    assert task.status is TaskStatus.VERIFYING


def test_cli_dry_run_uses_existing_worktree(
    extraction_task: tuple[Path, Task, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, task, _ = extraction_task
    router = FakeRouter([])
    monkeypatch.setattr(
        ServiceExtractionAgent,
        "_default_router",
        staticmethod(lambda: router),
    )
    result = RUNNER.invoke(
        app,
        [
            "extract-service",
            str(root),
            "--task-id",
            str(task.id),
            "--service",
            "Greeting Service",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "source_writes=no" in result.stdout
    assert router.requests == []
