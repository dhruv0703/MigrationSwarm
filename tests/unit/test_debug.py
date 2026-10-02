"""Tests for bounded service-worktree debugging."""

import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from migrationswarm.agents.debug import (
    DEBUG_RESULTS_DIR,
    DebugAgent,
    DebugLimits,
    DebugResponseError,
    DebugSafetyError,
    DebugWorkspaceError,
)
from migrationswarm.core.agents import AgentContext
from migrationswarm.core.models import ModelCapability
from migrationswarm.core.orchestrator import FailureCategory, FailureClassifier
from migrationswarm.core.tasks import TaskStatus
from tests.unit import test_service_extraction as extraction_module
from tests.unit.test_service_extraction import FakeRouter

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")


@pytest.fixture
def extraction_fixture(tmp_path: Path) -> tuple[Path, Any, Path]:
    """Reuse the established managed-worktree fixture without duplicating setup."""
    factory = extraction_module.extraction_task.__wrapped__  # type: ignore[attr-defined]
    return cast(tuple[Path, Any, Path], factory(tmp_path))


def debug_json(path: str = "services/greeting-service/src/main/java/GreetingService.java") -> str:
    """Return a small complete-file repair proposal."""
    return json.dumps(
        {
            "diagnosis": {
                "likely_cause": "The generated service class has a missing method body.",
                "evidence": ["Compilation output names the generated service class."],
                "proposed_fix_summary": "Restore the minimal method body.",
                "confidence": 0.9,
            },
            "changes": [
                {
                    "path": path,
                    "change_type": "modify",
                    "complete_new_content": "public class GreetingService {\n}\n",
                    "reasoning": "Keep the repair limited to the failing generated class.",
                }
            ],
            "warnings": [],
        }
    )


def debug_context(task: Any, workspace: Path, **metadata: Any) -> AgentContext:
    values: dict[str, Any] = {
        "debug_attempt": 1,
        "target_service_directory": "services/greeting-service",
        "failure_evidence": {
            "build_status": "failed",
            "stderr_summary": "COMPILATION ERROR: cannot find symbol",
        },
        "relevant_files": [
            "services/greeting-service/src/main/java/GreetingService.java",
        ],
    }
    values.update(metadata)
    return AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(workspace),
        metadata=values,
    )


def prepare_service(workspace: Path) -> Path:
    service = workspace / "services" / "greeting-service"
    source = service / "src" / "main" / "java" / "GreetingService.java"
    source.parent.mkdir(parents=True)
    source.write_text("public class GreetingService {\n  broken();\n}\n", encoding="utf-8")
    return source


def test_debug_context_uses_coding_and_applies_atomic_repair(
    extraction_fixture: tuple[Path, Any, Path],
) -> None:
    root, task, workspace = extraction_fixture
    source = prepare_service(workspace)
    router = FakeRouter([debug_json()])
    result = DebugAgent(router).execute(task, debug_context(task, workspace))

    assert router.requests[0].capability is ModelCapability.CODING
    assert "SampleApplication" not in router.requests[0].messages[1].content
    assert source.read_text(encoding="utf-8") == "public class GreetingService {\n}\n"
    artifact = root / result.artifacts[0]
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert artifact == root / DEBUG_RESULTS_DIR / str(task.id) / "attempt-1.json"
    assert payload["attempt"] == 1
    assert payload["changed_files"] == [
        "services/greeting-service/src/main/java/GreetingService.java"
    ]
    assert "complete_new_content" not in artifact.read_text(encoding="utf-8")
    assert task.status is TaskStatus.READY


def test_debug_defaults_are_bounded_and_safe(
    extraction_fixture: tuple[Path, Any, Path],
) -> None:
    _, task, workspace = extraction_fixture
    assert DebugLimits().max_changed_files == 5
    prepare_service(workspace)
    with pytest.raises(DebugSafetyError, match="outside extracted service"):
        DebugAgent(FakeRouter([debug_json("src/main/java/SampleApplication.java")])).execute(
            task, debug_context(task, workspace, relevant_files=[])
        )


def test_debug_rejects_missing_main_and_external_workspaces(
    extraction_fixture: tuple[Path, Any, Path],
) -> None:
    root, task, workspace = extraction_fixture
    agent = DebugAgent(FakeRouter([]))
    with pytest.raises(DebugWorkspaceError):
        agent.execute(task, debug_context(task, root))
    outside = root.parent / "outside-debug"
    outside.mkdir()
    with pytest.raises(DebugWorkspaceError):
        agent.execute(task, debug_context(task, outside))
    with pytest.raises(DebugSafetyError):
        agent.execute(task, debug_context(task, workspace, debug_attempt=0))


def test_debug_rejects_traversal_delete_and_oversized_proposals(
    extraction_fixture: tuple[Path, Any, Path],
) -> None:
    _, task, workspace = extraction_fixture
    prepare_service(workspace)
    delete = json.loads(debug_json())
    delete["changes"][0]["change_type"] = "delete"
    with pytest.raises(DebugResponseError):
        DebugAgent(FakeRouter([json.dumps(delete), json.dumps(delete)])).execute(
            task, debug_context(task, workspace)
        )
    traversal = json.loads(debug_json("services/greeting-service/../../bad.java"))
    with pytest.raises(DebugSafetyError):
        DebugAgent(FakeRouter([json.dumps(traversal)])).execute(
            task, debug_context(task, workspace)
        )
    huge = json.loads(debug_json())
    huge["changes"][0]["complete_new_content"] = "x" * 1_200
    with pytest.raises(DebugSafetyError):
        DebugAgent(
            FakeRouter([json.dumps(huge)]),
            limits=DebugLimits(max_generated_bytes=1_000, max_context_bytes=60_000),
        ).execute(task, debug_context(task, workspace))


@pytest.mark.parametrize(
    ("text", "category", "eligible"),
    [
        ("COMPILATION ERROR cannot find symbol", FailureCategory.COMPILATION, True),
        ("Tests run: 2, Failures: 1, Errors: 0", FailureCategory.TEST_FAILURE, True),
        (
            "application.yml has invalid configuration",
            FailureCategory.GENERATED_CONFIGURATION,
            True,
        ),
        ("mvn not found", FailureCategory.TOOLCHAIN, False),
        ("Connection refused while downloading", FailureCategory.NETWORK, False),
        ("Verification timed out", FailureCategory.TIMEOUT, False),
        ("Permission denied", FailureCategory.PERMISSION, False),
        ("unclassified failure", FailureCategory.UNKNOWN, False),
    ],
)
def test_failure_classifier_is_deterministic(
    text: str, category: FailureCategory, eligible: bool
) -> None:
    from uuid import uuid4

    from migrationswarm.agents.build_verification import (
        BuildCommandResult,
        BuildSystem,
        BuildVerificationResult,
        VerificationStatus,
    )
    from migrationswarm.core.verification import VerificationEvidence

    task_id = uuid4()
    result = BuildVerificationResult(
        task_id=task_id,
        workspace_path="service",
        build_system=BuildSystem.MAVEN,
        commands_run=[
            BuildCommandResult(
                command=["mvn", "test"],
                status=VerificationStatus.FAILED,
                duration_ms=1,
            )
        ],
        status=VerificationStatus.FAILED,
        duration_ms=1,
        stdout_summary=text,
    )
    evidence = VerificationEvidence.from_build_result(result, [])
    decision = FailureClassifier().classify(result, evidence)
    assert decision.category is category
    assert decision.eligible is eligible
