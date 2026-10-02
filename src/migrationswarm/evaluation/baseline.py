"""Simple package-grouping baseline for boundary discovery."""

import re
from pathlib import Path

from migrationswarm.evaluation.matching import boundary_metrics
from migrationswarm.evaluation.models import BaselineResult, FixtureMetadata


def _package_for(path: Path) -> str | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("package ") and stripped.endswith(";"):
                return stripped.removeprefix("package ").removesuffix(";").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _display_name(segment: str) -> str:
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", segment)
    return " ".join(word.capitalize() for word in words) or segment.capitalize()


def discover_package_services(fixture: str | Path, metadata: FixtureMetadata) -> list[str]:
    """Group Java classes by the first package segment below package_root."""
    root = Path(fixture).expanduser().resolve()
    source_root = root / "src" / "main" / "java"
    groups: set[str] = set()
    package_prefix = f"{metadata.package_root}."
    ignored = {item.casefold() for item in metadata.ignored_package_segments}
    for java_file in sorted(source_root.rglob("*.java")):
        package = _package_for(java_file)
        if package is None or not package.startswith(package_prefix):
            continue
        remainder = package.removeprefix(package_prefix).split(".")
        if remainder and remainder[0].casefold() not in ignored:
            groups.add(remainder[0])
    return [_display_name(item) for item in sorted(groups, key=str.casefold)]


def discover_package_baseline(
    fixture: str | Path, metadata: FixtureMetadata, *, build_passed: bool | None = None
) -> BaselineResult:
    """Return the package baseline and evaluate it with the common matcher."""
    predicted = discover_package_services(fixture, metadata)
    return BaselineResult(
        predicted_services=predicted,
        boundary=boundary_metrics(metadata.expected_services, predicted, metadata.aliases),
        build_attempted=build_passed is not None,
        build_passed=build_passed,
        tests_attempted=build_passed is not None,
        tests_passed=build_passed,
    )
