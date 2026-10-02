"""Unit tests for grounded migration planning and task conversion."""

import json
import shutil
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest
from typer.testing import CliRunner

from migrationswarm.agents.migration_planning import (
    MIGRATION_PLAN_JSON_ARTIFACT,
    MIGRATION_PLAN_MARKDOWN_ARTIFACT,
    MigrationPlan,
    MigrationPlanningAgent,
    MigrationPlanResponseError,
    MigrationStep,
)
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext
from migrationswarm.core.models import (
    ModelCapability,
    ModelDefinition,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelRouter,
    ProviderRegistry,
)
from migrationswarm.core.models.registry import ModelRegistry
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

PROJECT_ID = UUID(int=8000)
RUNNER = CliRunner()
SAMPLE_ROOT = Path(__file__).parents[2] / "examples" / "sample-spring-monolith"


def copy_evidence(root: Path) -> None:
    """Copy the checked-in sample evidence into an isolated workspace."""
    artifact_dir = root / ".migrationswarm"
    artifact_dir.mkdir(parents=True)
    for name in (
        "service-boundaries.json",
        "architecture-report.json",
        "java-dependency-graph.json",
    ):
        shutil.copy(SAMPLE_ROOT / ".migrationswarm" / name, artifact_dir / name)


def valid_output() -> dict[str, object]:
    """Return a realistic structured plan for the sample Greeting service."""
    steps = [
        {
            "step_id": "VERIFY_BOUNDARY",
            "phase_id": "DISCOVERY",
            "title": "Verify selected boundary",
            "description": "Confirm the selected classes form the approved candidate boundary.",
            "task_type": "service_boundary_analysis",
            "dependencies": [],
            "affected_classes": [
                "com.example.monolith.GreetingController",
                "com.example.monolith.GreetingService",
                "com.example.monolith.GreetingRepository",
            ],
            "expected_outputs": ["Boundary verification record"],
            "acceptance_criteria": ["All selected classes are confirmed in one candidate."],
            "risk_level": "low",
            "requires_human_review": False,
        },
        {
            "step_id": "ANALYZE_DEPENDENCIES",
            "phase_id": "DISCOVERY",
            "title": "Identify external dependencies",
            "description": "Record dependencies crossing the selected boundary.",
            "task_type": "dependency_analysis",
            "dependencies": ["VERIFY_BOUNDARY"],
            "affected_classes": ["com.example.monolith.GreetingService"],
            "expected_outputs": ["External dependency inventory"],
            "acceptance_criteria": ["Every dependency is grounded in the graph."],
            "risk_level": "medium",
            "requires_human_review": False,
        },
        {
            "step_id": "EXTRACT_CODE",
            "phase_id": "EXTRACTION",
            "title": "Extract domain code",
            "description": "Plan extraction of the selected service classes.",
            "task_type": "code_refactor",
            "dependencies": ["ANALYZE_DEPENDENCIES"],
            "affected_classes": [
                "com.example.monolith.GreetingService",
                "com.example.monolith.GreetingRepository",
            ],
            "expected_outputs": ["Target service code change set"],
            "acceptance_criteria": ["The target structure contains the selected domain code."],
            "risk_level": "high",
            "requires_human_review": True,
        },
        {
            "step_id": "CREATE_TESTS",
            "phase_id": "VALIDATION",
            "title": "Create migration tests",
            "description": "Plan tests for the extracted service behavior.",
            "task_type": "test",
            "dependencies": ["VERIFY_BOUNDARY"],
            "affected_classes": ["com.example.monolith.GreetingService"],
            "expected_outputs": ["Migration test suite"],
            "acceptance_criteria": ["Tests cover the current Greeting behavior."],
            "risk_level": "low",
            "requires_human_review": False,
        },
        {
            "step_id": "VERIFY",
            "phase_id": "VALIDATION",
            "title": "Verify behavior",
            "description": "Verify the target service behavior against the monolith baseline.",
            "task_type": "verify",
            "dependencies": ["EXTRACT_CODE", "CREATE_TESTS"],
            "affected_classes": ["com.example.monolith.GreetingService"],
            "expected_outputs": ["Verification report"],
            "acceptance_criteria": ["Verification passes with no unexplained regressions."],
            "risk_level": "medium",
            "requires_human_review": False,
        },
    ]
    return {
        "repository": str(SAMPLE_ROOT),
        "target_architecture": "Independently deployable Spring Boot Greeting service.",
        "candidate_service": "Greeting Service",
        "phases": [
            {
                "phase_id": "DISCOVERY",
                "title": "Discovery",
                "description": "Confirm the boundary and its dependencies.",
                "step_ids": ["VERIFY_BOUNDARY", "ANALYZE_DEPENDENCIES"],
            },
            {
                "phase_id": "EXTRACTION",
                "title": "Extraction",
                "description": "Prepare the target service code changes.",
                "step_ids": ["EXTRACT_CODE"],
            },
            {
                "phase_id": "VALIDATION",
                "title": "Validation",
                "description": "Test and verify the migration.",
                "step_ids": ["CREATE_TESTS", "VERIFY"],
            },
        ],
        "steps": steps,
        "risks": [
            {
                "description": "Database ownership is not evidenced by the static graph.",
                "risk_level": "medium",
                "affected_steps": ["EXTRACT_CODE"],
            }
        ],
        "assumptions": ["Database separation requires a later evidence-backed decision."],
        "human_review_points": ["Approve the high-risk extraction boundary before code changes."],
    }


class FakeRouter:
    """Scripted router that never contacts a real provider."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            provider="fake",
            model="fake-reasoning",
            content=self.contents.pop(0),
            latency_ms=1.0,
        )


def make_task() -> Task:
    """Create a ready migration-planning task."""
    return Task(
        project_id=PROJECT_ID,
        task_type=TaskType.MIGRATION_PLANNING,
        title="Plan Greeting migration",
        description="Create a grounded migration plan.",
        status=TaskStatus.READY,
        assigned_agent=MigrationPlanningAgent.name,
    )


def run_agent(root: Path, router: FakeRouter) -> dict[str, Any]:
    """Execute the planner against copied sample evidence."""
    task = make_task()
    context = AgentContext(
        project_id=PROJECT_ID,
        task=task,
        workspace_path=str(root),
        metadata={"candidate_service": "Greeting"},
    )
    result = MigrationPlanningAgent(router).execute(task, context)
    return result.metadata


def test_loading_service_boundary_artifact_and_alias_selection(tmp_path: Path) -> None:
    """The planner loads the boundary artifact and accepts the sample's short alias."""
    copy_evidence(tmp_path)
    evidence = MigrationPlanningAgent(FakeRouter([])).prepare_evidence(tmp_path, "Greeting")

    assert evidence["selected_service"]["name"] == "Greeting Service"


def test_selected_service_validation(tmp_path: Path) -> None:
    """Unknown candidate services are rejected before any model request."""
    copy_evidence(tmp_path)
    with pytest.raises(ValueError, match="not present"):
        MigrationPlanningAgent(FakeRouter([])).prepare_evidence(tmp_path, "Unknown")


def test_architecture_evidence_is_compact_and_grounded(tmp_path: Path) -> None:
    """Evidence contains architecture classes and graph edges, not Java source."""
    copy_evidence(tmp_path)
    evidence = MigrationPlanningAgent(FakeRouter([])).prepare_evidence(
        tmp_path, "Greeting Service"
    )

    assert evidence["architecture"]["classes"]
    assert evidence["dependencies"]
    assert "public class" not in json.dumps(evidence)


def test_reasoning_capability_request(tmp_path: Path) -> None:
    """Planning requests use the provider-neutral reasoning capability."""
    copy_evidence(tmp_path)
    router = FakeRouter([json.dumps(valid_output())])
    run_agent(tmp_path, router)

    assert router.requests[0].capability is ModelCapability.REASONING
    assert router.requests[0].max_tokens == 3200


def test_valid_plan_and_task_metadata(tmp_path: Path) -> None:
    """A valid response becomes a structured plan and pending tasks."""
    copy_evidence(tmp_path)
    metadata = run_agent(tmp_path, FakeRouter([json.dumps(valid_output())]))
    plan = MigrationPlan.model_validate(metadata["migration_plan"])

    assert plan.candidate_service == "Greeting Service"
    assert plan.estimated_task_count == 5
    assert len(metadata["tasks"]) == 5


def test_malformed_response_gets_one_repair_attempt(tmp_path: Path) -> None:
    """Malformed output is repaired once and then accepted."""
    copy_evidence(tmp_path)
    router = FakeRouter(["not json", json.dumps(valid_output())])

    run_agent(tmp_path, router)

    assert len(router.requests) == 2
    assert "Repair the following invalid migration plan" in router.requests[1].messages[-1].content


def test_second_invalid_response_is_rejected(tmp_path: Path) -> None:
    """The planner never retries indefinitely."""
    copy_evidence(tmp_path)
    router = FakeRouter(["bad", "still bad"])

    with pytest.raises(MigrationPlanResponseError, match="one repair attempt"):
        run_agent(tmp_path, router)
    assert len(router.requests) == 2


def mutate_output(**changes: object) -> dict[str, object]:
    """Deep-copy the valid response before applying a test mutation."""
    output = json.loads(json.dumps(valid_output()))
    output.update(changes)
    return cast(dict[str, object], output)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"steps": [{**cast(list[dict[str, object]], valid_output()["steps"])[0],
                         "affected_classes": ["com.example.Missing"]}] +
                      cast(list[dict[str, object]], valid_output()["steps"])[1:]},
            "Unknown affected class",
        ),
        (
            {"steps": [
                {**cast(list[dict[str, object]], valid_output()["steps"])[0], "step_id": "VERIFY"},
                *cast(list[dict[str, object]], valid_output()["steps"])[1:],
            ]},
            "step IDs must be unique",
        ),
        (
            {"steps": [{**cast(list[dict[str, object]], valid_output()["steps"])[0],
                         "dependencies": ["MISSING"]}] +
                      cast(list[dict[str, object]], valid_output()["steps"])[1:]},
            "unknown dependencies",
        ),
        (
            {"steps": [{**cast(list[dict[str, object]], valid_output()["steps"])[0],
                         "dependencies": ["VERIFY"]}] +
                      cast(list[dict[str, object]], valid_output()["steps"])[1:]},
            "dependency graph is invalid",
        ),
    ],
)
def test_grounding_and_dag_rejections(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    """Hallucinated classes, duplicate IDs, unknown dependencies, and cycles fail."""
    copy_evidence(tmp_path)
    content = json.dumps(mutate_output(**changes))
    router = FakeRouter([content, content])

    with pytest.raises(MigrationPlanResponseError, match="one repair attempt") as error:
        run_agent(tmp_path, router)
    assert message in str(error.value.__cause__)


def test_missing_verification_step_is_rejected(tmp_path: Path) -> None:
    """Every plan must include explicit verification."""
    copy_evidence(tmp_path)
    output = valid_output()
    steps = cast(list[dict[str, object]], output["steps"])[:-1]
    phases = cast(list[dict[str, object]], output["phases"])
    phases[-1]["step_ids"] = ["CREATE_TESTS"]
    output["steps"] = steps
    router = FakeRouter([json.dumps(output), json.dumps(output)])

    with pytest.raises(MigrationPlanResponseError, match="one repair attempt"):
        run_agent(tmp_path, router)


def test_code_changes_require_testing(tmp_path: Path) -> None:
    """Code-changing plans cannot omit a testing task."""
    copy_evidence(tmp_path)
    output = valid_output()
    steps = cast(list[dict[str, object]], output["steps"])
    steps[3]["task_type"] = "code_refactor"
    steps[2]["task_type"] = "architecture_analysis"
    router = FakeRouter([json.dumps(output), json.dumps(output)])

    with pytest.raises(MigrationPlanResponseError, match="one repair attempt"):
        run_agent(tmp_path, router)


def test_high_risk_step_requires_human_review(tmp_path: Path) -> None:
    """High-risk steps must explicitly require review."""
    copy_evidence(tmp_path)
    output = valid_output()
    steps = cast(list[dict[str, object]], output["steps"])
    steps[2]["requires_human_review"] = False
    router = FakeRouter([json.dumps(output), json.dumps(output)])

    with pytest.raises(MigrationPlanResponseError, match="one repair attempt"):
        run_agent(tmp_path, router)


def test_plan_repair_prompt_repeats_high_risk_review_invariant() -> None:
    """Repair context keeps the safety invariant explicit and bounded."""
    request = ModelRequest(
        messages=[ModelMessage(role=ModelRole.SYSTEM, content="system")],
        capability=ModelCapability.REASONING,
    )

    repaired = MigrationPlanningAgent._repair_request(
        request,
        "{}",
        validation_error="high-risk review is missing",
    )

    prompt = repaired.messages[-1].content
    assert 'risk_level is "high"' in prompt
    assert "requires_human_review" in prompt
    assert "human_review_points" in prompt
    assert "top-secret" not in prompt


@pytest.mark.parametrize("field", ["expected_outputs", "acceptance_criteria"])
def test_step_outputs_and_acceptance_criteria_are_nonempty(field: str) -> None:
    """Step-level output and acceptance fields reject empty lists."""
    step = cast(list[dict[str, object]], valid_output()["steps"])[0]
    step[field] = []

    with pytest.raises(ValueError):
        MigrationStep.model_validate(step)


def test_to_tasks_preserves_dependencies_and_pending_state(tmp_path: Path) -> None:
    """Plan conversion preserves edges and starts every task in PENDING."""
    copy_evidence(tmp_path)
    metadata = run_agent(tmp_path, FakeRouter([json.dumps(valid_output())]))
    plan = MigrationPlan.model_validate(metadata["migration_plan"])
    tasks = MigrationPlanningAgent.to_tasks(plan)
    by_title = {task.title: task for task in tasks}

    assert all(task.status is TaskStatus.PENDING for task in tasks)
    assert by_title["Verify behavior"].dependencies == [
        by_title["Extract domain code"].id,
        by_title["Create migration tests"].id,
    ]


def test_plan_tasks_are_compatible_with_graph_and_scheduler(tmp_path: Path) -> None:
    """Converted tasks can be scheduled by the existing in-memory DAG."""
    copy_evidence(tmp_path)
    metadata = run_agent(tmp_path, FakeRouter([json.dumps(valid_output())]))
    tasks = MigrationPlanningAgent.to_tasks(
        MigrationPlan.model_validate(metadata["migration_plan"])
    )
    graph = TaskGraph(tasks)
    ready = TaskScheduler(graph).schedule()

    assert [task.title for task in ready] == ["Verify selected boundary"]
    assert all(task.status is TaskStatus.READY for task in ready)


def test_artifacts_are_created(tmp_path: Path) -> None:
    """The planner writes both requested artifact formats."""
    copy_evidence(tmp_path)
    run_agent(tmp_path, FakeRouter([json.dumps(valid_output())]))

    assert (tmp_path / MIGRATION_PLAN_JSON_ARTIFACT).is_file()
    markdown = (tmp_path / MIGRATION_PLAN_MARKDOWN_ARTIFACT).read_text(encoding="utf-8")
    assert "# Migration Plan" in markdown
    assert "Acceptance criteria" in markdown
    assert "Human review points" in markdown


def test_dry_run_prepares_evidence_without_model_call(tmp_path: Path) -> None:
    """Dry-run evidence preparation does not invoke the router."""
    copy_evidence(tmp_path)
    router = FakeRouter([])
    evidence = MigrationPlanningAgent(router).prepare_evidence(
        tmp_path, "Greeting", allow_model=False
    )

    assert evidence["selected_service"]["name"] == "Greeting Service"
    assert router.requests == []


def test_cli_dry_run(tmp_path: Path) -> None:
    """The CLI dry-run reports reasoning input size and no external call."""
    copy_evidence(tmp_path)
    result = RUNNER.invoke(
        app, ["plan-migration", str(tmp_path), "--service", "Greeting", "--dry-run"]
    )

    assert result.exit_code == 0
    assert "Capability: reasoning" in result.stdout
    assert "Planned model call: yes" in result.stdout
    assert "External model call: no" in result.stdout


def test_cli_successful_mocked_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI renders a successful plan without contacting Groq."""
    copy_evidence(tmp_path)
    router = FakeRouter([json.dumps(valid_output())])
    from migrationswarm.cli import main as cli_main

    monkeypatch.setattr(cli_main, "MigrationPlanningAgent", lambda: MigrationPlanningAgent(router))
    result = RUNNER.invoke(
        app, ["plan-migration", str(tmp_path), "--service", "Greeting", "--show-dag"]
    )

    assert result.exit_code == 0
    assert "Migration Plan" in result.stdout
    assert "VERIFY_BOUNDARY" in result.stdout
    assert "-> ANALYZE_DEPENDENCIES" in result.stdout


def test_text_dag_rendering(tmp_path: Path) -> None:
    """The DAG renderer derives edges from the validated plan."""
    copy_evidence(tmp_path)
    metadata = run_agent(tmp_path, FakeRouter([json.dumps(valid_output())]))
    plan = MigrationPlan.model_validate(metadata["migration_plan"])
    dag = MigrationPlanningAgent.render_task_dag(plan)

    assert "VERIFY_BOUNDARY" in dag
    assert "EXTRACT_CODE" in dag
    assert "    -> VERIFY" in dag


class NamedFakeProvider(ModelProvider):
    """Fake provider used to verify routing without SiliconFlow credentials."""

    name: ClassVar[str] = "groq"

    def __init__(self, available: bool) -> None:
        self.available = available
        self.calls = 0

    @property
    def credentials_available(self) -> bool:
        return self.available

    def generate(self, request: ModelRequest, model: str) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            provider=self.name,
            model=model,
            content=json.dumps(valid_output()),
        )


class SiliconFlowFakeProvider(NamedFakeProvider):
    """Unavailable fallback provider."""

    name = "siliconflow"


def test_siliconflow_unavailable_does_not_block_groq_routing() -> None:
    """A reasoning-capable Groq route works without SiliconFlow credentials."""
    groq = NamedFakeProvider(True)
    siliconflow = SiliconFlowFakeProvider(False)
    router = ModelRouter(
        ModelRegistry(
            [
                ModelDefinition(
                    logical_name="groq-reasoning",
                    provider="groq",
                    provider_model_id="openai/gpt-oss-120b",
                    capabilities=frozenset({ModelCapability.REASONING}),
                    priority=1,
                ),
                ModelDefinition(
                    logical_name="siliconflow-reasoning",
                    provider="siliconflow",
                    provider_model_id="unused",
                    capabilities=frozenset({ModelCapability.REASONING}),
                    priority=2,
                ),
            ]
        ),
        ProviderRegistry([groq, siliconflow]),
        max_attempts_per_model=1,
    )
    response = router.generate(
        ModelRequest(
            messages=[ModelMessage(role=ModelRole.USER, content="plan")],
            capability=ModelCapability.REASONING,
        )
    )

    assert response.provider == "groq"
    assert groq.calls == 1
    assert siliconflow.calls == 0
