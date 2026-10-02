"""Unit tests for deterministic repository inventory analysis."""

import json
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

from migrationswarm.agents.repository_analysis import (
    INVENTORY_ARTIFACT,
    InvalidWorkspaceError,
    RepositoryAnalysisAgent,
)
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentCapability, AgentContext, AgentRegistry, AgentResult
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

PROJECT_ID = UUID(int=2000)
runner = CliRunner()


def write_file(root: Path, relative_path: str, content: str = "") -> Path:
    """Create a temporary repository file and return its path."""
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def run_agent(root: Path) -> AgentResult:
    """Execute the repository agent against a temporary workspace."""
    task = Task(
        project_id=PROJECT_ID,
        task_type=TaskType.REPOSITORY_ANALYSIS,
        title="Repository analysis",
        description="Inventory a repository",
        status=TaskStatus.READY,
        assigned_agent=RepositoryAnalysisAgent.name,
    )
    context = AgentContext(
        project_id=PROJECT_ID,
        task=task,
        workspace_path=str(root),
    )
    return RepositoryAnalysisAgent().execute(task, context)


def test_empty_repository(tmp_path: Path) -> None:
    """An empty workspace produces a valid empty inventory."""
    result = run_agent(tmp_path)
    inventory = result.metadata["inventory"]

    assert result.success
    assert inventory["total_file_count"] == 0
    assert inventory["total_directory_count"] == 0
    assert inventory["detected_languages"] == []


def test_simple_java_repository_detects_entry_point_and_roots(tmp_path: Path) -> None:
    """Java source, test roots, entry points, README, and test files are detected."""
    write_file(tmp_path, "README.md", "# Sample")
    write_file(
        tmp_path,
        "src/main/java/example/Application.java",
        "@SpringBootApplication\nclass Application { static void main(String[] args) {} }",
    )
    write_file(tmp_path, "src/test/java/example/ApplicationTest.java", "class ApplicationTest {}")

    inventory = run_agent(tmp_path).metadata["inventory"]

    assert inventory["language_file_counts"]["Java"] == 2
    assert "src/main/java" in inventory["source_roots"]
    assert "src/test/java" in inventory["test_roots"]
    assert inventory["test_file_count"] == 1
    assert inventory["has_tests"]
    assert inventory["readme_present"]
    assert inventory["application_entry_points"] == [
        "src/main/java/example/Application.java"
    ]


def test_maven_spring_boot_repository_detection(tmp_path: Path) -> None:
    """Maven and Spring Boot indicators are detected from a valid POM."""
    write_file(
        tmp_path,
        "pom.xml",
        """
        <project>
          <modelVersion>4.0.0</modelVersion>
          <parent>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-parent</artifactId>
            <version>3.3.0</version>
          </parent>
          <properties><java.version>17</java.version></properties>
          <dependencies>
            <dependency><groupId>org.springframework.boot</groupId>
              <artifactId>spring-boot-starter-web</artifactId></dependency>
          </dependencies>
        </project>
        """,
    )

    inventory = run_agent(tmp_path).metadata["inventory"]

    assert inventory["build_tools"]["maven"]
    assert "Spring Boot" in inventory["detected_frameworks"]
    assert inventory["java_version_hints"] == ["17"]
    assert "pom.xml" in inventory["build_tools"]["detected_files"]


def test_gradle_spring_boot_repository_and_modules(tmp_path: Path) -> None:
    """Gradle plugins, settings includes, and Spring Boot are detected."""
    write_file(tmp_path, "settings.gradle", "include ':app', ':shared'\n")
    write_file(
        tmp_path,
        "build.gradle",
        "plugins { id 'org.springframework.boot' version '3.3.0' }\n",
    )

    inventory = run_agent(tmp_path).metadata["inventory"]

    assert inventory["build_tools"]["gradle"]
    assert "Spring Boot" in inventory["detected_frameworks"]
    assert [module["name"] for module in inventory["gradle_modules"]] == ["app", "shared"]


def test_mixed_language_repository_counts_supported_languages(tmp_path: Path) -> None:
    """All initially supported language extensions are counted."""
    for filename in (
        "Main.java",
        "script.py",
        "main.go",
        "app.js",
        "app.ts",
        "Service.kt",
        "config.yml",
        "package.json",
        "pom.xml",
        "run.sh",
    ):
        write_file(tmp_path, filename, "content")

    inventory = run_agent(tmp_path).metadata["inventory"]

    assert set(inventory["detected_languages"]) == {
        "Java",
        "Python",
        "Go",
        "JavaScript",
        "TypeScript",
        "Kotlin",
        "YAML",
        "JSON",
        "XML",
        "Shell",
    }


def test_maven_module_detection(tmp_path: Path) -> None:
    """Maven module declarations become structured module summaries."""
    write_file(
        tmp_path,
        "pom.xml",
        "<project><modules><module>api</module><module>worker</module></modules></project>",
    )

    inventory = run_agent(tmp_path).metadata["inventory"]

    assert [(module["name"], module["path"]) for module in inventory["maven_modules"]] == [
        ("api", "api"),
        ("worker", "worker"),
    ]


def test_docker_and_github_actions_detection(tmp_path: Path) -> None:
    """Docker and GitHub Actions files are identified by path and filename."""
    write_file(tmp_path, "Dockerfile", "FROM eclipse-temurin:17")
    write_file(tmp_path, ".github/workflows/ci.yml", "name: CI")

    inventory = run_agent(tmp_path).metadata["inventory"]

    assert inventory["docker_files"] == ["Dockerfile"]
    assert inventory["ci_cd_files"] == [".github/workflows/ci.yml"]


def test_ignored_directories_and_git_presence(tmp_path: Path) -> None:
    """Generated/dependency directories are ignored while .git is detected."""
    (tmp_path / ".git").mkdir()
    write_file(tmp_path, "src/Main.java", "class Main {}")
    for directory in ("target", "node_modules", ".migrationswarm", ".venv", "build"):
        write_file(tmp_path, f"{directory}/Ignored.java", "class Ignored {}")

    inventory = run_agent(tmp_path).metadata["inventory"]

    assert inventory["git_repository_present"]
    assert inventory["total_file_count"] == 1
    assert inventory["language_file_counts"] == {"Java": 1}


def test_malformed_pom_records_warning_without_failing(tmp_path: Path) -> None:
    """Malformed build metadata is reported as a warning."""
    write_file(tmp_path, "pom.xml", "<project><broken>")

    result = run_agent(tmp_path)
    inventory = result.metadata["inventory"]

    assert result.success
    assert inventory["build_tools"]["maven"]
    assert any("pom.xml" in warning for warning in inventory["warnings"])


def test_inventory_artifact_is_valid_json_and_contains_inventory(tmp_path: Path) -> None:
    """The agent writes the same structured inventory it returns in metadata."""
    result = run_agent(tmp_path)
    artifact = tmp_path / INVENTORY_ARTIFACT
    artifact_data = json.loads(artifact.read_text(encoding="utf-8"))

    assert result.artifacts == [INVENTORY_ARTIFACT]
    assert artifact_data == result.metadata["inventory"]


def test_nonexistent_workspace_is_rejected(tmp_path: Path) -> None:
    """A missing workspace raises a clear domain exception."""
    missing = tmp_path / "missing"
    task = Task(
        project_id=PROJECT_ID,
        task_type=TaskType.REPOSITORY_ANALYSIS,
        title="Repository analysis",
        description="Inventory",
    )

    with pytest.raises(InvalidWorkspaceError, match="does not exist"):
        RepositoryAnalysisAgent().execute(
            task,
            AgentContext(project_id=PROJECT_ID, task=task, workspace_path=str(missing)),
        )


def test_workspace_file_is_rejected(tmp_path: Path) -> None:
    """A file path cannot be used as a repository workspace."""
    workspace_file = write_file(tmp_path, "workspace.txt", "not a directory")
    task = Task(
        project_id=PROJECT_ID,
        task_type=TaskType.REPOSITORY_ANALYSIS,
        title="Repository analysis",
        description="Inventory",
    )

    with pytest.raises(InvalidWorkspaceError, match="not a directory"):
        RepositoryAnalysisAgent().execute(
            task,
            AgentContext(project_id=PROJECT_ID, task=task, workspace_path=str(workspace_file)),
        )


def test_repository_agent_result_has_expected_structure(tmp_path: Path) -> None:
    """Repository execution returns a successful structured AgentResult."""
    result = run_agent(tmp_path)

    assert result.success
    assert result.agent_name == RepositoryAnalysisAgent.name
    assert result.summary.startswith("Scanned")
    assert result.metadata["inventory"]["repository_root"] == str(tmp_path.resolve())
    assert result.started_at.tzinfo is not None
    assert result.completed_at >= result.started_at


def test_repository_agent_capability_registration() -> None:
    """The production agent advertises and can be found by its capability."""
    registry = AgentRegistry()
    agent = RepositoryAnalysisAgent()
    registry.register(agent)

    assert registry.find_by_capability(AgentCapability.REPOSITORY_ANALYSIS) == (agent,)


def test_cli_analyze_repo_creates_inventory(tmp_path: Path) -> None:
    """The analyze-repo command prints a concise summary and writes its artifact."""
    write_file(tmp_path, "README.md", "# CLI fixture")

    result = runner.invoke(app, ["analyze-repo", str(tmp_path)])

    assert result.exit_code == 0
    assert "Repository Inventory" in result.stdout
    assert (tmp_path / INVENTORY_ARTIFACT).is_file()
