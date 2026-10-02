"""Deterministic, lightweight Java source dependency analysis."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, Field

from migrationswarm.core.agents.base import BaseAgent
from migrationswarm.core.agents.capabilities import AgentCapability
from migrationswarm.core.agents.models import AgentContext, AgentResult
from migrationswarm.core.security import (
    MAX_REPOSITORY_FILES,
    ensure_json_artifact_healthy,
    write_json_atomic,
)
from migrationswarm.core.tasks.models import Task

JAVA_DEPENDENCY_ARTIFACT: Final[str] = ".migrationswarm/java-dependency-graph.json"


class DependencyAnalysisError(ValueError):
    """Base exception for deterministic Java dependency analysis failures."""


class InvalidDependencyWorkspaceError(DependencyAnalysisError):
    """Raised when a dependency-analysis workspace is missing or not a directory."""


class JavaClassRole(StrEnum):
    """A coarse role inferred from common Spring and JPA annotations."""

    APPLICATION = "APPLICATION"
    CONTROLLER = "CONTROLLER"
    SERVICE = "SERVICE"
    REPOSITORY = "REPOSITORY"
    COMPONENT = "COMPONENT"
    CONFIGURATION = "CONFIGURATION"
    ENTITY = "ENTITY"
    OTHER = "OTHER"


class JavaDependencyRelationship(StrEnum):
    """Relationship types emitted for local Java classes."""

    IMPORTS = "IMPORTS"
    EXTENDS = "EXTENDS"
    IMPLEMENTS = "IMPLEMENTS"
    FIELD_DEPENDENCY = "FIELD_DEPENDENCY"
    CONSTRUCTOR_DEPENDENCY = "CONSTRUCTOR_DEPENDENCY"
    METHOD_PARAMETER_DEPENDENCY = "METHOD_PARAMETER_DEPENDENCY"
    METHOD_RETURN_DEPENDENCY = "METHOD_RETURN_DEPENDENCY"


class JavaAnnotation(BaseModel):
    """An annotation found on a Java type."""

    name: str
    arguments: str | None = None


class JavaClass(BaseModel):
    """Structured information extracted from one Java type declaration."""

    name: str
    fully_qualified_name: str
    package: str
    file_path: str
    role: JavaClassRole
    annotations: list[JavaAnnotation] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    declared_fields: list[str] = Field(default_factory=list)
    constructor_parameter_types: list[str] = Field(default_factory=list)
    method_parameter_types: list[str] = Field(default_factory=list)
    method_return_types: list[str] = Field(default_factory=list)
    extends: list[str] = Field(default_factory=list)
    implements: list[str] = Field(default_factory=list)

    @property
    def fqn(self) -> str:
        """Convenient short alias for the fully qualified name."""
        return self.fully_qualified_name


class JavaDependency(BaseModel):
    """One local class-to-class dependency relationship."""

    source: str
    target: str
    relationship: JavaDependencyRelationship
    evidence: str


class JavaDependencyGraph(BaseModel):
    """The complete in-memory result of Java dependency analysis."""

    repository_root: str
    total_java_classes: int
    total_dependency_edges: int
    role_counts: dict[str, int] = Field(default_factory=dict)
    classes: list[JavaClass] = Field(default_factory=list)
    dependencies: list[JavaDependency] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _ParsedJavaClass:
    """Internal parser output that retains source references for edge resolution."""

    java_class: JavaClass
    references: dict[JavaDependencyRelationship, tuple[str, ...]]


_TYPE_DECLARATION = re.compile(
    r"\b(class|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)"
)
_ANNOTATION = re.compile(r"@(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)(?:\((?P<args>[^)]*)\))?")
_IMPORT = re.compile(
    r"\bimport\s+(?:static\s+)?(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\.\*)*)\s*;"
)
_PACKAGE = re.compile(r"\bpackage\s+(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;")
_CONSTRUCTOR = re.compile(r"\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)\s*\{")
_METHOD = re.compile(
    r"(?:^|[;{}])\s*(?:public|private|protected|static|final|abstract|synchronized|native|\s)+"
    r"(?P<return>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?(?:\s*<[^;{}()]+>)?(?:\[\])?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)"
)
_FIELD = re.compile(
    r"(?:^|[;{}])\s*(?:public|private|protected|static|final|volatile|transient|\s)+"
    r"(?P<type>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?(?:\s*<[^;{}()]+>)?(?:\[\])?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:=|;)"
)
_PRIMITIVES: Final[frozenset[str]] = frozenset(
    {"byte", "short", "int", "long", "float", "double", "char", "boolean", "void"}
)
_IGNORED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".migrationswarm",
        ".gradle",
        ".idea",
        ".vscode",
        "target",
        "build",
        "out",
        "dist",
        "generated",
        "generated-sources",
        "__pycache__",
    }
)


class DependencyAnalysisAgent(BaseAgent):
    """Analyze local Java source files without building or modifying them."""

    name = "dependency-analysis"
    capabilities = frozenset({AgentCapability.DEPENDENCY_ANALYSIS})

    def __init__(self, include_test_sources: bool = False) -> None:
        self.include_test_sources = include_test_sources

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        """Analyze the workspace and write a structured graph artifact."""
        started_at = datetime.now(UTC)
        if context.workspace_path is None:
            raise InvalidDependencyWorkspaceError("Dependency analysis requires workspace_path")

        graph = self.analyze(context.workspace_path)
        artifact_path = Path(context.workspace_path).resolve() / JAVA_DEPENDENCY_ARTIFACT
        ensure_json_artifact_healthy(artifact_path)
        write_json_atomic(artifact_path, graph.model_dump(mode="json"))
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=(
                f"Analyzed {graph.total_java_classes} Java classes and "
                f"{graph.total_dependency_edges} dependency edges."
            ),
            artifacts=[JAVA_DEPENDENCY_ARTIFACT],
            metadata={"dependency_graph": graph.model_dump(mode="json")},
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    def analyze(self, workspace: str | Path) -> JavaDependencyGraph:
        """Return a dependency graph for a local workspace."""
        root = Path(workspace).expanduser().resolve()
        if not root.exists():
            raise InvalidDependencyWorkspaceError(f"Workspace does not exist: {root}")
        if not root.is_dir():
            raise InvalidDependencyWorkspaceError(f"Workspace is not a directory: {root}")

        parsed: list[_ParsedJavaClass] = []
        warnings: list[str] = []
        for path in self._java_files(root):
            try:
                if path.stat().st_size > 1_000_000:
                    warnings.append(
                        f"Skipped large Java source: {path.relative_to(root).as_posix()}"
                    )
                    continue
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                warnings.append(f"Could not read {path.relative_to(root).as_posix()}: {error}")
                continue
            parsed_class = self._parse_file(path, root, content)
            if parsed_class is None:
                warnings.append(
                    f"Could not find a supported top-level Java type in "
                    f"{path.relative_to(root).as_posix()}"
                )
            else:
                parsed.append(parsed_class)

        parsed.sort(key=lambda item: item.java_class.fully_qualified_name)
        dependencies = self._resolve_dependencies(parsed)
        role_counts: dict[str, int] = defaultdict(int)
        for item in parsed:
            role_counts[item.java_class.role.value] += 1

        return JavaDependencyGraph(
            repository_root=str(root),
            total_java_classes=len(parsed),
            total_dependency_edges=len(dependencies),
            role_counts=dict(sorted(role_counts.items())),
            classes=[item.java_class for item in parsed],
            dependencies=dependencies,
            warnings=warnings,
        )

    def _java_files(self, root: Path) -> list[Path]:
        files: list[Path] = []
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = sorted(
                directory
                for directory in directories
                if directory not in _IGNORED_DIRECTORIES
                and not (Path(current) / directory).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(current) / filename
                relative = path.relative_to(root)
                if path.suffix.lower() != ".java" or path.is_symlink():
                    continue
                if not self.include_test_sources and self._is_test_source(relative):
                    continue
                files.append(path)
                if len(files) > MAX_REPOSITORY_FILES:
                    raise DependencyAnalysisError(
                        f"Repository exceeds the {MAX_REPOSITORY_FILES}-file analysis limit"
                    )
        return files

    @staticmethod
    def _is_test_source(relative: Path) -> bool:
        parts = {part.lower() for part in relative.parts[:-1]}
        name = relative.name.lower()
        return bool(
            parts & {"test", "tests", "__tests__"}
            or name.startswith("test")
            or name.endswith("tests.java")
            or name.endswith("test.java")
        )

    @classmethod
    def _parse_file(cls, path: Path, root: Path, content: str) -> _ParsedJavaClass | None:
        source = cls._strip_comments(content)
        declaration = _TYPE_DECLARATION.search(source)
        if declaration is None:
            return None
        name = declaration.group("name")
        package_match = _PACKAGE.search(source)
        package = package_match.group("name") if package_match else ""
        fully_qualified_name = f"{package}.{name}" if package else name
        imports = sorted({match.group("name") for match in _IMPORT.finditer(source)})
        annotations = [
            JavaAnnotation(
                name=match.group("name").rsplit(".", 1)[-1],
                arguments=(match.group("args").strip() if match.group("args") else None),
            )
            for match in _ANNOTATION.finditer(source[: declaration.start()])
            if match.group("name") != "interface"
        ]
        header_end = source.find("{", declaration.end())
        header = source[declaration.end() : header_end if header_end >= 0 else len(source)]
        extends = cls._clause_types(header, "extends")
        implements = cls._clause_types(header, "implements")
        constructor_types: list[str] = []
        for match in _CONSTRUCTOR.finditer(source):
            if match.group("name") == name:
                constructor_types.extend(cls._parameter_types(match.group("params")))
        method_parameter_types: list[str] = []
        method_return_types: list[str] = []
        for match in _METHOD.finditer(source):
            if match.group("name") != name:
                method_return_types.append(match.group("return").strip())
                method_parameter_types.extend(cls._parameter_types(match.group("params")))
        declared_fields = [
            f"{match.group('type').strip()} {match.group('name')}"
            for match in _FIELD.finditer(source)
            if match.group("name") != name
        ]
        role = cls._class_role(annotations, name=name, extends=extends)
        java_class = JavaClass(
            name=name,
            fully_qualified_name=fully_qualified_name,
            package=package,
            file_path=path.relative_to(root).as_posix(),
            role=role,
            annotations=annotations,
            imports=imports,
            declared_fields=declared_fields,
            constructor_parameter_types=constructor_types,
            method_parameter_types=method_parameter_types,
            method_return_types=method_return_types,
            extends=extends,
            implements=implements,
        )
        references: dict[JavaDependencyRelationship, tuple[str, ...]] = {
            JavaDependencyRelationship.EXTENDS: tuple(extends),
            JavaDependencyRelationship.IMPLEMENTS: tuple(implements),
            JavaDependencyRelationship.FIELD_DEPENDENCY: tuple(
                match.group("type").strip() for match in _FIELD.finditer(source)
            ),
            JavaDependencyRelationship.CONSTRUCTOR_DEPENDENCY: tuple(constructor_types),
            JavaDependencyRelationship.METHOD_PARAMETER_DEPENDENCY: tuple(method_parameter_types),
            JavaDependencyRelationship.METHOD_RETURN_DEPENDENCY: tuple(method_return_types),
        }
        return _ParsedJavaClass(java_class=java_class, references=references)

    @staticmethod
    def _strip_comments(source: str) -> str:
        without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
        return re.sub(r"//[^\r\n]*", " ", without_block)

    @staticmethod
    def _clause_types(header: str, keyword: str) -> list[str]:
        match = re.search(
            rf"\b{keyword}\s+(.+?)(?=\b(?:extends|implements)\b|$)",
            header,
            flags=re.DOTALL,
        )
        if match is None:
            return []
        return [item.strip() for item in match.group(1).split(",") if item.strip()]

    @staticmethod
    def _parameter_types(parameters: str) -> list[str]:
        types: list[str] = []
        for parameter in parameters.split(","):
            cleaned = re.sub(r"@\w+(?:\([^)]*\))?", "", parameter).strip()
            cleaned = re.sub(r"\bfinal\b", "", cleaned).strip()
            tokens = cleaned.split()
            if len(tokens) >= 2:
                types.append(" ".join(tokens[:-1]))
        return types

    @staticmethod
    def _class_role(
        annotations: list[JavaAnnotation], *, name: str = "", extends: list[str] | None = None
    ) -> JavaClassRole:
        names = {annotation.name for annotation in annotations}
        if "SpringBootApplication" in names:
            return JavaClassRole.APPLICATION
        if names & {"RestController", "Controller"}:
            return JavaClassRole.CONTROLLER
        if "Service" in names:
            return JavaClassRole.SERVICE
        if "Repository" in names or name.endswith("Repository") or any(
            "Repository" in item for item in (extends or [])
        ):
            return JavaClassRole.REPOSITORY
        if "Configuration" in names:
            return JavaClassRole.CONFIGURATION
        if "Component" in names:
            return JavaClassRole.COMPONENT
        if names & {"Entity", "Table"}:
            return JavaClassRole.ENTITY
        return JavaClassRole.OTHER

    @classmethod
    def _resolve_dependencies(cls, parsed: list[_ParsedJavaClass]) -> list[JavaDependency]:
        by_fqn = {item.java_class.fully_qualified_name: item for item in parsed}
        by_simple: dict[str, list[str]] = defaultdict(list)
        for item in parsed:
            by_simple[item.java_class.name].append(item.java_class.fully_qualified_name)
        for targets in by_simple.values():
            targets.sort()

        evidence: dict[tuple[str, str, JavaDependencyRelationship], set[str]] = defaultdict(set)

        for item in parsed:
            source = item.java_class
            explicit_imports = {
                import_name.rsplit(".", 1)[-1]: import_name
                for import_name in source.imports
                if not import_name.endswith(".*")
            }
            wildcard_packages = [
                import_name[:-2] for import_name in source.imports if import_name.endswith(".*")
            ]
            for import_name in source.imports:
                if import_name in by_fqn:
                    cls._add_edge(
                        evidence,
                        source.fully_qualified_name,
                        import_name,
                        JavaDependencyRelationship.IMPORTS,
                        f"import {import_name}",
                    )
            for package in wildcard_packages:
                candidates = [
                    target
                    for target in by_fqn
                    if target.rsplit(".", 1)[0] == package
                ]
                if len(candidates) == 1:
                    cls._add_edge(
                        evidence,
                        source.fully_qualified_name,
                        candidates[0],
                        JavaDependencyRelationship.IMPORTS,
                        f"wildcard import {package}.*",
                    )

            for relationship, type_names in item.references.items():
                for type_name in type_names:
                    target = cls._resolve_type(
                        type_name,
                        source,
                        by_fqn,
                        by_simple,
                        explicit_imports,
                        wildcard_packages,
                    )
                    if target is not None and target != source.fully_qualified_name:
                        cls._add_edge(
                            evidence,
                            source.fully_qualified_name,
                            target,
                            relationship,
                            f"{relationship.value.lower()}: {type_name}",
                        )

        return [
            JavaDependency(
                source=source,
                target=target,
                relationship=relationship,
                evidence="; ".join(sorted(reasons)),
            )
            for (source, target, relationship), reasons in sorted(
                evidence.items(), key=lambda item: (item[0][0], item[0][1], item[0][2].value)
            )
        ]

    @staticmethod
    def _add_edge(
        evidence: dict[tuple[str, str, JavaDependencyRelationship], set[str]],
        source: str,
        target: str,
        relationship: JavaDependencyRelationship,
        reason: str,
    ) -> None:
        evidence[(source, target, relationship)].add(reason)

    @classmethod
    def _resolve_type(
        cls,
        type_name: str,
        source: JavaClass,
        by_fqn: dict[str, _ParsedJavaClass],
        by_simple: dict[str, list[str]],
        explicit_imports: dict[str, str],
        wildcard_packages: list[str],
    ) -> str | None:
        base = cls._base_type(type_name)
        if base in _PRIMITIVES:
            return None
        if base in by_fqn:
            return base
        if base in explicit_imports and explicit_imports[base] in by_fqn:
            return explicit_imports[base]
        same_package = f"{source.package}.{base}" if source.package else base
        if same_package in by_fqn:
            return same_package
        wildcard_candidates = [f"{package}.{base}" for package in wildcard_packages]
        wildcard_local = [candidate for candidate in wildcard_candidates if candidate in by_fqn]
        if len(wildcard_local) == 1:
            return wildcard_local[0]
        candidates = by_simple.get(base, [])
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _base_type(type_name: str) -> str:
        cleaned = re.sub(r"\?\s*(?:extends|super)?\s*", "", type_name)
        cleaned = cleaned.split("<", 1)[0].strip().replace("[]", "")
        return cleaned.rsplit(".", 1)[-1]
