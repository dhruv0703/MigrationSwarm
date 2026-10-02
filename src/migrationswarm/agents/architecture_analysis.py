"""Deterministic architecture metrics for a Java dependency graph."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

import networkx as nx
from pydantic import BaseModel, Field

from migrationswarm.agents.dependency_analysis import (
    JAVA_DEPENDENCY_ARTIFACT,
    DependencyAnalysisAgent,
    JavaClassRole,
    JavaDependencyGraph,
)
from migrationswarm.core.agents.base import BaseAgent
from migrationswarm.core.agents.capabilities import AgentCapability
from migrationswarm.core.agents.models import AgentContext, AgentResult
from migrationswarm.core.security import (
    ArtifactCorruptionError,
    ensure_json_artifact_healthy,
    load_json_object,
    write_json_atomic,
    write_text_atomic,
)
from migrationswarm.core.tasks.models import Task

ARCHITECTURE_REPORT_ARTIFACT: Final[str] = ".migrationswarm/architecture-report.json"
JAVA_DEPENDENCY_DOT_ARTIFACT: Final[str] = ".migrationswarm/java-dependency-graph.dot"

# These defaults are deliberately small and visible so future phases can tune them.
HIGH_FAN_OUT_THRESHOLD: Final[int] = 3
HIGH_FAN_IN_THRESHOLD: Final[int] = 3


class ArchitectureAnalysisError(ValueError):
    """Base exception for architecture analysis failures."""


class InvalidArchitectureWorkspaceError(ArchitectureAnalysisError):
    """Raised when an architecture-analysis workspace is invalid."""


class ArchitectureRiskType(StrEnum):
    """Deterministic architecture risk categories."""

    HIGH_FAN_OUT = "HIGH_FAN_OUT"
    HIGH_FAN_IN = "HIGH_FAN_IN"
    CYCLIC_DEPENDENCY = "CYCLIC_DEPENDENCY"
    CROSS_PACKAGE_COUPLING = "CROSS_PACKAGE_COUPLING"
    ISOLATED_CLASS = "ISOLATED_CLASS"


class ClassArchitectureMetric(BaseModel):
    """Degree and role metrics for one Java class."""

    fully_qualified_name: str
    name: str
    package: str
    role: JavaClassRole
    in_degree: int
    out_degree: int
    fan_in: int
    fan_out: int


class PackageArchitectureMetric(BaseModel):
    """Class and coupling metrics for one Java package."""

    package: str
    class_count: int
    in_degree: int
    out_degree: int
    fan_in: int
    fan_out: int
    internal_edge_count: int
    external_edge_count: int


class ArchitectureRisk(BaseModel):
    """One explainable architecture risk indicator."""

    risk_type: ArchitectureRiskType
    subject: str
    description: str


class ArchitectureComponent(BaseModel):
    """A deterministic candidate structural component, not a service boundary."""

    component_id: str
    classes: list[str]
    packages: list[str]
    internal_edge_count: int
    external_edge_count: int
    controller_count: int
    service_count: int
    repository_count: int


class ArchitectureReport(BaseModel):
    """Structured architecture-level analysis output."""

    repository_root: str
    total_classes: int
    total_dependencies: int
    connected_component_count: int
    strongly_connected_component_count: int
    cycle_count: int
    class_metrics: list[ClassArchitectureMetric] = Field(default_factory=list)
    package_metrics: list[PackageArchitectureMetric] = Field(default_factory=list)
    risks: list[ArchitectureRisk] = Field(default_factory=list)
    components: list[ArchitectureComponent] = Field(default_factory=list)
    high_coupling_classes: list[str] = Field(default_factory=list)
    isolated_classes: list[str] = Field(default_factory=list)
    controller_service_relationships: int = 0
    service_repository_relationships: int = 0
    warnings: list[str] = Field(default_factory=list)


class ArchitectureAnalysisAgent(BaseAgent):
    """Compute deterministic architecture metrics from local Java dependencies."""

    name = "architecture-analysis"
    capabilities = frozenset({AgentCapability.ARCHITECTURE_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        """Load or produce a dependency graph, then write architecture artifacts."""
        started_at = datetime.now(UTC)
        if context.workspace_path is None:
            raise InvalidArchitectureWorkspaceError(
                "Architecture analysis requires workspace_path"
            )

        root = Path(context.workspace_path).expanduser().resolve()
        report = self.analyze(root)
        report_path = root / ARCHITECTURE_REPORT_ARTIFACT
        dot_path = root / JAVA_DEPENDENCY_DOT_ARTIFACT
        report_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_json_artifact_healthy(report_path)
        write_json_atomic(report_path, report.model_dump(mode="json"))
        write_text_atomic(dot_path, self._dot_for_graph(self._last_graph))
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=(
                f"Analyzed {report.total_classes} classes, "
                f"{report.total_dependencies} dependencies, and "
                f"{len(report.components)} candidate components."
            ),
            artifacts=[ARCHITECTURE_REPORT_ARTIFACT, JAVA_DEPENDENCY_DOT_ARTIFACT],
            metadata={"architecture_report": report.model_dump(mode="json")},
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )

    def analyze(self, workspace: str | Path) -> ArchitectureReport:
        """Analyze an existing dependency artifact or derive one in memory."""
        root = Path(workspace).expanduser().resolve()
        if not root.exists():
            raise InvalidArchitectureWorkspaceError(f"Workspace does not exist: {root}")
        if not root.is_dir():
            raise InvalidArchitectureWorkspaceError(f"Workspace is not a directory: {root}")

        artifact = root / JAVA_DEPENDENCY_ARTIFACT
        warnings: list[str] = []
        if artifact.is_file():
            try:
                dependency_graph = JavaDependencyGraph.model_validate(load_json_object(artifact))
            except (ArtifactCorruptionError, ValueError) as error:
                raise ArchitectureAnalysisError(
                    f"Could not load {JAVA_DEPENDENCY_ARTIFACT}: artifact is corrupt"
                ) from error
        else:
            dependency_graph = DependencyAnalysisAgent().analyze(root)

        report, graph = self.analyze_graph(dependency_graph, warnings=warnings)
        self._last_graph = graph
        return report

    def analyze_graph(
        self,
        dependency_graph: JavaDependencyGraph,
        warnings: list[str] | None = None,
    ) -> tuple[ArchitectureReport, nx.MultiDiGraph[str]]:
        """Return a report and NetworkX graph for a structured dependency graph."""
        graph = self.build_graph(dependency_graph)
        report = self._build_report(dependency_graph, graph, warnings or [])
        return report, graph

    @staticmethod
    def build_graph(dependency_graph: JavaDependencyGraph) -> nx.MultiDiGraph[str]:
        """Build a directed multigraph, retaining every relationship edge."""
        graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        for java_class in sorted(
            dependency_graph.classes, key=lambda item: item.fully_qualified_name
        ):
            graph.add_node(
                java_class.fully_qualified_name,
                name=java_class.name,
                package=java_class.package,
                role=java_class.role.value,
                file_path=java_class.file_path,
            )
        for dependency in sorted(
            dependency_graph.dependencies,
            key=lambda item: (item.source, item.target, item.relationship.value, item.evidence),
        ):
            if dependency.source in graph and dependency.target in graph:
                graph.add_edge(
                    dependency.source,
                    dependency.target,
                    relationship=dependency.relationship.value,
                    evidence=dependency.evidence,
                )
        return graph

    def _build_report(
        self,
        dependency_graph: JavaDependencyGraph,
        graph: nx.MultiDiGraph[str],
        warnings: list[str],
    ) -> ArchitectureReport:
        class_metrics = self._class_metrics(graph)
        package_metrics = self._package_metrics(graph)
        cycles = self._cycles(graph)
        risks = self._risks(graph, class_metrics, package_metrics, cycles)
        components = self._components(graph)
        high_coupling = sorted(
            metric.fully_qualified_name
            for metric in class_metrics
            if metric.fan_in >= HIGH_FAN_IN_THRESHOLD
            or metric.fan_out >= HIGH_FAN_OUT_THRESHOLD
        )
        isolated = sorted(
            metric.fully_qualified_name
            for metric in class_metrics
            if metric.in_degree == 0 and metric.out_degree == 0
        )
        controller_service = self._role_relationship_count(
            graph, JavaClassRole.CONTROLLER, JavaClassRole.SERVICE
        )
        service_repository = self._role_relationship_count(
            graph, JavaClassRole.SERVICE, JavaClassRole.REPOSITORY
        )
        return ArchitectureReport(
            repository_root=dependency_graph.repository_root,
            total_classes=graph.number_of_nodes(),
            total_dependencies=graph.number_of_edges(),
            connected_component_count=nx.number_weakly_connected_components(graph),
            strongly_connected_component_count=nx.number_strongly_connected_components(graph),
            cycle_count=len(cycles),
            class_metrics=class_metrics,
            package_metrics=package_metrics,
            risks=risks,
            components=components,
            high_coupling_classes=high_coupling,
            isolated_classes=isolated,
            controller_service_relationships=controller_service,
            service_repository_relationships=service_repository,
            warnings=sorted(set(warnings) | set(dependency_graph.warnings)),
        )

    @staticmethod
    def _class_metrics(graph: nx.MultiDiGraph[str]) -> list[ClassArchitectureMetric]:
        return [
            ClassArchitectureMetric(
                fully_qualified_name=node,
                name=attributes["name"],
                package=attributes["package"],
                role=JavaClassRole(attributes["role"]),
                in_degree=graph.in_degree(node),
                out_degree=graph.out_degree(node),
                fan_in=graph.in_degree(node),
                fan_out=graph.out_degree(node),
            )
            for node, attributes in sorted(graph.nodes(data=True))
        ]

    @staticmethod
    def _package_metrics(graph: nx.MultiDiGraph[str]) -> list[PackageArchitectureMetric]:
        package_nodes: dict[str, set[str]] = defaultdict(set)
        for node, attributes in graph.nodes(data=True):
            package_nodes[attributes["package"]].add(node)
        metrics: list[PackageArchitectureMetric] = []
        for package in sorted(package_nodes):
            nodes = package_nodes[package]
            internal = 0
            external = 0
            incoming = 0
            outgoing = 0
            for source, target, _key in graph.edges(keys=True):
                source_package = graph.nodes[source]["package"]
                target_package = graph.nodes[target]["package"]
                if source_package == package and target_package == package:
                    internal += 1
                elif source_package == package and target_package != package:
                    outgoing += 1
                    external += 1
                elif target_package == package and source_package != package:
                    incoming += 1
                    external += 1
            metrics.append(
                PackageArchitectureMetric(
                    package=package,
                    class_count=len(nodes),
                    in_degree=incoming,
                    out_degree=outgoing,
                    fan_in=incoming,
                    fan_out=outgoing,
                    internal_edge_count=internal,
                    external_edge_count=external,
                )
            )
        return metrics

    @staticmethod
    def _cycles(graph: nx.MultiDiGraph[str]) -> list[tuple[str, ...]]:
        simple_graph = nx.DiGraph(graph)
        cycles = {
            ArchitectureAnalysisAgent._canonical_cycle(tuple(cycle))
            for cycle in nx.simple_cycles(simple_graph)
        }
        return sorted(cycles)

    @staticmethod
    def _canonical_cycle(cycle: tuple[str, ...]) -> tuple[str, ...]:
        if not cycle:
            return cycle
        rotations = [cycle[index:] + cycle[:index] for index in range(len(cycle))]
        return min(rotations)

    @staticmethod
    def _risks(
        graph: nx.MultiDiGraph[str],
        class_metrics: list[ClassArchitectureMetric],
        package_metrics: list[PackageArchitectureMetric],
        cycles: list[tuple[str, ...]],
    ) -> list[ArchitectureRisk]:
        risks: list[ArchitectureRisk] = []
        for metric in class_metrics:
            if metric.fan_out >= HIGH_FAN_OUT_THRESHOLD:
                risks.append(
                    ArchitectureRisk(
                        risk_type=ArchitectureRiskType.HIGH_FAN_OUT,
                        subject=metric.fully_qualified_name,
                        description=(
                            f"Fan-out {metric.fan_out} meets threshold "
                            f"{HIGH_FAN_OUT_THRESHOLD}."
                        ),
                    )
                )
            if metric.fan_in >= HIGH_FAN_IN_THRESHOLD:
                risks.append(
                    ArchitectureRisk(
                        risk_type=ArchitectureRiskType.HIGH_FAN_IN,
                        subject=metric.fully_qualified_name,
                        description=(
                            f"Fan-in {metric.fan_in} meets threshold "
                            f"{HIGH_FAN_IN_THRESHOLD}."
                        ),
                    )
                )
            if metric.in_degree == 0 and metric.out_degree == 0:
                risks.append(
                    ArchitectureRisk(
                        risk_type=ArchitectureRiskType.ISOLATED_CLASS,
                        subject=metric.fully_qualified_name,
                        description="Class has no local dependency edges.",
                    )
                )
        for cycle in cycles:
            risks.append(
                ArchitectureRisk(
                    risk_type=ArchitectureRiskType.CYCLIC_DEPENDENCY,
                    subject=" -> ".join(cycle + (cycle[0],)),
                    description="Local dependency cycle detected.",
                )
            )
        for package_metric in package_metrics:
            if package_metric.external_edge_count:
                risks.append(
                    ArchitectureRisk(
                        risk_type=ArchitectureRiskType.CROSS_PACKAGE_COUPLING,
                        subject=package_metric.package,
                        description=(
                            f"Package participates in {package_metric.external_edge_count} "
                            "cross-package dependency edges."
                        ),
                    )
                )
        return sorted(
            risks,
            key=lambda risk: (risk.risk_type.value, risk.subject, risk.description),
        )

    @staticmethod
    def _components(graph: nx.MultiDiGraph[str]) -> list[ArchitectureComponent]:
        """Group each weak component by package for stable structural candidates."""
        groups: list[set[str]] = []
        for weak_component in nx.weakly_connected_components(graph):
            by_package: dict[str, set[str]] = defaultdict(set)
            for node in weak_component:
                by_package[graph.nodes[node]["package"]].add(node)
            groups.extend(by_package.values())

        ordered_groups = sorted(groups, key=lambda group: tuple(sorted(group)))
        components: list[ArchitectureComponent] = []
        for index, group in enumerate(ordered_groups, start=1):
            internal = 0
            external = 0
            for source, target, _key in graph.edges(keys=True):
                source_in = source in group
                target_in = target in group
                if source_in and target_in:
                    internal += 1
                elif source_in != target_in:
                    external += 1
            roles = [graph.nodes[node]["role"] for node in group]
            components.append(
                ArchitectureComponent(
                    component_id=f"component-{index:03d}",
                    classes=sorted(group),
                    packages=sorted({graph.nodes[node]["package"] for node in group}),
                    internal_edge_count=internal,
                    external_edge_count=external,
                    controller_count=roles.count(JavaClassRole.CONTROLLER.value),
                    service_count=roles.count(JavaClassRole.SERVICE.value),
                    repository_count=roles.count(JavaClassRole.REPOSITORY.value),
                )
            )
        return components

    @staticmethod
    def _role_relationship_count(
        graph: nx.MultiDiGraph[str], source_role: JavaClassRole, target_role: JavaClassRole
    ) -> int:
        return sum(
            1
            for source, target in graph.edges()
            if graph.nodes[source]["role"] == source_role.value
            and graph.nodes[target]["role"] == target_role.value
        )

    @staticmethod
    def _dot_for_graph(graph: nx.MultiDiGraph[str]) -> str:
        lines = ["digraph JavaDependencyGraph {", "  rankdir=LR;"]
        for node, attributes in sorted(graph.nodes(data=True)):
            label = f"{attributes['name']}\\n{attributes['role']}\\n{attributes['package']}"
            lines.append(f'  "{_dot_escape(node)}" [label="{_dot_escape(label)}"];')
        edges: list[tuple[str, str, str, str]] = []
        for source, target, attributes in graph.edges(data=True):
            edges.append((source, target, attributes["relationship"], attributes["evidence"]))
        for source, target, relationship, evidence in sorted(edges):
            label = relationship if not evidence else f"{relationship}\\n{evidence}"
            lines.append(
                f'  "{_dot_escape(source)}" -> "{_dot_escape(target)}" '
                f'[label="{_dot_escape(label)}"];'
            )
        lines.append("}")
        return "\n".join(lines) + "\n"

    _last_graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()


def _dot_escape(value: str) -> str:
    """Escape the small subset of characters relevant to quoted DOT strings."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "ARCHITECTURE_REPORT_ARTIFACT",
    "HIGH_FAN_IN_THRESHOLD",
    "HIGH_FAN_OUT_THRESHOLD",
    "JAVA_DEPENDENCY_DOT_ARTIFACT",
    "ArchitectureAnalysisAgent",
    "ArchitectureAnalysisError",
    "ArchitectureComponent",
    "ArchitectureReport",
    "ArchitectureRisk",
    "ArchitectureRiskType",
    "ClassArchitectureMetric",
    "InvalidArchitectureWorkspaceError",
    "PackageArchitectureMetric",
]
