"""End-to-end tests for the deterministic local demonstration."""

import hashlib
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

from migrationswarm.agents.service_boundary import CandidateService, ServiceBoundaryReport
from migrationswarm.cli.main import app
from migrationswarm.core.models import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
)
from migrationswarm.demo import (
    DemoError,
    DemoResult,
    RecordingRouter,
    _initialize_git_repository,
    _select_demo_services,
    _set_demo_approvals,
    run_demo,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "examples" / "demo-commerce-monolith"
RUNNER = CliRunner()


def _copy_fixture(destination: Path) -> Path:
    shutil.copytree(
        FIXTURE,
        destination,
        ignore=shutil.ignore_patterns("target", ".migrationswarm", ".git"),
    )
    return destination


def _copy_named_fixture(name: str, destination: Path) -> Path:
    source = ROOT / "examples" / name
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("target", ".migrationswarm", ".git"),
    )
    return destination


def _source_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.java")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_demo_fixture_baseline_builds() -> None:
    if shutil.which("mvn") is None:
        pytest.skip("Maven is not installed")
    maven = shutil.which("mvn") or "mvn"
    command = [maven, "test", "-q"]
    if maven.lower().endswith((".cmd", ".bat")):
        command = ["cmd.exe", "/d", "/s", "/c", *command]
    completed = subprocess.run(
        command,
        cwd=FIXTURE,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_live_candidate_names_are_preserved_for_demo_domains() -> None:
    report = ServiceBoundaryReport(
        candidate_services=[
            CandidateService(
                name="Inventory Service",
                description="inventory",
                classes=["example.Inventory"],
                packages=[],
                controllers=[],
                services=[],
                repositories=[],
                confidence=0.8,
                reasoning="grounded",
                dependencies_on_other_candidates=[],
                risks=[],
            ),
            CandidateService(
                name="Order Service",
                description="orders",
                classes=["example.Order"],
                packages=[],
                controllers=[],
                services=[],
                repositories=[],
                confidence=0.8,
                reasoning="grounded",
                dependencies_on_other_candidates=[],
                risks=[],
            ),
            CandidateService(
                name="Notification Service",
                description="notifications",
                classes=["example.Notification"],
                packages=[],
                controllers=[],
                services=[],
                repositories=[],
                confidence=0.8,
                reasoning="grounded",
                dependencies_on_other_candidates=[],
                risks=[],
            ),
        ],
        shared_components=[],
        unresolved_classes=[],
        overall_reasoning="grounded",
        warnings=[],
        model_provider="test",
        model_name="test",
    )

    assert _select_demo_services(report) == [
        "Inventory Service",
        "Order Service",
        "Notification Service",
    ]


def test_generic_demo_accepts_arbitrary_validated_candidates() -> None:
    report = ServiceBoundaryReport(
        candidate_services=[
            CandidateService(
                name=name,
                description=f"{name} domain",
                classes=[f"example.{name}"],
                packages=[],
                controllers=[],
                services=[],
                repositories=[],
                confidence=0.8,
                reasoning="grounded",
                dependencies_on_other_candidates=[],
                risks=[],
            )
            for name in ("Vet", "Owner", "System")
        ],
        shared_components=[],
        unresolved_classes=[],
        overall_reasoning="grounded",
        warnings=[],
        model_provider="test",
        model_name="test",
    )

    assert _select_demo_services(report, ()) == ["Vet", "Owner", "System"]


def test_fixture_selection_remains_strict_for_expected_domains() -> None:
    report = ServiceBoundaryReport(
        candidate_services=[
            CandidateService(
                name="Inventory",
                description="inventory",
                classes=["example.Inventory"],
                packages=[],
                controllers=[],
                services=[],
                repositories=[],
                confidence=0.8,
                reasoning="grounded",
                dependencies_on_other_candidates=[],
                risks=[],
            )
        ],
        shared_components=[],
        unresolved_classes=[],
        overall_reasoning="grounded",
        warnings=[],
        model_provider="test",
        model_name="test",
    )

    with pytest.raises(DemoError, match="required fixture domains"):
        _select_demo_services(report, ("Inventory", "Orders", "Notifications"))


def test_demo_approvals_replace_stale_candidate_names(tmp_path: Path) -> None:
    approvals = tmp_path / ".migrationswarm" / "service-approvals.json"
    approvals.parent.mkdir()
    approvals.write_text(
        '{"approved": ["Inventory", "Notifications", "Orders"]}\n',
        encoding="utf-8",
    )

    _set_demo_approvals(
        tmp_path,
        ["Inventory Service", "Order Service", "Notification Service"],
    )

    assert approvals.read_text(encoding="utf-8") == (
        '{\n'
        '  "approved": [\n'
        '    "Inventory Service",\n'
        '    "Notification Service",\n'
        '    "Order Service"\n'
        '  ]\n'
        '}\n'
    )


def test_demo_staging_ignores_runtime_metadata_with_existing_gitignore(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / ".gitignore").write_text("target/\n", encoding="utf-8")

    _initialize_git_repository(staging)
    (staging / ".migrationswarm" / "runtime.json").parent.mkdir()
    (staging / ".migrationswarm" / "runtime.json").write_text("{}\n", encoding="utf-8")

    assert ".migrationswarm/" in (staging / ".gitignore").read_text(encoding="utf-8")
    assert "!! .migrationswarm/" in subprocess.run(
        ["git", "status", "--porcelain=v1", "--ignored=matching"],
        cwd=staging,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_recording_router_paces_live_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRouter:
        def generate(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(provider="fake", model="fake", content="ok")

    clock = iter([0.0, 0.5, 1.0])
    sleeps: list[float] = []
    monkeypatch.setattr("migrationswarm.demo.monotonic", lambda: next(clock))
    monkeypatch.setattr("migrationswarm.demo.sleep", sleeps.append)

    from migrationswarm.core.observability import MetricsCollector

    router = RecordingRouter(
        FakeRouter(), MetricsCollector(), min_interval_seconds=1.0
    )
    request = ModelRequest(
        messages=[ModelMessage(role=ModelRole.USER, content="test")],
        capability=ModelCapability.REASONING,
    )

    router.generate(request)
    router.generate(request)

    assert sleeps == [0.5]


def test_offline_demo_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("mvn") is None or shutil.which("git") is None:
        pytest.skip("The offline demo requires Maven and Git")

    def unexpected_provider_access(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("offline demo must not inspect provider configuration")

    monkeypatch.setattr("migrationswarm.demo.default_model_registry", unexpected_provider_access)
    monkeypatch.setattr("migrationswarm.demo.default_provider_registry", unexpected_provider_access)
    repository = _copy_fixture(tmp_path / "demo-commerce-monolith")

    result = run_demo(repository, live=False, service_workers=2)

    assert result.status == "completed"
    assert result.offline is True
    assert result.main_repository_modified is False
    assert result.model_calls > 0
    assert len(result.generated_service_directories) == 4
    for relative in result.generated_service_directories:
        assert (repository / relative / "pom.xml").is_file()
    metrics = repository / result.metrics_path
    benchmark = repository / result.benchmark_path
    assert metrics.is_file()
    assert benchmark.is_file()
    assert "api_key" not in metrics.read_text(encoding="utf-8").lower()
    assert "prompt" not in benchmark.read_text(encoding="utf-8").lower()
    assert (repository / ".migrationswarm" / "multi-runs").is_dir()
    assert (repository / ".migrationswarm" / "verification-results").is_dir()


@pytest.mark.parametrize(
    "fixture_name",
    ["demo-support-monolith", "demo-booking-monolith"],
)
def test_offline_demo_extracts_all_metadata_backed_fixtures(
    fixture_name: str, tmp_path: Path
) -> None:
    if shutil.which("mvn") is None or shutil.which("git") is None:
        pytest.skip("The offline demo requires Maven and Git")
    repository = _copy_named_fixture(fixture_name, tmp_path / fixture_name)
    before = _source_hash(repository)

    result = run_demo(repository, live=False, service_workers=2)

    assert result.status == "completed"
    assert result.main_repository_modified is False
    assert len(result.generated_service_directories) == 3
    assert _source_hash(repository) == before
    for relative in result.generated_service_directories:
        service = repository / relative
        assert (service / "pom.xml").is_file()
        assert list(service.rglob("*.java"))


def test_demo_cli_rejects_conflicting_modes() -> None:
    result = RUNNER.invoke(
        app,
        ["demo", "examples/demo-commerce-monolith", "--offline", "--live"],
    )

    assert result.exit_code == 2
    assert "exactly one" in result.output.lower()


def test_demo_cli_prints_secret_free_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = DemoResult(
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        status="completed",
        offline=True,
        metrics_path=".migrationswarm/metrics/run.json",
        benchmark_path=".migrationswarm/reports/run-benchmark.json",
        generated_service_directories=[".migrationswarm/demo-services/run/inventory"],
    )
    monkeypatch.setattr("migrationswarm.cli.main.run_demo", lambda path, live: expected)

    result = RUNNER.invoke(app, ["demo", str(tmp_path), "--offline"])

    assert result.exit_code == 0
    assert "completed" in result.output
    assert "api_key" not in result.output.lower()


def test_benchmark_cli_reports_missing_data(tmp_path: Path) -> None:
    result = RUNNER.invoke(app, ["benchmark", str(tmp_path)])

    assert result.exit_code == 1
    assert "no benchmark reports" in result.output.lower()
