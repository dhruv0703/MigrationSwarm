"""Bounded release-candidate validation using existing MigrationSwarm commands."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from migrationswarm import __version__
from migrationswarm.core.security import write_json_atomic, write_text_atomic

_UUID_TEXT = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def _release_environment() -> dict[str, str]:
    """Return the bounded environment used by release-only subprocess checks."""
    environment = os.environ.copy()
    environment["MAVEN_OPTS"] = "-Xmx256m"
    environment["MIGRATIONSWARM_RELEASE_SERVICE_WORKERS"] = "1"
    return environment


class ReleaseValidation:
    """Run and summarize safe release checks without exposing command output."""

    def __init__(self, root: Path, *, full: bool, skip_expensive: bool, skip_soak: bool) -> None:
        self.root = root.resolve()
        self.full = full
        self.skip_expensive = skip_expensive
        self.skip_soak = skip_soak
        self.checks: list[dict[str, Any]] = []

    def command(self, name: str, args: list[str], *, timeout: int = 900) -> bool:
        try:
            result = subprocess.run(
                [sys.executable, *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
                env=_release_environment(),
            )
            passed = result.returncode == 0
            detail = "passed" if passed else f"exit_code={result.returncode}"
        except (OSError, subprocess.TimeoutExpired) as error:
            passed = False
            detail = type(error).__name__
        self.checks.append(
            {"name": name, "status": "passed" if passed else "failed", "detail": detail}
        )
        return passed

    def run(self) -> bool:
        self.command("ruff", ["-m", "ruff", "check", "."], timeout=300)
        self.command("mypy", ["-m", "mypy"], timeout=300)
        self.command("pytest", ["-m", "pytest"], timeout=900)
        self.command(
            "security-check", ["-m", "migrationswarm.cli.main", "security-check"], timeout=120
        )
        self.command("clean-install", ["scripts/validate_clean_install.py"], timeout=900)
        if not self.skip_expensive:
            self.command(
                "benchmark", ["-m", "migrationswarm.cli.main", "benchmark", "--all"], timeout=1800
            )
            self.command(
                "maven-fixtures",
                ["scripts/validate_release_candidate.py", "--fixtures-only"],
                timeout=1800,
            )
        if self.full:
            self.repeatability()
            if not self.skip_soak:
                self.soak()
            if not self.skip_expensive:
                self.command(
                    "performance",
                    ["-m", "migrationswarm.cli.main", "perf", "--offline", "--iterations", "3"],
                    timeout=1800,
                )
                self.command("package-build", ["-m", "build"], timeout=600)
                self.command("pip-check", ["-m", "pip", "check"], timeout=300)
        return all(item["status"] == "passed" for item in self.checks)

    def repeatability(self) -> None:
        fixture = self.root / "examples" / "demo-commerce-monolith"
        if not fixture.is_dir():
            self.checks.append(
                {"name": "repeatability", "status": "failed", "detail": "fixture missing"}
            )
            return
        normalized_runs: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="migrationswarm-release-") as temporary:
            for index in range(3):
                destination = Path(temporary) / f"run-{index}"
                shutil.copytree(
                    fixture,
                    destination,
                    ignore=shutil.ignore_patterns("target", ".migrationswarm", ".git"),
                )
                demo_passed = self._run_demo(destination)
                self.checks.append(
                    {
                        "name": f"demo-{index + 1}",
                        "status": "passed" if demo_passed else "failed",
                        "detail": "offline commerce demo",
                    }
                )
                if not demo_passed:
                    return
                normalized_runs.append(self._normalized_artifacts(destination / ".migrationswarm"))
            passed = normalized_runs[1:] == normalized_runs[:1] * 2
            self.checks.append(
                {
                    "name": "repeatability",
                    "status": "passed" if passed else "failed",
                    "detail": "three normalized offline runs",
                }
            )

    def soak(self) -> None:
        fixture = self.root / "examples" / "demo-commerce-monolith"
        if not fixture.is_dir():
            self.checks.append(
                {"name": "multi-service-soak", "status": "failed", "detail": "fixture missing"}
            )
            return
        with tempfile.TemporaryDirectory(prefix="migrationswarm-soak-") as temporary:
            results: list[bool] = []
            for index in range(5):
                destination = Path(temporary) / f"iteration-{index}"
                shutil.copytree(
                    fixture,
                    destination,
                    ignore=shutil.ignore_patterns("target", ".migrationswarm", ".git"),
                )
                results.append(self._run_demo(destination))
            passed = all(results)
        self.checks.append(
            {
                "name": "multi-service-soak",
                "status": "passed" if passed else "failed",
                "detail": "five bounded offline iterations",
            }
        )

    def _run_demo(self, destination: Path) -> bool:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "migrationswarm.cli.main",
                "demo",
                str(destination),
                "--offline",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            shell=False,
            env=_release_environment(),
        )
        if completed.returncode != 0:
            return False
        report = subprocess.run(
            [sys.executable, "-m", "migrationswarm.cli.main", "validate-run", str(destination)],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            shell=False,
            env=_release_environment(),
        )
        self.checks.append(
            {
                "name": "validate-run",
                "status": "passed" if report.returncode == 0 else "failed",
                "detail": "offline demo artifact check",
            }
        )
        return report.returncode == 0

    @staticmethod
    def _normalized_artifacts(metadata: Path) -> dict[str, Any]:
        def normalize(value: Any, key: str = "") -> Any:
            if key.endswith(("_ms", "_seconds")):
                return "<measurement>"
            if key in {"stdout_summary", "stderr_summary"}:
                return "<bounded-build-output>"
            if isinstance(value, dict):
                return {
                    item: normalize(child, item)
                    for item, child in sorted(value.items())
                    if item
                    not in {
                        "run_id",
                        "started_at",
                        "completed_at",
                        "created_at",
                        "updated_at",
                        "generated_at",
                        "occurred_at",
                        "recorded_at",
                        "decided_at",
                        "duration_ms",
                        "elapsed_ms",
                    }
                }
            if isinstance(value, list):
                items = [normalize(child, key) for child in value]
                return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
            if isinstance(value, str):
                if "repository" in key or key.endswith("_path"):
                    return "<path>"
                value = _UUID_TEXT.sub("<uuid>", value)
                return re.sub(r"migrationswarm-(?:demo|release|soak)-[^\\/]+", "<temporary>", value)
            return value

        result: dict[str, Any] = {}

        def add(relative: str, value: Any) -> None:
            existing = result.get(relative)
            if existing is None:
                result[relative] = value
            elif isinstance(existing, list):
                existing.append(value)
                existing.sort(key=lambda item: json.dumps(item, sort_keys=True))
            else:
                result[relative] = sorted(
                    [existing, value], key=lambda item: json.dumps(item, sort_keys=True)
                )

        for path in sorted(metadata.rglob("*.json")):
            try:
                relative = _UUID_TEXT.sub("<uuid>", path.relative_to(metadata).as_posix())
                add(relative, normalize(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                relative = _UUID_TEXT.sub("<uuid>", path.relative_to(metadata).as_posix())
                add(relative, "<corrupt>")
        return result


def validate_fixtures(root: Path) -> int:
    """Run Maven tests in committed fixtures for the harness itself."""
    maven = shutil.which("mvn")
    if maven is None:
        return 1
    for fixture in sorted(
        path
        for path in (root / "examples").iterdir()
        if (path / "pom.xml").is_file() and (path / "benchmark.json").is_file()
    ):
        command = [maven, "test", "-q"]
        if maven.lower().endswith((".cmd", ".bat")):
            command = ["cmd.exe", "/d", "/s", "/c", *command]
        result = subprocess.run(
            command,
            cwd=fixture,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            shell=False,
            env=_release_environment(),
        )
        if result.returncode != 0:
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--skip-expensive", action="store_true")
    parser.add_argument("--skip-soak", action="store_true")
    parser.add_argument("--fixtures-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.fixtures_only:
        return validate_fixtures(root)
    validator = ReleaseValidation(
        root, full=args.full, skip_expensive=args.skip_expensive, skip_soak=args.skip_soak
    )
    passed = validator.run()
    run_id = uuid4()
    output = root / "artifacts" / "release-validation" / str(run_id)
    report = {
        "run_id": str(run_id),
        "version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "full" if args.full else "bounded",
        "status": "READY_FOR_LIVE_VALIDATION" if passed else "NOT_READY",
        "tests": _section(validator.checks, {"ruff", "mypy", "pytest"}),
        "fixtures": _section(validator.checks, {"maven-fixtures"}),
        "demo": _section(validator.checks, {"demo-1", "demo-2", "demo-3"}),
        "evaluation": _section(validator.checks, {"benchmark"}),
        "performance": _section(validator.checks, {"performance"}),
        "security": _section(validator.checks, {"security-check"}),
        "repeatability": _section(validator.checks, {"repeatability"}),
        "recovery": {"status": "manual_only"},
        "artifacts": _section(validator.checks, {"validate-run"}),
        "packaging": _section(validator.checks, {"clean-install", "package-build", "pip-check"}),
        "safety": {
            "main_repository_modified": False,
            "automatic_commits": False,
            "automatic_pushes": False,
            "automatic_reset_or_clean": False,
        },
        "final": "READY_FOR_LIVE_VALIDATION" if passed else "NOT_READY",
        "checks": validator.checks,
        "scope": "release-candidate validation; not a production-readiness certification",
    }
    write_json_atomic(output / "summary.json", report)
    lines = [
        f"# MigrationSwarm release validation {run_id}",
        "",
        f"- Version: {__version__}",
        f"- Status: {report['status']}",
        "",
    ]
    lines.extend(
        f"- {item['name']}: {item['status']} ({item['detail']})" for item in validator.checks
    )
    write_text_atomic(output / "summary.md", "\n".join(lines) + "\n")
    print(f"report={output.relative_to(root).as_posix()}")
    print(f"status={report['status']}")
    return 0 if passed else 1


def _section(checks: list[dict[str, Any]], names: set[str]) -> dict[str, Any]:
    """Return a stable, secret-free section for the release report."""
    values = [item for item in checks if item["name"] in names]
    status = "not_run"
    if values:
        status = "passed" if all(item["status"] == "passed" for item in values) else "failed"
    return {"status": status, "checks": values}


if __name__ == "__main__":
    raise SystemExit(main())
