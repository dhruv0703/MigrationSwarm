"""Tests for the copy-first Spring Boot service extraction workflow."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from migrationswarm.agents.dependency_analysis import (
    JavaClass,
    JavaClassRole,
    JavaDependency,
    JavaDependencyGraph,
    JavaDependencyRelationship,
)
from migrationswarm.agents.service_boundary import CandidateService, ServiceBoundaryReport
from migrationswarm.agents.service_extraction import (
    ExtractionEvidenceError,
    ExtractionLimits,
    ExtractionResponseError,
    ExtractionSafetyError,
    ExtractionWorkspaceError,
    ServiceExtractionAgent,
    ServiceExtractionError,
    _select_context_classes,
    service_slug,
)
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext, AgentRegistry, WorkerRuntime
from migrationswarm.core.git import GitRepository, GitWorktreeManager
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


def test_fresh_extraction_worktree_is_clean(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, _, workspace = extraction_task
    repository = GitRepository(workspace)

    assert repository.status_porcelain() == ""
    assert not repository.is_dirty()


def test_modified_tracked_file_reports_clean_start_diagnostics(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    path = workspace / "src/main/java/com/example/monolith/GreetingService.java"
    path.write_text(path.read_text(encoding="utf-8") + "// changed\n", encoding="utf-8")

    with pytest.raises(ExtractionWorkspaceError) as error:
        ServiceExtractionAgent(FakeRouter([])).execute(task, make_context(task, workspace))

    message = str(error.value)
    assert "git_status_porcelain=" in message
    assert "tracked_modified=" in message
    assert "staged=[]" in message
    assert "untracked=[]" in message
    assert "GreetingService.java" in message
    assert "worktree_path=" in message
    assert "starting_commit=" in message


def test_staged_file_reports_staged_clean_start_diagnostics(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    relative = Path("src/main/java/com/example/monolith/GreetingService.java")
    path = workspace / relative
    path.write_text(path.read_text(encoding="utf-8") + "// staged\n", encoding="utf-8")
    git(workspace, "add", relative.as_posix())

    with pytest.raises(ExtractionWorkspaceError) as error:
        ServiceExtractionAgent(FakeRouter([])).execute(task, make_context(task, workspace))

    message = str(error.value)
    assert "staged=['src/main/java/com/example/monolith/GreetingService.java']" in message


def test_untracked_file_reports_untracked_clean_start_diagnostics(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    (workspace / "unexpected-runtime-file.txt").write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(ExtractionWorkspaceError) as error:
        ServiceExtractionAgent(FakeRouter([])).execute(task, make_context(task, workspace))

    message = str(error.value)
    assert "untracked=['unexpected-runtime-file.txt']" in message


def test_ignored_generated_file_does_not_fail_clean_start(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    exclude = Path(git(workspace, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = workspace / exclude
    exclude.write_text(exclude.read_text(encoding="utf-8") + ".generated/\n", encoding="utf-8")
    generated = workspace / ".generated" / "build.log"
    generated.parent.mkdir()
    generated.write_text("ignored\n", encoding="utf-8")
    assert "!! .generated/" in GitRepository(workspace).status_porcelain(include_ignored=True)

    result, _ = run_agent(task, workspace)

    assert result.success is True


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
    with pytest.raises(ExtractionEvidenceError, match="file count") as error:
        agent.dry_run(task, make_context(task, workspace))
    message = str(error.value)
    assert "candidate=Greeting Service" in message
    assert "required_file_count=3" in message
    assert "configured_limit=2" in message
    assert "required_candidate_owned" in message


def _selection_fixture() -> tuple[CandidateService, JavaDependencyGraph, ServiceBoundaryReport]:
    """Build a small graph for deterministic context-selection tests."""
    candidate = CandidateService(
        name="Order",
        description="Order candidate",
        classes=["example.order.OrderService"],
        packages=["example.order"],
        controllers=[],
        services=["example.order.OrderService"],
        repositories=[],
        confidence=1.0,
        reasoning="grounded",
        dependencies_on_other_candidates=[],
        risks=[],
    )
    classes = [
        JavaClass(
            name="OrderService",
            fully_qualified_name="example.order.OrderService",
            package="example.order",
            file_path="src/main/java/example/order/OrderService.java",
            role=JavaClassRole.SERVICE,
        ),
        JavaClass(
            name="OrderPort",
            fully_qualified_name="example.shared.OrderPort",
            package="example.shared",
            file_path="src/main/java/example/shared/OrderPort.java",
            role=JavaClassRole.OTHER,
        ),
        JavaClass(
            name="OrderConfiguration",
            fully_qualified_name="example.order.OrderConfiguration",
            package="example.order",
            file_path="src/main/java/example/order/OrderConfiguration.java",
            role=JavaClassRole.CONFIGURATION,
        ),
        JavaClass(
            name="UnrelatedService",
            fully_qualified_name="example.inventory.UnrelatedService",
            package="example.inventory",
            file_path="src/main/java/example/inventory/UnrelatedService.java",
            role=JavaClassRole.SERVICE,
        ),
    ]
    graph = JavaDependencyGraph(
        repository_root=".",
        total_java_classes=len(classes),
        total_dependency_edges=3,
        classes=classes,
        dependencies=[
            JavaDependency(
                source="example.order.OrderService",
                target="example.shared.OrderPort",
                relationship=JavaDependencyRelationship.FIELD_DEPENDENCY,
                evidence="field_dependency: OrderPort",
            ),
            JavaDependency(
                source="example.order.OrderService",
                target="example.order.OrderConfiguration",
                relationship=JavaDependencyRelationship.CONSTRUCTOR_DEPENDENCY,
                evidence="constructor_dependency: OrderConfiguration",
            ),
        ],
    )
    boundary = ServiceBoundaryReport(
        candidate_services=[candidate],
        shared_components=[],
        unresolved_classes=[],
        overall_reasoning="grounded",
        warnings=[],
        model_provider="test",
        model_name="test",
    )
    return candidate, graph, boundary


def test_context_selection_prefers_direct_dependencies_and_excludes_unrelated() -> None:
    candidate, graph, boundary = _selection_fixture()
    classes_by_name = {item.fully_qualified_name: item for item in graph.classes}
    selected, diagnostics = _select_context_classes(
        candidate,
        graph,
        boundary,
        classes_by_name,
        ExtractionLimits(max_context_files=2),
    )

    assert [item.fully_qualified_name for item in selected] == [
        "example.order.OrderService",
        "example.shared.OrderPort",
    ]
    assert diagnostics.selected_file_count == 2
    assert diagnostics.optional_file_count == 2
    assert "UnrelatedService.java" not in json.dumps(diagnostics.model_dump())


def test_context_selection_is_deterministic_and_retains_merged_internal_classes() -> None:
    candidate, graph, boundary = _selection_fixture()
    internal = JavaClass(
        name="OrderInternal",
        fully_qualified_name="example.order.internal.OrderInternal",
        package="example.order.internal",
        file_path="src/main/java/example/order/internal/OrderInternal.java",
        role=JavaClassRole.COMPONENT,
    )
    candidate.classes.append(internal.fully_qualified_name)
    graph.classes.append(internal)
    graph.total_java_classes += 1
    classes_by_name = {item.fully_qualified_name: item for item in graph.classes}
    first = _select_context_classes(
        candidate, graph, boundary, classes_by_name, ExtractionLimits(max_context_files=3)
    )
    second = _select_context_classes(
        candidate, graph, boundary, classes_by_name, ExtractionLimits(max_context_files=3)
    )

    assert first[0] == second[0]
    assert first[1] == second[1]
    required_paths = first[1].selected_files_by_reason["required_candidate_owned"]
    assert "src/main/java/example/order/internal/OrderInternal.java" in required_paths


def test_context_selection_reports_required_files_over_hard_limit() -> None:
    candidate, graph, boundary = _selection_fixture()
    candidate.classes.extend(
        [
            "example.order.internal.OrderInternalA",
            "example.order.internal.OrderInternalB",
        ]
    )
    for name in ("OrderInternalA", "OrderInternalB"):
        graph.classes.append(
            JavaClass(
                name=name,
                fully_qualified_name=f"example.order.internal.{name}",
                package="example.order.internal",
                file_path=f"src/main/java/example/order/internal/{name}.java",
                role=JavaClassRole.COMPONENT,
            )
        )
    classes_by_name = {item.fully_qualified_name: item for item in graph.classes}
    with pytest.raises(ExtractionEvidenceError, match="file count") as error:
        _select_context_classes(
            candidate, graph, boundary, classes_by_name, ExtractionLimits(max_context_files=2)
        )

    message = str(error.value)
    assert "required_file_count=3" in message
    assert "configured_limit=2" in message
    assert "OrderInternalA.java" in message
    assert "OrderInternalB.java" in message


def _sized_selection_fixture(
    required_count: int, optional_count: int
) -> tuple[CandidateService, JavaDependencyGraph, ServiceBoundaryReport]:
    """Build a graph with a controlled required/optional context size."""
    required_names = [f"example.order.Required{index}" for index in range(required_count)]
    optional_names = [f"example.shared.Optional{index}" for index in range(optional_count)]
    classes = [
        JavaClass(
            name=name.rsplit(".", 1)[1],
            fully_qualified_name=name,
            package="example.order",
            file_path=f"src/main/java/example/order/{name.rsplit('.', 1)[1]}.java",
            role=JavaClassRole.SERVICE,
        )
        for name in required_names
    ]
    classes.extend(
        JavaClass(
            name=name.rsplit(".", 1)[1],
            fully_qualified_name=name,
            package="example.shared",
            file_path=f"src/main/java/example/shared/{name.rsplit('.', 1)[1]}.java",
            role=JavaClassRole.OTHER,
        )
        for name in optional_names
    )
    dependencies = [
        JavaDependency(
            source=required_names[0],
            target=name,
            relationship=JavaDependencyRelationship.FIELD_DEPENDENCY,
            evidence=f"field_dependency: {name.rsplit('.', 1)[1]}",
        )
        for name in optional_names
    ]
    candidate = CandidateService(
        name="Sized Service",
        description="Sized candidate",
        classes=required_names,
        packages=["example.order"],
        controllers=[],
        services=required_names,
        repositories=[],
        confidence=1.0,
        reasoning="grounded",
        dependencies_on_other_candidates=[],
        risks=[],
    )
    graph = JavaDependencyGraph(
        repository_root=".",
        total_java_classes=len(classes),
        total_dependency_edges=len(dependencies),
        classes=classes,
        dependencies=dependencies,
    )
    boundary = ServiceBoundaryReport(
        candidate_services=[candidate],
        shared_components=[],
        unresolved_classes=[],
        overall_reasoning="grounded",
        warnings=[],
        model_provider="test",
        model_name="test",
    )
    return candidate, graph, boundary


def test_six_optional_files_fit_after_ten_required_files_under_default_ceiling() -> None:
    candidate, graph, boundary = _sized_selection_fixture(10, 10)
    classes_by_name = {item.fully_qualified_name: item for item in graph.classes}

    selected, diagnostics = _select_context_classes(
        candidate, graph, boundary, classes_by_name, ExtractionLimits()
    )

    assert diagnostics.configured_limit == 16
    assert diagnostics.required_file_count == 10
    assert diagnostics.optional_file_count == 10
    assert diagnostics.selected_file_count == 16
    assert len(selected) == 16
    assert set(candidate.classes).issubset({item.fully_qualified_name for item in selected})


def test_sixteen_required_files_fit_under_default_ceiling() -> None:
    candidate, graph, boundary = _sized_selection_fixture(16, 3)
    classes_by_name = {item.fully_qualified_name: item for item in graph.classes}

    selected, diagnostics = _select_context_classes(
        candidate, graph, boundary, classes_by_name, ExtractionLimits()
    )

    assert len(selected) == 16
    assert diagnostics.required_file_count == 16
    assert diagnostics.selected_file_count == 16
    assert diagnostics.selected_files_by_reason["direct_compile_dependency"] == []


def test_seventeen_required_files_fail_without_truncation() -> None:
    candidate, graph, boundary = _sized_selection_fixture(17, 3)
    classes_by_name = {item.fully_qualified_name: item for item in graph.classes}

    with pytest.raises(ExtractionEvidenceError, match="file count") as error:
        _select_context_classes(candidate, graph, boundary, classes_by_name, ExtractionLimits())

    message = str(error.value)
    assert "required_file_count=17" in message
    assert "configured_limit=16" in message
    assert "Required16.java" in message


def test_optional_context_never_displaces_required_files() -> None:
    candidate, graph, boundary = _sized_selection_fixture(16, 10)
    classes_by_name = {item.fully_qualified_name: item for item in graph.classes}

    selected, diagnostics = _select_context_classes(
        candidate, graph, boundary, classes_by_name, ExtractionLimits()
    )

    assert [item.fully_qualified_name for item in selected] == sorted(candidate.classes)
    assert diagnostics.selected_file_count == diagnostics.required_file_count == 16


def test_context_byte_limits_remain_independent_of_file_ceiling(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    agent = ServiceExtractionAgent(
        FakeRouter([]), limits=ExtractionLimits(max_context_bytes_per_file=1)
    )

    with pytest.raises(ExtractionEvidenceError, match="source file exceeds context limit"):
        agent.dry_run(task, make_context(task, workspace))


def test_total_context_byte_limit_remains_independent_of_file_ceiling(
    extraction_task: tuple[Path, Task, Path],
) -> None:
    _, task, workspace = extraction_task
    agent = ServiceExtractionAgent(
        FakeRouter([]), limits=ExtractionLimits(max_total_context_bytes=10)
    )

    with pytest.raises(ExtractionEvidenceError, match="total byte limit"):
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
