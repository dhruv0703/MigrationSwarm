"""Unit tests for deterministic architecture analysis."""

import json
from pathlib import Path
from uuid import UUID

import networkx as nx
from typer.testing import CliRunner

from migrationswarm.agents.architecture_analysis import (
    ARCHITECTURE_REPORT_ARTIFACT,
    HIGH_FAN_IN_THRESHOLD,
    HIGH_FAN_OUT_THRESHOLD,
    JAVA_DEPENDENCY_DOT_ARTIFACT,
    ArchitectureAnalysisAgent,
    ArchitectureRiskType,
)
from migrationswarm.agents.dependency_analysis import (
    JAVA_DEPENDENCY_ARTIFACT,
    JavaClass,
    JavaClassRole,
    JavaDependency,
    JavaDependencyGraph,
    JavaDependencyRelationship,
)
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext, AgentResult
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

PROJECT_ID = UUID(int=4000)
runner = CliRunner()


def write_file(root: Path, relative_path: str, content: str) -> Path:
    """Write a UTF-8 fixture file."""
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def java_class(fqn: str, role: JavaClassRole = JavaClassRole.OTHER) -> JavaClass:
    """Create a compact Java class fixture."""
    package, name = fqn.rsplit(".", 1)
    return JavaClass(
        name=name,
        fully_qualified_name=fqn,
        package=package,
        file_path=f"src/main/java/{fqn.replace('.', '/')}.java",
        role=role,
    )


def dependency(
    source: str,
    target: str,
    relationship: JavaDependencyRelationship = JavaDependencyRelationship.FIELD_DEPENDENCY,
) -> JavaDependency:
    """Create a dependency fixture."""
    return JavaDependency(
        source=source,
        target=target,
        relationship=relationship,
        evidence=f"{relationship.value.lower()}: fixture",
    )


def fixture_graph() -> JavaDependencyGraph:
    """Build a graph with branches, a cycle, package coupling, and an isolate."""
    classes = [
        java_class("web.Controller", JavaClassRole.CONTROLLER),
        java_class("app.Service", JavaClassRole.SERVICE),
        java_class("data.Repository", JavaClassRole.REPOSITORY),
        java_class("app.ConsumerOne"),
        java_class("app.ConsumerTwo"),
        java_class("app.ConsumerThree"),
        java_class("app.Hub"),
        java_class("app.Helper"),
        java_class("target.One"),
        java_class("target.Two"),
        java_class("target.Three"),
        java_class("isolated.Lonely"),
    ]
    dependencies = [
        dependency(
            "web.Controller", "app.Service", JavaDependencyRelationship.CONSTRUCTOR_DEPENDENCY
        ),
        dependency("app.Service", "data.Repository"),
        dependency("data.Repository", "app.Service"),
        dependency(
            "app.Service", "app.Helper", JavaDependencyRelationship.METHOD_RETURN_DEPENDENCY
        ),
        dependency("app.ConsumerOne", "app.Service"),
        dependency("app.ConsumerTwo", "app.Service"),
        dependency("app.ConsumerThree", "app.Service"),
        dependency("app.Hub", "target.One"),
        dependency("app.Hub", "target.Two"),
        dependency("app.Hub", "target.Three"),
    ]
    return JavaDependencyGraph(
        repository_root="C:/fixture",
        total_java_classes=len(classes),
        total_dependency_edges=len(dependencies),
        classes=classes,
        dependencies=dependencies,
    )


def run_agent(root: Path) -> AgentResult:
    """Execute the architecture agent against a temporary workspace."""
    task = Task(
        project_id=PROJECT_ID,
        task_type=TaskType.ARCHITECTURE_ANALYSIS,
        title="Architecture analysis",
        description="Analyze architecture",
        status=TaskStatus.READY,
        assigned_agent=ArchitectureAnalysisAgent.name,
    )
    context = AgentContext(project_id=PROJECT_ID, task=task, workspace_path=str(root))
    return ArchitectureAnalysisAgent().execute(task, context)


def test_networkx_graph_construction_and_edge_metadata() -> None:
    """The directed multigraph preserves nodes and relationship metadata."""
    graph = ArchitectureAnalysisAgent.build_graph(fixture_graph())

    assert isinstance(graph, nx.MultiDiGraph)
    assert graph.number_of_nodes() == 12
    assert graph.number_of_edges() == 10
    assert graph.nodes["app.Service"]["role"] == JavaClassRole.SERVICE
    assert graph["web.Controller"]["app.Service"][0]["relationship"] == (
        JavaDependencyRelationship.CONSTRUCTOR_DEPENDENCY
    )


def test_in_degree_out_degree_and_fan_metrics() -> None:
    """Class metrics expose deterministic in/out degree and fan-in/fan-out."""
    report, _ = ArchitectureAnalysisAgent().analyze_graph(fixture_graph())
    metrics = {metric.fully_qualified_name: metric for metric in report.class_metrics}

    assert metrics["app.Service"].in_degree == 5
    assert metrics["app.Service"].out_degree == 2
    assert metrics["app.Service"].fan_in == 5
    assert metrics["app.Service"].fan_out == 2
    assert metrics["app.Hub"].fan_out == 3
    assert metrics["isolated.Lonely"].fan_in == 0
    assert metrics["isolated.Lonely"].fan_out == 0


def test_weak_and_strong_components_and_cycle_detection() -> None:
    """Weak components, strong components, and the service/repository cycle are reported."""
    report, _ = ArchitectureAnalysisAgent().analyze_graph(fixture_graph())

    assert report.connected_component_count == 3
    assert report.strongly_connected_component_count == 11
    assert report.cycle_count == 1
    assert any(risk.risk_type is ArchitectureRiskType.CYCLIC_DEPENDENCY for risk in report.risks)


def test_self_cycle_is_detected_once() -> None:
    """A self-loop is one cycle and one cyclic risk."""
    graph = JavaDependencyGraph(
        repository_root="C:/fixture",
        total_java_classes=1,
        total_dependency_edges=1,
        classes=[java_class("cycle.Self")],
        dependencies=[dependency("cycle.Self", "cycle.Self")],
    )

    report, _ = ArchitectureAnalysisAgent().analyze_graph(graph)

    assert report.cycle_count == 1
    assert [risk.risk_type for risk in report.risks].count(
        ArchitectureRiskType.CYCLIC_DEPENDENCY
    ) == 1


def test_package_metrics_fan_in_fan_out_and_coupling() -> None:
    """Package metrics count internal edges separately from cross-package edges."""
    report, _ = ArchitectureAnalysisAgent().analyze_graph(fixture_graph())
    packages = {metric.package: metric for metric in report.package_metrics}

    assert packages["app"].fan_in == 2
    assert packages["app"].fan_out == 4
    assert packages["app"].internal_edge_count == 4
    assert packages["app"].external_edge_count == 6
    assert packages["target"].fan_in == 3
    assert packages["target"].fan_out == 0


def test_risks_include_fan_in_fan_out_cross_package_and_isolated() -> None:
    """Configured thresholds produce explainable risk indicators."""
    report, _ = ArchitectureAnalysisAgent().analyze_graph(fixture_graph())
    risk_types = {risk.risk_type for risk in report.risks}

    assert HIGH_FAN_IN_THRESHOLD == 3
    assert HIGH_FAN_OUT_THRESHOLD == 3
    assert ArchitectureRiskType.HIGH_FAN_IN in risk_types
    assert ArchitectureRiskType.HIGH_FAN_OUT in risk_types
    assert ArchitectureRiskType.CROSS_PACKAGE_COUPLING in risk_types
    assert ArchitectureRiskType.ISOLATED_CLASS in risk_types
    assert "isolated.Lonely" in report.isolated_classes
    assert "app.Hub" in report.high_coupling_classes


def test_role_relationships_and_candidate_components() -> None:
    """Role relationships and package-scoped candidate components are counted."""
    report, _ = ArchitectureAnalysisAgent().analyze_graph(fixture_graph())

    assert report.controller_service_relationships == 1
    assert report.service_repository_relationships == 1
    assert report.components
    app_component = next(
        component for component in report.components if "app.Service" in component.classes
    )
    assert app_component.controller_count == 0
    assert app_component.service_count == 1
    assert app_component.internal_edge_count == 4
    assert app_component.external_edge_count == 3


def test_existing_dependency_artifact_is_loaded(tmp_path: Path) -> None:
    """An existing dependency artifact is consumed without Java source parsing."""
    graph = fixture_graph()
    write_file(
        tmp_path,
        JAVA_DEPENDENCY_ARTIFACT,
        json.dumps(graph.model_dump(mode="json"), indent=2),
    )

    result = run_agent(tmp_path)

    assert result.success
    assert result.metadata["architecture_report"]["total_classes"] == 12
    assert result.metadata["architecture_report"]["warnings"] == []


def test_missing_dependency_artifact_falls_back_to_dependency_analysis(tmp_path: Path) -> None:
    """Missing graph artifacts trigger deterministic dependency analysis fallback."""
    write_file(
        tmp_path,
        "src/main/java/example/Service.java",
        "package example; public class Service {}",
    )

    result = run_agent(tmp_path)

    assert result.success
    assert result.metadata["architecture_report"]["total_classes"] == 1
    assert (tmp_path / ARCHITECTURE_REPORT_ARTIFACT).is_file()


def test_report_and_dot_artifacts_are_created(tmp_path: Path) -> None:
    """Execution writes the JSON report and valid basic DOT text."""
    write_file(
        tmp_path,
        JAVA_DEPENDENCY_ARTIFACT,
        json.dumps(fixture_graph().model_dump(mode="json")),
    )

    result = run_agent(tmp_path)
    report_path = tmp_path / ARCHITECTURE_REPORT_ARTIFACT
    dot_path = tmp_path / JAVA_DEPENDENCY_DOT_ARTIFACT

    assert result.artifacts == [ARCHITECTURE_REPORT_ARTIFACT, JAVA_DEPENDENCY_DOT_ARTIFACT]
    assert json.loads(report_path.read_text(encoding="utf-8"))["total_dependencies"] == 10
    dot = dot_path.read_text(encoding="utf-8")
    assert dot.startswith("digraph JavaDependencyGraph {")
    assert '"web.Controller" -> "app.Service"' in dot
    assert "CONSTRUCTOR_DEPENDENCY" in dot
    assert dot.endswith("}\n")


def test_agent_result_is_structured_and_deterministic(tmp_path: Path) -> None:
    """Repeated analysis serializes report and DOT output identically."""
    write_file(
        tmp_path,
        JAVA_DEPENDENCY_ARTIFACT,
        json.dumps(fixture_graph().model_dump(mode="json")),
    )

    first = run_agent(tmp_path)
    first_report = (tmp_path / ARCHITECTURE_REPORT_ARTIFACT).read_text(encoding="utf-8")
    first_dot = (tmp_path / JAVA_DEPENDENCY_DOT_ARTIFACT).read_text(encoding="utf-8")
    second = run_agent(tmp_path)

    assert first.agent_name == ArchitectureAnalysisAgent.name
    assert first.success
    assert first.started_at.tzinfo is not None
    assert first.completed_at >= first.started_at
    assert second.metadata["architecture_report"] == first.metadata["architecture_report"]
    assert (tmp_path / ARCHITECTURE_REPORT_ARTIFACT).read_text(encoding="utf-8") == first_report
    assert (tmp_path / JAVA_DEPENDENCY_DOT_ARTIFACT).read_text(encoding="utf-8") == first_dot


def test_cli_analyze_architecture_prints_summary_and_writes_artifacts(tmp_path: Path) -> None:
    """The architecture CLI command prints calculated values."""
    write_file(
        tmp_path,
        "src/main/java/example/Service.java",
        "package example; public class Service {}",
    )

    result = runner.invoke(app, ["analyze-architecture", str(tmp_path)])

    assert result.exit_code == 0
    assert "Architecture Analysis" in result.stdout
    assert "Candidate components" in result.stdout
    assert (tmp_path / ARCHITECTURE_REPORT_ARTIFACT).is_file()
    assert (tmp_path / JAVA_DEPENDENCY_DOT_ARTIFACT).is_file()
