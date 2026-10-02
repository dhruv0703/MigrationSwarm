"""Unit tests for deterministic Java dependency analysis."""

import json
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

from migrationswarm.agents.dependency_analysis import (
    JAVA_DEPENDENCY_ARTIFACT,
    DependencyAnalysisAgent,
    JavaClassRole,
)
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext, AgentResult
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

PROJECT_ID = UUID(int=3000)
runner = CliRunner()


def write_file(root: Path, relative_path: str, content: str) -> Path:
    """Write a UTF-8 fixture file."""
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def run_agent(root: Path, include_test_sources: bool = False) -> AgentResult:
    """Execute the dependency agent against a temporary workspace."""
    task = Task(
        project_id=PROJECT_ID,
        task_type=TaskType.DEPENDENCY_ANALYSIS,
        title="Dependency analysis",
        description="Analyze Java dependencies",
        status=TaskStatus.READY,
        assigned_agent=DependencyAnalysisAgent.name,
    )
    context = AgentContext(project_id=PROJECT_ID, task=task, workspace_path=str(root))
    return DependencyAnalysisAgent(include_test_sources=include_test_sources).execute(task, context)


def dependency_tuples(result: AgentResult) -> set[tuple[str, str, str]]:
    """Return compact dependency tuples from an agent result."""
    return {
        (item["source"], item["target"], item["relationship"])
        for item in result.metadata["dependency_graph"]["dependencies"]
    }


def test_class_metadata_and_spring_jpa_roles(tmp_path: Path) -> None:
    """Packages, names, imports, annotations, and common roles are extracted."""
    write_file(
        tmp_path,
        "src/main/java/example/Application.java",
        "package example; import org.springframework.boot.autoconfigure.SpringBootApplication; "
        "@SpringBootApplication public class Application {}",
    )
    write_file(
        tmp_path,
        "src/main/java/example/Customer.java",
        "package example; import jakarta.persistence.Entity; import jakarta.persistence.Table; "
        "@Entity @Table(name = \"customer\") public class Customer {}",
    )
    write_file(
        tmp_path,
        "src/main/java/example/Controller.java",
        "package example; import org.springframework.web.bind.annotation.RestController; "
        "@RestController public class Controller {}",
    )

    graph = run_agent(tmp_path).metadata["dependency_graph"]
    classes = {item["name"]: item for item in graph["classes"]}

    assert classes["Application"]["fully_qualified_name"] == "example.Application"
    assert classes["Application"]["package"] == "example"
    assert classes["Application"]["file_path"] == "src/main/java/example/Application.java"
    assert classes["Application"]["role"] == JavaClassRole.APPLICATION
    assert classes["Customer"]["role"] == JavaClassRole.ENTITY
    assert classes["Controller"]["role"] == JavaClassRole.CONTROLLER
    assert (
        "org.springframework.web.bind.annotation.RestController"
        in classes["Controller"]["imports"]
    )


def test_explicit_imports_same_package_and_relationships(tmp_path: Path) -> None:
    """Explicit imports and same-package types produce typed local edges."""
    write_file(
        tmp_path,
        "src/main/java/example/api/Api.java",
        "package example.api; public interface Api {}",
    )
    write_file(
        tmp_path,
        "src/main/java/example/model/Model.java",
        "package example.model; public class Model {}",
    )
    write_file(
        tmp_path,
        "src/main/java/example/api/Controller.java",
        """
        package example.api;
        import example.model.Model;
        public class Controller implements Api {
            private Model model;
            public Controller(Model model) { this.model = model; }
            public Model get(Model value) { return value; }
        }
        """,
    )

    result = run_agent(tmp_path)
    tuples = dependency_tuples(result)
    assert ("example.api.Controller", "example.model.Model", "IMPORTS") in tuples
    assert ("example.api.Controller", "example.model.Model", "FIELD_DEPENDENCY") in tuples
    assert ("example.api.Controller", "example.model.Model", "CONSTRUCTOR_DEPENDENCY") in tuples
    assert (
        "example.api.Controller",
        "example.model.Model",
        "METHOD_PARAMETER_DEPENDENCY",
    ) in tuples
    assert (
        "example.api.Controller",
        "example.model.Model",
        "METHOD_RETURN_DEPENDENCY",
    ) in tuples
    assert ("example.api.Controller", "example.api.Api", "IMPLEMENTS") in tuples


def test_extends_and_duplicate_relationship_types_are_preserved(tmp_path: Path) -> None:
    """A pair of classes can have multiple distinct relationship edges."""
    write_file(tmp_path, "src/main/java/example/Base.java", "package example; public class Base {}")
    write_file(
        tmp_path,
        "src/main/java/example/Child.java",
        "package example; public class Child extends Base { private Base base; "
        "public Child(Base base) { this.base = base; } }",
    )

    tuples = dependency_tuples(run_agent(tmp_path))
    assert ("example.Child", "example.Base", "EXTENDS") in tuples
    assert ("example.Child", "example.Base", "FIELD_DEPENDENCY") in tuples
    assert ("example.Child", "example.Base", "CONSTRUCTOR_DEPENDENCY") in tuples


def test_wildcard_import_is_resolved_when_unambiguous(tmp_path: Path) -> None:
    """A wildcard import resolves to its only local class."""
    write_file(
        tmp_path,
        "src/main/java/example/Helper.java",
        "package example; public class Helper {}",
    )
    write_file(
        tmp_path,
        "src/main/java/consumer/Consumer.java",
        "package consumer; import example.*; public class Consumer { private Helper helper; }",
    )

    tuples = dependency_tuples(run_agent(tmp_path))
    assert ("consumer.Consumer", "example.Helper", "IMPORTS") in tuples
    assert ("consumer.Consumer", "example.Helper", "FIELD_DEPENDENCY") in tuples


def test_external_jdk_and_unresolved_types_are_ignored(tmp_path: Path) -> None:
    """Only local classes become graph targets."""
    write_file(
        tmp_path,
        "src/main/java/example/UsesExternal.java",
        "package example; import java.util.List; import org.example.External; "
        "public class UsesExternal { private List<String> values; private External external; }",
    )

    graph = run_agent(tmp_path).metadata["dependency_graph"]
    assert graph["total_java_classes"] == 1
    assert graph["dependencies"] == []


def test_test_sources_are_excluded_by_default_and_can_be_included(tmp_path: Path) -> None:
    """Test source inclusion is deterministic and configurable."""
    write_file(tmp_path, "src/main/java/example/Main.java", "package example; public class Main {}")
    write_file(
        tmp_path,
        "src/test/java/example/TestOnly.java",
        "package example; public class TestOnly {}",
    )

    production_graph = run_agent(tmp_path).metadata["dependency_graph"]
    all_graph = run_agent(tmp_path, include_test_sources=True).metadata["dependency_graph"]
    assert production_graph["total_java_classes"] == 1
    assert all_graph["total_java_classes"] == 2


def test_malformed_java_file_becomes_warning_without_failing(tmp_path: Path) -> None:
    """Files without a supported type are reported as warnings."""
    write_file(
        tmp_path,
        "src/main/java/example/Broken.java",
        "package example; this is not a class;",
    )

    result = run_agent(tmp_path)
    assert result.success
    assert result.metadata["dependency_graph"]["warnings"]


def test_safe_defaults_and_artifact_match_result(tmp_path: Path) -> None:
    """The graph artifact is valid JSON and includes counts and warnings."""
    result = run_agent(tmp_path)
    artifact = tmp_path / JAVA_DEPENDENCY_ARTIFACT
    artifact_data = json.loads(artifact.read_text(encoding="utf-8"))

    assert result.artifacts == [JAVA_DEPENDENCY_ARTIFACT]
    assert artifact_data == result.metadata["dependency_graph"]
    assert artifact_data["total_java_classes"] == 0
    assert artifact_data["total_dependency_edges"] == 0
    assert artifact_data["classes"] == []
    assert artifact_data["dependencies"] == []


def test_missing_workspace_is_rejected(tmp_path: Path) -> None:
    """A missing workspace produces a clear domain error."""
    missing = tmp_path / "missing"
    task = Task(
        project_id=PROJECT_ID,
        task_type=TaskType.DEPENDENCY_ANALYSIS,
        title="Dependency analysis",
        description="Analyze",
    )

    with pytest.raises(ValueError, match="does not exist"):
        DependencyAnalysisAgent().execute(
            task,
            AgentContext(project_id=PROJECT_ID, task=task, workspace_path=str(missing)),
        )


def test_cli_analyze_dependencies_creates_graph(tmp_path: Path) -> None:
    """The dependency CLI command prints a calculated summary and writes its artifact."""
    write_file(tmp_path, "src/main/java/example/Main.java", "package example; public class Main {}")

    result = runner.invoke(app, ["analyze-dependencies", str(tmp_path)])

    assert result.exit_code == 0
    assert "Java Dependency Analysis" in result.stdout
    assert "Java classes" in result.stdout
    assert (tmp_path / JAVA_DEPENDENCY_ARTIFACT).is_file()


def test_sample_style_controller_service_repository_graph(tmp_path: Path) -> None:
    """The sample architecture produces controller-to-service-to-repository edges."""
    base = "package example;"
    write_file(
        tmp_path,
        "src/main/java/example/GreetingRepository.java",
        f"{base} @Repository public class GreetingRepository {{}}",
    )
    write_file(
        tmp_path,
        "src/main/java/example/GreetingService.java",
        f"{base} @Service public class GreetingService {{ private GreetingRepository repository; "
        "public GreetingService(GreetingRepository repository) {} }",
    )
    write_file(
        tmp_path,
        "src/main/java/example/GreetingController.java",
        f"{base} @RestController public class GreetingController {{ "
        "private GreetingService service; "
        "public GreetingController(GreetingService service) {} }",
    )

    graph = run_agent(tmp_path).metadata["dependency_graph"]
    roles = {item["fully_qualified_name"]: item["role"] for item in graph["classes"]}
    flows = {
        (roles[item["source"]], roles[item["target"]])
        for item in graph["dependencies"]
    }
    assert (JavaClassRole.CONTROLLER, JavaClassRole.SERVICE) in flows
    assert (JavaClassRole.SERVICE, JavaClassRole.REPOSITORY) in flows
