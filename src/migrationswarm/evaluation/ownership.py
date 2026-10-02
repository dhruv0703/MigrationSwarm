"""Deterministic checks for bounded dependency and persistence ownership."""

from __future__ import annotations

import re
from pathlib import Path

from migrationswarm.agents.dependency_analysis import JavaClassRole, JavaDependencyGraph
from migrationswarm.agents.service_boundary import ServiceBoundaryReport
from migrationswarm.evaluation.matching import normalize_service_name
from migrationswarm.evaluation.models import OwnershipReport


def analyze_boundary_ownership(
    boundary: ServiceBoundaryReport,
    expected_services: list[str],
) -> OwnershipReport:
    """Inspect candidate boundaries for obvious duplicate or leaked repositories."""
    expected_keys = {normalize_service_name(item) for item in expected_services}
    selected = [
        candidate
        for candidate in boundary.candidate_services
        if normalize_service_name(candidate.name) in expected_keys
    ]
    findings: list[str] = []
    owners: dict[str, list[str]] = {}
    for candidate in selected:
        for class_name in candidate.classes:
            owners.setdefault(class_name, []).append(candidate.name)
        package_keys = {package.casefold() for package in candidate.packages}
        for repository in candidate.repositories:
            package = repository.rsplit(".", 1)[0].casefold()
            if package not in package_keys:
                findings.append(
                    f"Repository {repository} is outside candidate package ownership "
                    f"for {candidate.name}."
                )
    for class_name, candidate_names in sorted(owners.items()):
        if len(candidate_names) > 1:
            findings.append(
                f"Class {class_name} is assigned to multiple candidates: "
                + ", ".join(sorted(candidate_names))
            )
    duplicate_repositories: dict[str, list[str]] = {}
    for candidate in selected:
        for repository in candidate.repositories:
            duplicate_repositories.setdefault(repository, []).append(candidate.name)
    for repository, candidate_names in sorted(duplicate_repositories.items()):
        if len(candidate_names) > 1:
            findings.append(
                f"Writable repository {repository} is copied by multiple services: "
                + ", ".join(sorted(candidate_names))
            )
    return OwnershipReport(
        cross_service_dependency_count=sum(
            len(set(candidate.dependencies_on_other_candidates)) for candidate in selected
        ),
        unresolved_dependency_count=len(boundary.unresolved_classes),
        shared_component_count=len(boundary.shared_components),
        ownership_violation_count=len(findings),
        findings=findings,
    )


def inspect_generated_services(
    root: str | Path,
    *,
    source_package_root: str | None = None,
) -> OwnershipReport:
    """Check generated service files for source-root leakage and duplicate types."""
    repository = Path(root).expanduser().resolve()
    generated_root = repository / ".migrationswarm" / "demo-services"
    findings: list[str] = []
    type_owners: dict[str, list[str]] = {}
    if not generated_root.is_dir():
        return OwnershipReport(findings=["Generated service output directory is missing."])
    for path in sorted(generated_root.rglob("*.java"), key=lambda item: item.as_posix()):
        owner = path.relative_to(generated_root).parts[0]
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(f"Generated Java file could not be read: {path.name}")
            continue
        if str(repository) in content:
            findings.append(f"Generated file contains an absolute source path: {path.name}")
        if re.search(r"(?:^|\n)\s*import\s+[^;]*\.src\.", content):
            findings.append(f"Generated file imports a source fixture path: {path.name}")
        package_match = re.search(r"\bpackage\s+([\w.]+)\s*;", content)
        type_match = re.search(r"\b(?:class|interface|enum|record)\s+(\w+)", content)
        if package_match and source_package_root:
            own_group = _package_group(package_match.group(1), source_package_root)
            for imported in re.findall(r"\bimport\s+([\w.]+)\s*;", content):
                if not imported.startswith(source_package_root + "."):
                    continue
                imported_group = _package_group(imported.rsplit(".", 1)[0], source_package_root)
                if imported_group is not None and imported_group != own_group:
                    findings.append(
                        f"Generated file {path.name} imports another source domain: {imported}."
                    )
        if package_match and re.search(
            r"\bimport\s+[\w.]+\.(?:internal|implementation|impl)\.", content
        ):
            findings.append(
                f"Generated file {path.name} imports another generated implementation package."
            )
        if "orchestrat" in content.casefold() and "@SpringBootApplication" not in content:
            findings.append(
                f"Generated file {path.name} may include unrelated orchestration code."
            )
        if package_match and type_match:
            fully_qualified = f"{package_match.group(1)}.{type_match.group(1)}"
            type_owners.setdefault(fully_qualified, []).append(owner)
    for type_name, owners in sorted(type_owners.items()):
        if len(set(owners)) > 1:
            findings.append(
                f"Generated type {type_name} appears in multiple services: "
                + ", ".join(sorted(set(owners)))
            )
    return OwnershipReport(
        ownership_violation_count=len(findings),
        findings=findings,
    )


def analyze_entity_ownership(
    boundary: ServiceBoundaryReport,
    graph: JavaDependencyGraph,
) -> OwnershipReport:
    """Separate read-only entity references from duplicate persistence ownership."""
    candidate_by_class = {
        class_name: candidate.name
        for candidate in boundary.candidate_services
        for class_name in candidate.classes
    }
    classes = {item.fully_qualified_name: item for item in graph.classes}
    references: set[tuple[str, str]] = set()
    for edge in graph.dependencies:
        source = candidate_by_class.get(edge.source)
        target = classes.get(edge.target)
        target_owner = candidate_by_class.get(edge.target)
        if (
            source is not None
            and target is not None
            and target_owner is not None
            and source != target_owner
            and target.role is JavaClassRole.ENTITY
        ):
            references.add((source, edge.target))
    entity_owners: dict[str, set[str]] = {}
    for candidate in boundary.candidate_services:
        for class_name in candidate.classes:
            java_class = classes.get(class_name)
            if java_class is not None and java_class.role is JavaClassRole.ENTITY:
                entity_owners.setdefault(class_name, set()).add(candidate.name)
    duplicate_entities = {
        name: owners for name, owners in entity_owners.items() if len(owners) > 1
    }
    shared_values = {
        item.class_name
        for item in boundary.shared_components
        if item.class_name.rsplit(".", 1)[-1].casefold()
        in {"money", "address", "auditrecord", "auditstamp"}
    }
    findings = [
        f"Entity {name} is assigned to multiple candidates: "
        + ", ".join(sorted(owners, key=str.casefold))
        for name, owners in sorted(duplicate_entities.items())
    ]
    return OwnershipReport(
        entity_reference_count=len(references),
        shared_value_object_count=len(shared_values),
        ambiguous_entity_ownership_count=len(duplicate_entities),
        duplicate_mutable_entity_count=len(duplicate_entities),
        ownership_violation_count=len(duplicate_entities),
        findings=findings,
    )


def _package_group(package: str, package_root: str) -> str | None:
    prefix = package_root + "."
    if not package.startswith(prefix):
        return None
    remainder = package.removeprefix(prefix).split(".")
    return remainder[0] if remainder else None


__all__ = [
    "analyze_boundary_ownership",
    "analyze_entity_ownership",
    "inspect_generated_services",
]
