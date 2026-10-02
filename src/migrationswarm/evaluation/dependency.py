"""Deterministic service-dependency, SCC, and migration-order analysis."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from migrationswarm.agents.dependency_analysis import (
    JavaClass,
    JavaClassRole,
    JavaDependencyGraph,
)
from migrationswarm.agents.service_boundary import ServiceBoundaryReport
from migrationswarm.evaluation.models import (
    DependencyComponent,
    HumanReviewFinding,
    MigrationOrdering,
    ServiceDependencyAnalysis,
    ServiceDependencyEdge,
)


def strongly_connected_components(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> list[tuple[str, ...]]:
    """Return deterministic Tarjan SCCs for edges where source depends on target."""
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    for source, target in edges:
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set())

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(adjacency[node], key=str.casefold):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        members: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            members.append(member)
            if member == node:
                break
        components.append(tuple(sorted(members, key=str.casefold)))

    for node in sorted(adjacency, key=str.casefold):
        if node not in indices:
            visit(node)
    return sorted(components, key=lambda component: tuple(item.casefold() for item in component))


def dependency_order(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> list[str]:
    """Return prerequisite-first order for an acyclic dependency graph."""
    names = set(nodes)
    dependencies: dict[str, set[str]] = {name: set() for name in names}
    dependents: dict[str, set[str]] = {name: set() for name in names}
    for source, target in edges:
        names.update((source, target))
        dependencies.setdefault(source, set()).add(target)
        dependencies.setdefault(target, set())
        dependents.setdefault(target, set()).add(source)
        dependents.setdefault(source, set())
    ready = sorted((name for name in names if not dependencies[name]), key=str.casefold)
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for dependent in sorted(dependents[current], key=str.casefold):
            dependencies[dependent].discard(current)
            if not dependencies[dependent] and dependent not in ordered and dependent not in ready:
                ready.append(dependent)
        ready.sort(key=str.casefold)
    return ordered


def continuation_status(
    category: str,
) -> Literal["safe", "review_required", "blocked"]:
    """Apply the explicit safe-continuation policy for a finding category."""
    if category in {"shared_stateless_utility", "read_only_dto", "cross_service_contract"}:
        return "safe"
    if category in {
        "circular_dependency",
        "foreign_repository_dependency",
        "cross_domain_transaction",
        "ambiguous_entity_ownership",
        "shared_mutable_state",
        "ownership_review",
    }:
        return "review_required"
    return "blocked"


def analyze_service_dependencies(
    boundary: ServiceBoundaryReport,
    graph: JavaDependencyGraph,
) -> ServiceDependencyAnalysis:
    """Analyze service edges, cycles, foreign repositories, and transactions."""
    class_owners: dict[str, str] = {}
    class_lookup = {item.fully_qualified_name: item for item in graph.classes}
    candidate_by_class: dict[str, str] = {}
    candidate_classes: dict[str, set[str]] = {}
    for candidate in boundary.candidate_services:
        candidate_classes[candidate.name] = set(candidate.classes)
        for class_name in candidate.classes:
            class_owners[class_name] = candidate.name
            candidate_by_class[class_name] = candidate.name

    edge_keys: set[tuple[str, str, str]] = set()
    foreign: set[tuple[str, str]] = set()
    service_edges: set[tuple[str, str]] = set()
    for edge in graph.dependencies:
        source_service = candidate_by_class.get(edge.source)
        target_service = candidate_by_class.get(edge.target)
        if source_service is None or target_service is None or source_service == target_service:
            continue
        source_class = class_lookup.get(edge.source)
        target_class = class_lookup.get(edge.target)
        relationship = "cross_service_contract"
        if (
            source_class is not None
            and source_class.role is JavaClassRole.SERVICE
            and target_class is not None
            and target_class.role is JavaClassRole.REPOSITORY
        ):
            relationship = "foreign_repository"
            foreign.add((source_service, target_service))
        edge_keys.add((source_service, target_service, relationship))
        service_edges.add((source_service, target_service))

    names = sorted(candidate_classes, key=str.casefold)
    components = strongly_connected_components(names, service_edges)
    cyclic = [
        component
        for component in components
        if len(component) > 1 or _has_self_edge(component, service_edges)
    ]
    blocked = _blocked_by_cycles(names, service_edges, cyclic)
    recommended = [name for name in dependency_order(names, service_edges) if name not in blocked]
    ordering = MigrationOrdering(
        services=names,
        dependencies=[
            ServiceDependencyEdge(source=source, target=target, relationship=relationship)
            for source, target, relationship in sorted(
                edge_keys, key=lambda item: (item[0].casefold(), item[1].casefold(), item[2])
            )
        ],
        components=[
            DependencyComponent(services=list(component), cyclic=component in cyclic)
            for component in components
        ],
        cyclic_components=[list(component) for component in cyclic],
        recommended_order=recommended,
        blocked_services=blocked,
    )

    findings: list[HumanReviewFinding] = []
    for component in cyclic:
        evidence = [
            f"{source} -> {target}"
            for source, target in sorted(
                service_edges,
                key=lambda item: (item[0].casefold(), item[1].casefold()),
            )
            if source in component and target in component
        ]
        findings.append(
            HumanReviewFinding(
                category="circular_dependency",
                source_service=component[0],
                affected_target=" / ".join(component),
                evidence=evidence or ["Strongly connected service component."],
                impact="review_required",
                can_continue_safely=False,
                recommended_review_focus=(
                    "Choose a contract, inversion, or coordination seam before extraction."
                ),
            )
        )
    for source, target in sorted(
        foreign, key=lambda item: (item[0].casefold(), item[1].casefold())
    ):
        findings.append(
            HumanReviewFinding(
                category="foreign_repository_dependency",
                source_service=source,
                affected_target=target,
                evidence=[f"{source} directly references a repository owned by {target}."],
                impact="review_required",
                can_continue_safely=False,
                recommended_review_focus=(
                    "Replace direct persistence access with an explicit data or service contract."
                ),
            )
        )

    transaction_findings = _transaction_findings(graph, candidate_by_class, class_lookup)
    findings.extend(transaction_findings)
    for class_name, owners in _duplicate_entity_owners(graph, candidate_by_class).items():
        findings.append(
            HumanReviewFinding(
                category="ambiguous_entity_ownership",
                source_service=owners[0],
                affected_target=class_name,
                evidence=["The mutable entity is assigned to multiple candidate services."],
                impact="blocked",
                can_continue_safely=False,
                recommended_review_focus=(
                    "Select one persistence owner; keep other references read-only or contractual."
                ),
            )
        )
    if boundary.unresolved_classes:
        findings.append(
            HumanReviewFinding(
                category="unresolved_data_dependency",
                source_service="boundary-analysis",
                affected_target="unresolved classes",
                evidence=sorted(boundary.unresolved_classes),
                impact="blocked",
                can_continue_safely=False,
                recommended_review_focus=(
                    "Resolve the missing type or explicitly approve a human-owned treatment."
                ),
            )
        )
    return ServiceDependencyAnalysis(
        ordering=ordering,
        foreign_repository_dependencies=[
            ServiceDependencyEdge(
                source=source, target=target, relationship="foreign_repository"
            )
            for source, target in sorted(
                foreign, key=lambda item: (item[0].casefold(), item[1].casefold())
            )
        ],
        cross_domain_transaction_count=len(transaction_findings),
        review_findings=findings,
    )


def _has_self_edge(component: tuple[str, ...], edges: set[tuple[str, str]]) -> bool:
    return len(component) == 1 and (component[0], component[0]) in edges


def _blocked_by_cycles(
    names: list[str], edges: set[tuple[str, str]], cycles: list[tuple[str, ...]]
) -> list[str]:
    blocked = {name for component in cycles for name in component}
    changed = True
    while changed:
        changed = False
        for source, target in edges:
            if target in blocked and source not in blocked:
                blocked.add(source)
                changed = True
    return sorted((name for name in names if name in blocked), key=str.casefold)


def _transaction_findings(
    graph: JavaDependencyGraph,
    candidate_by_class: dict[str, str],
    class_lookup: dict[str, JavaClass],
) -> list[HumanReviewFinding]:
    repository_edges: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for edge in graph.dependencies:
        source = class_lookup.get(edge.source)
        target = class_lookup.get(edge.target)
        source_service = candidate_by_class.get(edge.source)
        target_service = candidate_by_class.get(edge.target)
        if (
            source is not None
            and target is not None
            and source.role is JavaClassRole.SERVICE
            and target.role is JavaClassRole.REPOSITORY
            and source_service is not None
            and target_service is not None
        ):
            repository_edges[source.fully_qualified_name].add(
                (target_service, target.fully_qualified_name)
            )
    findings: list[HumanReviewFinding] = []
    for class_name, repositories in sorted(repository_edges.items()):
        if len({service for service, _ in repositories}) < 2:
            continue
        source_path = Path(graph.repository_root) / class_lookup[class_name].file_path
        try:
            content = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "@Transactional" not in content:
            continue
        services = sorted({service for service, _ in repositories}, key=str.casefold)
        findings.append(
            HumanReviewFinding(
                category="cross_domain_transaction",
                source_service=candidate_by_class[class_name],
                affected_target=" / ".join(services),
                evidence=[
                    f"{class_name} is @Transactional and references: "
                    + ", ".join(sorted(repository for _, repository in repositories))
                ],
                impact="review_required",
                can_continue_safely=False,
                recommended_review_focus=(
                    "Define an explicit consistency and compensation strategy "
                    "before splitting writes."
                ),
            )
        )
    return findings


def _duplicate_entity_owners(
    graph: JavaDependencyGraph, candidate_by_class: dict[str, str]
) -> dict[str, list[str]]:
    owners: dict[str, set[str]] = defaultdict(set)
    for class_name, service in candidate_by_class.items():
        java_class = next(
            (item for item in graph.classes if item.fully_qualified_name == class_name), None
        )
        if java_class is not None and java_class.role is JavaClassRole.ENTITY:
            owners[class_name].add(service)
    return {
        class_name: sorted(values, key=str.casefold)
        for class_name, values in owners.items()
        if len(values) > 1
    }


__all__ = [
    "analyze_service_dependencies",
    "continuation_status",
    "dependency_order",
    "strongly_connected_components",
]
