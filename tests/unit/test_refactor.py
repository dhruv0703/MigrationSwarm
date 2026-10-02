"""Integration-style tests for the isolated RefactorAgent."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from migrationswarm.agents.refactor import (
    ChangeType,
    DirtyWorkspaceError,
    FileChange,
    RefactorAgent,
    RefactorContextError,
    RefactorGroundingError,
    RefactorLimits,
    RefactorProposal,
    RefactorResponseError,
    RefactorWorkspaceError,
)
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext, AgentRegistry, WorkerRuntime
from migrationswarm.core.git import GitWorktreeManager
from migrationswarm.core.models import ModelCapability, ModelRequest, ModelResponse
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
RUNNER = CliRunner()
PROJECT_ID = UUID(int=9100)
SERVICE_FILE = "src/main/java/com/example/monolith/GreetingService.java"
CONTROLLER_FILE = "src/main/java/com/example/monolith/GreetingController.java"
PACKAGE_DIR = "src/main/java/com/example/monolith"


def git(root: Path, *args: str) -> str:
    """Run a Git command for a temporary repository."""
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def isolated_task(tmp_path: Path) -> tuple[Path, Task, Path]:
    """Create a clean committed repository and one task worktree."""
    root = tmp_path / "repository"
    source = root / SERVICE_FILE
    controller = root / CONTROLLER_FILE
    root.mkdir()
    source.parent.mkdir(parents=True)
    git(root, "init")
    git(root, "config", "user.name", "MigrationSwarm Tests")
    git(root, "config", "user.email", "tests@migrationswarm.local")
    git(root, "branch", "-M", "main")
    (root / ".gitignore").write_text(".migrationswarm/\n", encoding="utf-8")
    source.write_text(
        """package com.example.monolith;

import org.springframework.stereotype.Service;

@Service
public class GreetingService {
    public String greeting() {
        return \"Hello\";
    }
}
""",
        encoding="utf-8",
    )
    controller.write_text(
        """package com.example.monolith;

public class GreetingController {
}
""",
        encoding="utf-8",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    task_id = uuid4()
    task = Task(
        id=task_id,
        project_id=PROJECT_ID,
        task_type=TaskType.CODE_REFACTOR,
        title="Create Greeting facade",
        description=(
            "Create a GreetingFacade class in the existing greeting package that "
            "delegates to GreetingService."
        ),
        status=TaskStatus.PENDING,
        assigned_agent=RefactorAgent.name,
    )
    worktree = GitWorktreeManager(root).create_worktree(task)
    return root, task, worktree.path


class FakeRouter:
    """Scripted router that records requests and never contacts a provider."""

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


def context(task: Task, workspace: Path, **metadata: Any) -> AgentContext:
    """Build a task context with a bounded source scope."""
    values: dict[str, Any] = {
        "affected_files": [SERVICE_FILE],
        "expected_outputs": ["GreetingFacade.java"],
        "acceptance_criteria": ["The facade delegates to GreetingService."],
    }
    values.update(metadata)
    return AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(workspace),
        metadata=values,
    )


def create_content() -> str:
    """Return complete content for a safe new Java class."""
    return """package com.example.monolith;

public class GreetingFacade {
    private final GreetingService service;

    public GreetingFacade(GreetingService service) {
        this.service = service;
    }

    public String greeting() {
        return service.greeting();
    }
}
"""


def modify_content() -> str:
    """Return complete content for a safe existing-file modification."""
    return """package com.example.monolith;

import org.springframework.stereotype.Service;

@Service
public class GreetingService {
    public String greeting() {
        return \"Hello from the isolated worktree\";
    }
}
"""


def proposal(*changes: dict[str, object]) -> str:
    """Serialize a strict fake model proposal."""
    return json.dumps(
        {
            "summary": "Apply one narrow isolated refactor.",
            "changes": list(changes),
            "warnings": [],
        }
    )


def create_change(path: str = f"{PACKAGE_DIR}/GreetingFacade.java") -> dict[str, object]:
    """Build a valid CREATE response item."""
    return {
        "path": path,
        "change_type": "create",
        "complete_new_content": create_content(),
        "reasoning": "The facade delegates without changing existing behavior.",
    }


def modify_change(path: str = SERVICE_FILE) -> dict[str, object]:
    """Build a valid MODIFY response item."""
    return {
        "path": path,
        "change_type": "modify",
        "complete_new_content": modify_content(),
        "reasoning": "The service text is changed mechanically for the task objective.",
    }


def run_agent(
    task: Task,
    workspace: Path,
    router: FakeRouter,
    **metadata: Any,
) -> tuple[Any, FakeRouter]:
    """Execute the production agent with fake model output."""
    result = RefactorAgent(router).execute(task, context(task, workspace, **metadata))
    return result, router


def test_missing_workspace_rejected(isolated_task: tuple[Path, Task, Path]) -> None:
    """A refactor cannot run without a workspace path."""
    _, task, _ = isolated_task
    with pytest.raises(RefactorWorkspaceError, match="workspace_path"):
        RefactorAgent(FakeRouter([])).execute(
            task,
            AgentContext(project_id=task.project_id, task=task),
        )


def test_main_repository_rejected(isolated_task: tuple[Path, Task, Path]) -> None:
    """The original repository root is never accepted as a code workspace."""
    root, task, _ = isolated_task
    with pytest.raises(RefactorWorkspaceError):
        RefactorAgent(FakeRouter([])).execute(
            task,
            context(task, root),
        )


def test_outside_managed_worktree_rejected(isolated_task: tuple[Path, Task, Path]) -> None:
    """A Git checkout outside the managed layout is rejected."""
    root, task, _ = isolated_task
    outside = root.parent / str(task.id)
    outside.mkdir()
    with pytest.raises(RefactorWorkspaceError, match="worktrees"):
        RefactorAgent(FakeRouter([])).execute(task, context(task, outside))


def test_unregistered_managed_worktree_rejected(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """A directory with a task-shaped name is not enough to authorize execution."""
    root, task, _ = isolated_task
    unknown_id = uuid4()
    unknown_path = root / ".migrationswarm" / "worktrees" / str(unknown_id)
    unknown_path.mkdir(parents=True)
    unknown_task = task.model_copy(update={"id": unknown_id})

    with pytest.raises(RefactorWorkspaceError, match="worktree"):
        RefactorAgent(FakeRouter([])).execute(
            unknown_task,
            context(unknown_task, unknown_path),
        )


def test_dirty_worktree_is_rejected(isolated_task: tuple[Path, Task, Path]) -> None:
    """Refactor execution requires a clean starting worktree."""
    _, task, workspace = isolated_task
    (workspace / SERVICE_FILE).write_text("dirty\n", encoding="utf-8")
    with pytest.raises(DirtyWorkspaceError):
        RefactorAgent(FakeRouter([])).execute(task, context(task, workspace))


def test_coding_capability_and_compact_context(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """The request uses CODING and sends only bounded relevant source."""
    _, task, workspace = isolated_task
    router = FakeRouter([proposal(create_change())])
    result = RefactorAgent(router).dry_run(task, context(task, workspace))

    assert router.requests[0].capability is ModelCapability.CODING
    assert "GreetingService" in router.requests[0].messages[-1].content
    assert "pom.xml" not in router.requests[0].messages[-1].content
    assert result.metadata["dry_run"] is True


def test_context_size_limit(isolated_task: tuple[Path, Task, Path]) -> None:
    """Oversized source is rejected instead of silently truncated."""
    _, task, workspace = isolated_task
    limits = RefactorLimits(max_bytes_per_file=10)
    with pytest.raises(RefactorContextError, match="exceeds limit"):
        RefactorAgent(FakeRouter([]), limits=limits).build_context(
            task, context(task, workspace)
        )


def test_valid_create_writes_only_worktree(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """A valid CREATE is applied only to the isolated checkout."""
    root, task, workspace = isolated_task
    result, _ = run_agent(task, workspace, FakeRouter([proposal(create_change())]))

    created = workspace / f"{PACKAGE_DIR}/GreetingFacade.java"
    assert created.is_file()
    assert not (root / f"{PACKAGE_DIR}/GreetingFacade.java").exists()
    assert result.metadata["changed_files"] == [
        f"{PACKAGE_DIR}/GreetingFacade.java"
    ]


def test_valid_modify_writes_complete_content(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """A valid MODIFY replaces the complete existing file."""
    _, task, workspace = isolated_task
    result, _ = run_agent(task, workspace, FakeRouter([proposal(modify_change())]))

    assert "isolated worktree" in (workspace / SERVICE_FILE).read_text(encoding="utf-8")
    assert result.success


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({**create_change(), "change_type": "delete"}, "DELETE"),
        ({**create_change(), "path": "/absolute/Greeting.java"}, "Absolute"),
        ({**create_change(), "path": "../outside.java"}, "traversal"),
        ({**create_change(), "path": ".git/config"}, "Protected"),
        ({**create_change(), "path": ".migrationswarm/result.json"}, "Protected"),
        ({**create_change(), "path": f"{PACKAGE_DIR}/image.bin"}, "Unsupported"),
        (
            {**modify_change("missing.java"), "path": f"{PACKAGE_DIR}/missing.java"},
            "does not exist",
        ),
        ({**create_change(), "path": SERVICE_FILE}, "already exists"),
        ({**create_change(), "complete_new_content": ""}, "empty"),
    ],
)
def test_invalid_file_changes_are_rejected(
    isolated_task: tuple[Path, Task, Path],
    change: dict[str, object],
    message: str,
) -> None:
    """Unsafe, unsupported, empty, and invalid operations write nothing."""
    _, task, workspace = isolated_task
    original = (workspace / SERVICE_FILE).read_text(encoding="utf-8")
    content = proposal(change)
    agent = RefactorAgent(FakeRouter([content, content]))

    with pytest.raises((RefactorGroundingError, RefactorResponseError), match=message):
        agent.execute(task, context(task, workspace))
    assert (workspace / SERVICE_FILE).read_text(encoding="utf-8") == original


def test_max_changed_files_enforced(isolated_task: tuple[Path, Task, Path]) -> None:
    """The proposal cannot exceed the configured file count."""
    _, task, workspace = isolated_task
    changes = [
        modify_change(SERVICE_FILE),
        modify_change(CONTROLLER_FILE),
        create_change(f"{PACKAGE_DIR}/One.java"),
        create_change(f"{PACKAGE_DIR}/Two.java"),
        create_change(f"{PACKAGE_DIR}/Three.java"),
    ]
    agent = RefactorAgent(FakeRouter([proposal(*changes), proposal(*changes)]))
    with pytest.raises(RefactorGroundingError, match="limit"):
        agent.execute(
            task,
            context(
                task,
                workspace,
                affected_files=[SERVICE_FILE, CONTROLLER_FILE],
                allowed_directories=[PACKAGE_DIR],
            ),
        )


def test_artifact_diff_and_agent_result_structure(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """Successful execution returns diff metadata and writes the safe artifact."""
    root, task, workspace = isolated_task
    result, _ = run_agent(task, workspace, FakeRouter([proposal(modify_change())]))

    assert result.task_id == task.id
    assert result.agent_name == RefactorAgent.name
    assert result.success
    assert result.metadata["changed_files"] == [SERVICE_FILE]
    assert "+        return \"Hello from the isolated worktree\";" in result.metadata["diff"]
    assert result.artifacts == [
        f".migrationswarm/refactor-results/{task.id}.json"
    ]
    artifact = root / result.artifacts[0]
    assert artifact.is_file()
    assert not (workspace / ".migrationswarm" / "refactor-results").exists()


def test_dry_run_writes_nothing(isolated_task: tuple[Path, Task, Path]) -> None:
    """Dry-run validates the proposal without source or artifact writes."""
    root, task, workspace = isolated_task
    result = RefactorAgent(FakeRouter([proposal(create_change())])).dry_run(
        task, context(task, workspace)
    )

    assert result.success
    assert not (workspace / f"{PACKAGE_DIR}/GreetingFacade.java").exists()
    assert not (root / ".migrationswarm" / "refactor-results").exists()


def test_artifact_does_not_contain_provider_credentials(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """Result artifacts contain provider metadata but no credential fields."""
    root, task, workspace = isolated_task
    result, _ = run_agent(task, workspace, FakeRouter([proposal(modify_change())]))

    artifact = (root / result.artifacts[0]).read_text(encoding="utf-8")
    assert "api_key" not in artifact.lower()
    assert "GROQ_API_KEY" not in artifact


def test_malformed_response_gets_one_repair_attempt(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """Malformed model output receives exactly one repair request."""
    _, task, workspace = isolated_task
    router = FakeRouter(["not json", proposal(create_change())])
    result = RefactorAgent(router).dry_run(task, context(task, workspace))

    assert result.success
    assert len(router.requests) == 2
    assert "Repair the invalid response" in router.requests[1].messages[-1].content


def test_two_invalid_responses_fail_without_writing(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """Repair is bounded and invalid output cannot reach the filesystem."""
    _, task, workspace = isolated_task
    router = FakeRouter(["bad", "still bad"])
    with pytest.raises(RefactorResponseError, match="one repair attempt"):
        RefactorAgent(router).execute(task, context(task, workspace))
    assert len(router.requests) == 2
    assert not (workspace / f"{PACKAGE_DIR}/GreetingFacade.java").exists()


def test_partial_write_rolls_back(
    isolated_task: tuple[Path, Task, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source write failure restores already-written files."""
    _, task, workspace = isolated_task
    changes = [modify_change(SERVICE_FILE), modify_change(CONTROLLER_FILE)]
    original_service = (workspace / SERVICE_FILE).read_bytes()
    original_controller = (workspace / CONTROLLER_FILE).read_bytes()
    original_atomic_write = RefactorAgent._atomic_write
    calls = 0

    def fail_second(path: Path, content: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        original_atomic_write(path, content)

    monkeypatch.setattr(RefactorAgent, "_atomic_write", staticmethod(fail_second))
    agent = RefactorAgent(FakeRouter([proposal(*changes)]))

    with pytest.raises(Exception, match="rolled back"):
        agent.execute(
            task,
            context(
                task,
                workspace,
                affected_files=[SERVICE_FILE, CONTROLLER_FILE],
                allowed_paths=[SERVICE_FILE, CONTROLLER_FILE],
            ),
        )
    assert (workspace / SERVICE_FILE).read_bytes() == original_service
    assert (workspace / CONTROLLER_FILE).read_bytes() == original_controller


def test_worker_runtime_leaves_successful_refactor_verifying(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """The existing WorkerRuntime applies normal READY-to-VERIFYING semantics."""
    _, task, workspace = isolated_task
    task.status = TaskStatus.PENDING
    router = FakeRouter([proposal(create_change())])
    agent = RefactorAgent(router)
    registry = AgentRegistry()
    registry.register(agent)
    graph = TaskGraph([task])
    scheduler = TaskScheduler(graph)
    scheduler.schedule()
    runtime = WorkerRuntime(scheduler, registry)
    result = runtime.execute(task, context(task, workspace))

    assert result.success
    assert task.status is TaskStatus.VERIFYING


def test_cli_refactor_uses_existing_worktree(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI uses an existing isolated worktree and leaves main source unchanged."""
    root, task, workspace = isolated_task
    router = FakeRouter([proposal(create_change())])
    fake_agent = RefactorAgent(router)
    from migrationswarm.cli import main as cli_main

    monkeypatch.setattr(cli_main, "RefactorAgent", lambda: fake_agent)
    result = RUNNER.invoke(
        app,
        [
            "refactor",
            str(root),
            "--task-id",
            str(task.id),
            "--instruction",
            task.description,
        ],
    )

    assert result.exit_code == 0
    assert "changed_files=" in result.stdout
    assert not (root / f"{PACKAGE_DIR}/GreetingFacade.java").exists()
    assert (workspace / f"{PACKAGE_DIR}/GreetingFacade.java").exists()


def test_change_models_are_strict() -> None:
    """The proposal models reject unexpected fields and empty content for validation."""
    change = FileChange(
        path="src/A.java",
        change_type=ChangeType.CREATE,
        complete_new_content="class A {}",
        reasoning="new class",
    )
    parsed = RefactorProposal(summary="one", changes=[change])
    assert parsed.changes[0].change_type is ChangeType.CREATE
    with pytest.raises(ValueError):
        RefactorProposal.model_validate(
            {"summary": "one", "changes": [], "unexpected": True}
        )
