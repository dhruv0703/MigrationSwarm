"""Deterministic local repository inventory analysis."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from migrationswarm.core.agents.base import BaseAgent
from migrationswarm.core.agents.capabilities import AgentCapability
from migrationswarm.core.agents.models import AgentContext, AgentResult
from migrationswarm.core.security import (
    MAX_REPOSITORY_FILES,
    ensure_json_artifact_healthy,
    write_json_atomic,
)
from migrationswarm.core.tasks.models import Task

MAX_INSPECT_BYTES: Final[int] = 1_000_000
INVENTORY_ARTIFACT: Final[str] = ".migrationswarm/repository-inventory.json"


class RepositoryAnalysisError(ValueError):
    """Raised when a repository cannot be analyzed or its artifact written."""


class InvalidWorkspaceError(RepositoryAnalysisError):
    """Raised when the analysis workspace does not exist or is not a directory."""


class LanguageSummary(BaseModel):
    """A detected language and the number of files using its extension."""

    language: str
    file_count: int = Field(ge=0)


class BuildToolSummary(BaseModel):
    """Detected Java build tools and their relevant files."""

    maven: bool = False
    gradle: bool = False
    detected_files: list[str] = Field(default_factory=list)


class FrameworkSummary(BaseModel):
    """A detected framework and the files that provided evidence."""

    name: str
    evidence: list[str] = Field(default_factory=list)


class ModuleSummary(BaseModel):
    """A Maven or Gradle module reference."""

    name: str
    path: str
    build_tool: str


class RepositoryInventory(BaseModel):
    """Structured, JSON-serializable facts about a local repository."""

    model_config = ConfigDict(extra="forbid")

    repository_root: str
    total_file_count: int = Field(ge=0)
    total_directory_count: int = Field(ge=0)
    languages: list[LanguageSummary] = Field(default_factory=list)
    detected_languages: list[str] = Field(default_factory=list)
    language_file_counts: dict[str, int] = Field(default_factory=dict)
    build_tools: BuildToolSummary
    frameworks: list[FrameworkSummary] = Field(default_factory=list)
    detected_frameworks: list[str] = Field(default_factory=list)
    java_version_hints: list[str] = Field(default_factory=list)
    maven_modules: list[ModuleSummary] = Field(default_factory=list)
    gradle_modules: list[ModuleSummary] = Field(default_factory=list)
    has_tests: bool = False
    test_file_count: int = Field(default=0, ge=0)
    configuration_files: list[str] = Field(default_factory=list)
    docker_files: list[str] = Field(default_factory=list)
    ci_cd_files: list[str] = Field(default_factory=list)
    source_roots: list[str] = Field(default_factory=list)
    test_roots: list[str] = Field(default_factory=list)
    application_entry_points: list[str] = Field(default_factory=list)
    readme_present: bool = False
    git_repository_present: bool = False
    warnings: list[str] = Field(default_factory=list)


class RepositoryAnalysisAgent(BaseAgent):
    """Inspect a local repository without changing its application files."""

    name = "repository-analysis"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        """Scan the context workspace and write its structured inventory artifact."""
        started_at = datetime.now(UTC)
        root = self._workspace_root(context.workspace_path)
        inventory = self.analyze(root)
        artifact_path = root / INVENTORY_ARTIFACT
        try:
            ensure_json_artifact_healthy(artifact_path)
            write_json_atomic(artifact_path, inventory.model_dump(mode="json"))
        except OSError as error:
            raise RepositoryAnalysisError(
                f"Could not write repository inventory artifact: {artifact_path}"
            ) from error

        completed_at = datetime.now(UTC)
        inventory_data = inventory.model_dump(mode="json")
        summary = (
            f"Scanned {inventory.total_file_count} files and "
            f"{inventory.total_directory_count} directories; "
            f"detected {len(inventory.detected_languages)} languages."
        )
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=summary,
            artifacts=[INVENTORY_ARTIFACT],
            metadata={"inventory": inventory_data},
            started_at=started_at,
            completed_at=completed_at,
        )

    def analyze(self, workspace: str | Path) -> RepositoryInventory:
        """Build an inventory for a workspace without writing an artifact."""
        root = self._workspace_root(workspace)
        files, directories, warnings = self._collect_paths(root)
        relative_files = {path: self._relative(path, root) for path in files}
        text_contents: dict[Path, str] = {}
        for path in files:
            content = self._read_text(path, relative_files[path], warnings)
            if content is not None:
                text_contents[path] = content

        language_counts = Counter(
            language
            for path in files
            if (language := self._language_for(path)) is not None
        )
        language_file_counts = dict(sorted(language_counts.items()))
        languages = [
            LanguageSummary(language=language, file_count=count)
            for language, count in language_file_counts.items()
        ]

        maven_files = sorted(
            relative_files[path]
            for path in files
            if path.name.lower() in {"pom.xml", "mvnw", "mvnw.cmd"}
        )
        gradle_files = sorted(
            relative_files[path]
            for path in files
            if path.name.lower()
            in {
                "build.gradle",
                "build.gradle.kts",
                "gradlew",
                "settings.gradle",
                "settings.gradle.kts",
            }
        )
        maven_modules: list[ModuleSummary] = []
        gradle_modules: list[ModuleSummary] = []
        spring_evidence: set[str] = set()
        java_version_hints: set[str] = set()

        for path in files:
            if path.name.lower() == "pom.xml":
                modules, spring_detected, versions = self._inspect_pom(
                    path, root, relative_files[path], warnings
                )
                maven_modules.extend(modules)
                java_version_hints.update(versions)
                if spring_detected:
                    spring_evidence.add(relative_files[path])

        for path in files:
            if path.name.lower() in {"settings.gradle", "settings.gradle.kts"}:
                gradle_modules.extend(
                    self._inspect_gradle_modules(text_contents.get(path, ""), root)
                )
            if path.name.lower() in {"build.gradle", "build.gradle.kts"}:
                content = text_contents.get(path, "")
                java_version_hints.update(self._extract_java_versions(content))
                if "org.springframework.boot" in content or "spring-boot" in content:
                    spring_evidence.add(relative_files[path])

        entry_points: set[str] = set()
        for path, content in text_contents.items():
            relative_path = relative_files[path]
            if "@SpringBootApplication" in content:
                entry_points.add(relative_path)
                spring_evidence.add(relative_path)
            if path.suffix.lower() in {".java", ".kt"} and re.search(
                r"\bstatic\s+void\s+main\s*\(|\bfun\s+main\s*\(", content
            ):
                if "Application" in path.stem:
                    entry_points.add(relative_path)
            if path.suffix.lower() == ".py" and '__name__ == "__main__"' in content:
                entry_points.add(relative_path)
            if path.suffix.lower() == ".go" and re.search(r"\bfunc\s+main\s*\(", content):
                entry_points.add(relative_path)

        frameworks: list[FrameworkSummary] = []
        if spring_evidence:
            frameworks.append(
                FrameworkSummary(name="Spring Boot", evidence=sorted(spring_evidence))
            )

        source_roots, test_roots = self._find_roots(directories)
        test_files = [
            relative_files[path]
            for path in files
            if self._is_test_file(path, relative_files[path])
        ]
        configuration_files = sorted(
            relative_files[path]
            for path in files
            if self._is_configuration_file(path, relative_files[path])
        )
        docker_files = sorted(
            relative_files[path]
            for path in files
            if self._is_docker_file(path)
        )
        ci_cd_files = sorted(
            relative_files[path]
            for path in files
            if self._is_ci_file(path, relative_files[path])
        )
        readme_present = any(
            path.parent == root and path.name.lower().startswith("readme") for path in files
        )
        frameworks = sorted(frameworks, key=lambda framework: framework.name)
        maven_modules = self._unique_modules(maven_modules)
        gradle_modules = self._unique_modules(gradle_modules)
        warning_list = sorted(set(warnings))

        return RepositoryInventory(
            repository_root=str(root),
            total_file_count=len(files),
            total_directory_count=len(directories),
            languages=languages,
            detected_languages=[summary.language for summary in languages],
            language_file_counts=language_file_counts,
            build_tools=BuildToolSummary(
                maven=bool(maven_files),
                gradle=bool(gradle_files),
                detected_files=sorted(set(maven_files + gradle_files)),
            ),
            frameworks=frameworks,
            detected_frameworks=[framework.name for framework in frameworks],
            java_version_hints=sorted(java_version_hints),
            maven_modules=maven_modules,
            gradle_modules=gradle_modules,
            has_tests=bool(test_files),
            test_file_count=len(test_files),
            configuration_files=configuration_files,
            docker_files=docker_files,
            ci_cd_files=ci_cd_files,
            source_roots=source_roots,
            test_roots=test_roots,
            application_entry_points=sorted(entry_points),
            readme_present=readme_present,
            git_repository_present=(root / ".git").exists(),
            warnings=warning_list,
        )

    @staticmethod
    def _workspace_root(workspace: str | Path | None) -> Path:
        if workspace is None:
            raise InvalidWorkspaceError("A workspace_path is required for repository analysis")
        root = Path(workspace).expanduser()
        if not root.exists():
            raise InvalidWorkspaceError(f"Workspace does not exist: {root}")
        if not root.is_dir():
            raise InvalidWorkspaceError(f"Workspace is not a directory: {root}")
        return root.resolve()

    @classmethod
    def _collect_paths(cls, root: Path) -> tuple[list[Path], list[str], list[str]]:
        files: list[Path] = []
        directories: list[str] = []
        warnings: list[str] = []

        def on_error(error: OSError) -> None:
            warnings.append(f"Could not inspect directory: {error.filename or error}")

        for current, dir_names, file_names in os.walk(
            root, topdown=True, followlinks=False, onerror=on_error
        ):
            current_path = Path(current)
            kept_dirs: list[str] = []
            for name in sorted(dir_names):
                path = current_path / name
                if name in cls._ignored_directories or path.is_symlink():
                    continue
                kept_dirs.append(name)
                directories.append(cls._relative(path, root))
            dir_names[:] = kept_dirs
            for name in sorted(file_names):
                path = current_path / name
                if path.is_symlink():
                    continue
                try:
                    if path.is_file():
                        files.append(path)
                        if len(files) > MAX_REPOSITORY_FILES:
                            raise RepositoryAnalysisError(
                                f"Repository exceeds the {MAX_REPOSITORY_FILES}-file analysis limit"
                            )
                except OSError as error:
                    warnings.append(f"Could not inspect file {path.name}: {error}")
        return (
            sorted(files, key=lambda path: cls._relative(path, root)),
            sorted(directories),
            warnings,
        )

    @staticmethod
    def _relative(path: Path, root: Path) -> str:
        return path.relative_to(root).as_posix()

    @staticmethod
    def _read_text(path: Path, relative_path: str, warnings: list[str]) -> str | None:
        try:
            if path.stat().st_size > MAX_INSPECT_BYTES:
                warnings.append(f"Skipped large file content: {relative_path}")
                return None
            data = path.read_bytes()
        except OSError as error:
            warnings.append(f"Could not read {relative_path}: {error}")
            return None
        if b"\x00" in data:
            return None
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _language_for(path: Path) -> str | None:
        suffix = path.suffix.lower()
        if suffix == ".java":
            return "Java"
        if suffix == ".py":
            return "Python"
        if suffix == ".go":
            return "Go"
        if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
            return "JavaScript"
        if suffix in {".ts", ".tsx", ".mts", ".cts"}:
            return "TypeScript"
        if suffix in {".kt", ".kts"}:
            return "Kotlin"
        if suffix in {".yml", ".yaml"}:
            return "YAML"
        if suffix == ".json":
            return "JSON"
        if suffix == ".xml":
            return "XML"
        if suffix in {".sh", ".bash", ".zsh"}:
            return "Shell"
        return None

    @staticmethod
    def _inspect_pom(
        path: Path,
        root: Path,
        relative_path: str,
        warnings: list[str],
    ) -> tuple[list[ModuleSummary], bool, list[str]]:
        raw_text = RepositoryAnalysisAgent._safe_text(path)
        try:
            parsed = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as error:
            warnings.append(f"Could not parse {relative_path}: {error}")
            return [], bool(raw_text and "spring-boot" in raw_text), (
                RepositoryAnalysisAgent._extract_java_versions(raw_text or "")
            )

        values = [
            (element.text or "").strip()
            for element in parsed.iter()
            if (element.text or "").strip()
        ]
        spring_detected = any(
            "spring-boot" in value or "org.springframework.boot" in value for value in values
        )
        modules: list[ModuleSummary] = []
        for element in parsed.iter():
            if element.tag.rsplit("}", 1)[-1] != "module" or not element.text:
                continue
            module_name = element.text.strip()
            module_path = (path.parent / module_name).resolve()
            try:
                relative_module_path = module_path.relative_to(root).as_posix()
            except ValueError:
                relative_module_path = module_name.replace("\\", "/")
            modules.append(
                ModuleSummary(name=module_name, path=relative_module_path, build_tool="maven")
            )
        return modules, spring_detected, RepositoryAnalysisAgent._extract_java_versions(
            raw_text or ""
        )

    @staticmethod
    def _safe_text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    @staticmethod
    def _extract_java_versions(content: str) -> list[str]:
        """Extract common Java version declarations without running build tools."""
        patterns = (
            r"<(?:java\.version|maven\.compiler\.(?:source|target|release))>\s*([^<]+)",
            r"(?:sourceCompatibility|targetCompatibility)\s*=\s*['\"](?:JavaVersion\.VERSION_)?([^'\"]+)",
            r"JavaLanguageVersion\.of\(\s*([0-9]+)\s*\)",
        )
        versions: set[str] = set()
        for pattern in patterns:
            versions.update(match.strip() for match in re.findall(pattern, content))
        return sorted(versions)

    @staticmethod
    def _inspect_gradle_modules(content: str, root: Path) -> list[ModuleSummary]:
        del root
        modules: list[ModuleSummary] = []
        for match in re.finditer(r"['\"](:[^'\"]+)['\"]", content):
            module_reference = match.group(1)
            module_path = module_reference.lstrip(":").replace(":", "/")
            modules.append(
                ModuleSummary(
                    name=module_path.rsplit("/", 1)[-1],
                    path=module_path,
                    build_tool="gradle",
                )
            )
        return modules

    @staticmethod
    def _unique_modules(modules: Iterable[ModuleSummary]) -> list[ModuleSummary]:
        unique = {(module.build_tool, module.path): module for module in modules}
        return [unique[key] for key in sorted(unique)]

    @staticmethod
    def _find_roots(directories: Iterable[str]) -> tuple[list[str], list[str]]:
        directory_set = set(directories)
        source_roots: set[str] = set()
        test_roots: set[str] = set()
        for directory in directory_set:
            parts = Path(directory).parts
            lower_parts = {part.lower() for part in parts}
            if len(parts) >= 3 and parts[-3:-2] == ("src",) and parts[-2] == "main":
                source_roots.add(directory)
            if len(parts) >= 3 and parts[-3:-2] == ("src",) and parts[-2] == "test":
                test_roots.add(directory)
            if parts and parts[-1].lower() in {"test", "tests", "__tests__"}:
                test_roots.add(directory)
            if parts == ("src",) and not any("src/main" in path for path in directory_set):
                source_roots.add(directory)
            del lower_parts
        return sorted(source_roots), sorted(test_roots)

    @staticmethod
    def _is_test_file(path: Path, relative_path: str) -> bool:
        parts = {part.lower() for part in Path(relative_path).parts[:-1]}
        name = path.name.lower()
        return bool(
            parts & {"test", "tests", "__tests__"}
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name.endswith("_test.go")
            or re.search(r"(?:test|tests|spec)\.[^.]+$", name) is not None
            or re.search(r"tests?\.(java|kt)$", name) is not None
        )

    @staticmethod
    def _is_configuration_file(path: Path, relative_path: str) -> bool:
        name = path.name.lower()
        return bool(
            path.suffix.lower() in {".properties", ".ini", ".toml", ".json"}
            or name.startswith(".env")
            or name in {"application.yml", "application.yaml", "application.properties"}
            or "config" in {part.lower() for part in Path(relative_path).parts[:-1]}
        )

    @staticmethod
    def _is_docker_file(path: Path) -> bool:
        name = path.name.lower()
        return bool(
            name == "dockerfile"
            or name.startswith("dockerfile.")
            or name in {"docker-compose.yml", "docker-compose.yaml"}
            or path.suffix.lower() == ".dockerfile"
        )

    @staticmethod
    def _is_ci_file(path: Path, relative_path: str) -> bool:
        parts = {part.lower() for part in Path(relative_path).parts}
        name = path.name.lower()
        return bool(
            {".github", "workflows"}.issubset(parts)
            or name in {".gitlab-ci.yml", ".gitlab-ci.yaml", "azure-pipelines.yml"}
            or ".circleci" in parts
            or ".buildkite" in parts
        )

    _ignored_directories: Final[frozenset[str]] = frozenset(
        {
            ".git",
            ".migrationswarm",
            "node_modules",
            "target",
            "build",
            "dist",
            "out",
            "__pycache__",
            ".venv",
            "venv",
            ".idea",
            ".vscode",
            ".gradle",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
        }
    )
