"""Fail-closed, read-only checks for cross-artifact run consistency."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from migrationswarm.core.security import ArtifactCorruptionError, load_json_object


class ArtifactValidationFinding(BaseModel):
    """One validation finding suitable for concise CLI output."""

    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1)
    message: str = Field(min_length=1)
    path: str | None = None


class ArtifactValidationReport(BaseModel):
    """The result of a non-mutating repository artifact inspection."""

    model_config = ConfigDict(extra="forbid")

    repository: str
    run_id: UUID | None = None
    checked_files: list[str] = Field(default_factory=list)
    findings: list[ArtifactValidationFinding] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.findings


_UUID_RE = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE,
)
_PATH_KEYS = {
    "artifact",
    "artifact_path",
    "artifact_paths",
    "generated_files",
    "changed_files",
    "worktree_path",
    "repository_root",
    "metrics_artifact",
    "target_service_directory",
}


class ArtifactConsistencyValidator:
    """Validate existing artifacts without changing the repository."""

    def validate(
        self, repository: str | Path, *, run_id: UUID | None = None
    ) -> ArtifactValidationReport:
        root = Path(repository).expanduser().resolve()
        findings: list[ArtifactValidationFinding] = []
        checked: list[str] = []
        if not root.is_dir():
            findings.append(
                ArtifactValidationFinding(
                    category="repository", message="repository is not a directory"
                )
            )
            return ArtifactValidationReport(repository=str(root), run_id=run_id, findings=findings)
        metadata = root / ".migrationswarm"
        if not metadata.is_dir():
            findings.append(
                ArtifactValidationFinding(
                    category="artifacts", message=".migrationswarm directory is missing"
                )
            )
            return ArtifactValidationReport(repository=str(root), run_id=run_id, findings=findings)

        payloads: dict[str, dict[str, Any]] = {}
        for path in sorted(metadata.rglob("*.json"), key=lambda item: item.as_posix()):
            if "worktrees" in path.relative_to(metadata).parts:
                continue
            relative = path.relative_to(root).as_posix()
            checked.append(relative)
            try:
                payloads[relative] = load_json_object(path)
            except ArtifactCorruptionError as error:
                findings.append(
                    ArtifactValidationFinding(
                        category="artifact", message=str(error), path=relative
                    )
                )

        self._validate_paths(root, payloads, findings)
        selected_run = self._select_run(payloads, run_id)
        if run_id is not None and selected_run is None:
            findings.append(
                ArtifactValidationFinding(category="run", message=f"run was not found: {run_id}")
            )
        if selected_run is not None:
            self._validate_run_lineage(selected_run, payloads, findings)
        self._validate_references(root, payloads, findings)
        return ArtifactValidationReport(
            repository=str(root),
            run_id=selected_run or run_id,
            checked_files=checked,
            findings=findings,
        )

    @staticmethod
    def _select_run(payloads: dict[str, dict[str, Any]], run_id: UUID | None) -> UUID | None:
        candidates: list[tuple[str, UUID]] = []
        for path, payload in payloads.items():
            if "/multi-runs/" not in f"/{path}" and "/metrics/" not in f"/{path}":
                continue
            value = payload.get("run_id")
            try:
                candidate = UUID(str(value)) if value is not None else None
            except ValueError:
                candidate = None
            if candidate is not None and (run_id is None or candidate == run_id):
                timestamp = str(
                    payload.get(
                        "started_at", payload.get("created_at", payload.get("completed_at", ""))
                    )
                )
                candidates.append((timestamp, candidate))
        return max(candidates, key=lambda item: (item[0], str(item[1])))[1] if candidates else None

    @staticmethod
    def _validate_paths(
        root: Path,
        payloads: dict[str, dict[str, Any]],
        findings: list[ArtifactValidationFinding],
    ) -> None:
        def visit(value: Any, key: str, source: str) -> None:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    visit(child_value, str(child_key), source)
            elif isinstance(value, list):
                for child in value:
                    visit(child, key, source)
            elif isinstance(value, str) and key in _PATH_KEYS and ("/" in value or "\\" in value):
                candidate = Path(value)
                if candidate.is_absolute() and key in {"repository_root", "worktree_path"}:
                    # These are historical execution locations, not write targets. They may
                    # be unavailable after a demo or restart, but must not contain traversal.
                    return
                if candidate.is_absolute() or ".." in candidate.parts:
                    findings.append(
                        ArtifactValidationFinding(
                            category="path_containment",
                            message="artifact path is absolute or traverses outside the repository",
                            path=source,
                        )
                    )
                    return
                try:
                    resolved = (root / candidate).resolve()
                    resolved.relative_to(root)
                except ValueError:
                    findings.append(
                        ArtifactValidationFinding(
                            category="path_containment",
                            message="artifact path escapes repository",
                            path=source,
                        )
                    )

        for source, payload in payloads.items():
            visit(payload, "", source)

    @staticmethod
    def _validate_run_lineage(
        run_id: UUID,
        payloads: dict[str, dict[str, Any]],
        findings: list[ArtifactValidationFinding],
    ) -> None:
        run_payloads = [
            payload for payload in payloads.values() if str(payload.get("run_id")) == str(run_id)
        ]
        if not run_payloads:
            findings.append(
                ArtifactValidationFinding(
                    category="run_lineage", message="selected run has no matching artifacts"
                )
            )
            return
        services: set[str] = set()
        task_ids: set[str] = set()
        task_sources = [
            payload
            for path, payload in payloads.items()
            if "/multi-runs/" in f"/{path}" or payload in run_payloads
        ]
        for payload in task_sources:
            for service in payload.get("selected_services", []):
                if isinstance(service, str):
                    services.add(service.casefold())
            for service in payload.get("services", payload.get("service_states", [])):
                if isinstance(service, dict):
                    name = service.get("service_name")
                    if isinstance(name, str):
                        services.add(name.casefold())
                    for task_id in service.get("task_ids", []):
                        task_ids.add(str(task_id))
        assignments: dict[str, str] = {}
        for payload in run_payloads:
            states = payload.get("services", payload.get("service_states", []))
            for service in states:
                if not isinstance(service, dict):
                    continue
                service_name = str(service.get("service_name", ""))
                for task_id in service.get("task_ids", []):
                    previous = assignments.setdefault(str(task_id), service_name)
                    if previous != service_name:
                        findings.append(
                            ArtifactValidationFinding(
                                category="duplicate_references",
                                message="task is assigned to multiple services",
                            )
                        )
                for dependency in service.get("dependencies", []):
                    if str(dependency).casefold() not in services:
                        findings.append(
                            ArtifactValidationFinding(
                                category="service_references",
                                message="service dependency is not present in the selected run",
                            )
                        )
        for path, payload in payloads.items():
            if payload.get("run_id") is not None and str(payload.get("run_id")) != str(run_id):
                continue
            candidate = payload.get("selected_candidate", {})
            if isinstance(candidate, dict):
                name = candidate.get("name")
                if isinstance(name, str) and services and name.casefold() not in services:
                    findings.append(
                        ArtifactValidationFinding(
                            category="service_references",
                            message="extraction candidate is absent from the selected run",
                            path=path,
                        )
                    )
            value = payload.get("task_id")
            if value is not None and task_ids and str(value) not in task_ids:
                findings.append(
                    ArtifactValidationFinding(
                        category="task_references",
                        message="artifact task_id is not present in the selected run",
                        path=path,
                    )
                )

    @staticmethod
    def _validate_references(
        root: Path,
        payloads: dict[str, dict[str, Any]],
        findings: list[ArtifactValidationFinding],
    ) -> None:
        known_tasks: set[str] = set()
        for path, payload in payloads.items():
            for match in _UUID_RE.findall(path):
                known_tasks.add(match)
            for key in ("task_id", "original_task_id", "debug_task_id"):
                value = payload.get(key)
                if value is not None:
                    known_tasks.add(str(value))
        for path, payload in payloads.items():
            for key in ("task_id", "original_task_id", "debug_task_id"):
                value = payload.get(key)
                if value is not None:
                    try:
                        UUID(str(value))
                    except ValueError:
                        findings.append(
                            ArtifactValidationFinding(
                                category="task_references",
                                message=f"invalid UUID in {key}",
                                path=path,
                            )
                        )
            repair = payload.get("original_task_id")
            if repair is not None and str(repair) not in known_tasks:
                findings.append(
                    ArtifactValidationFinding(
                        category="repair_lineage",
                        message="repair references an unknown original task",
                        path=path,
                    )
                )
            filename_ids = _UUID_RE.findall(Path(path).name)
            task_artifact = any(
                marker in f"/{path}"
                for marker in ("/extraction-results/", "/verification-results/")
            )
            if (
                task_artifact
                and filename_ids
                and value is not None
                and str(value) != filename_ids[0]
            ):
                findings.append(
                    ArtifactValidationFinding(
                        category="task_references",
                        message="artifact filename task ID does not match payload task_id",
                        path=path,
                    )
                )
            for key in ("artifact_path", "metrics_artifact"):
                reference = payload.get(key)
                if isinstance(reference, str):
                    ArtifactConsistencyValidator._check_referenced_file(
                        root, reference, path, findings
                    )
            references = payload.get("artifact_paths", [])
            if isinstance(references, list):
                for reference in references:
                    if isinstance(reference, str):
                        ArtifactConsistencyValidator._check_referenced_file(
                            root, reference, path, findings
                        )
            steps = payload.get("steps")
            if isinstance(steps, list):
                step_ids = {
                    str(step.get("step_id"))
                    for step in steps
                    if isinstance(step, dict) and step.get("step_id") is not None
                }
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    for dependency in step.get("dependencies", []):
                        if str(dependency) not in step_ids:
                            findings.append(
                                ArtifactValidationFinding(
                                    category="task_references",
                                    message="plan step dependency is unknown",
                                    path=path,
                                )
                            )

    @staticmethod
    def _check_referenced_file(
        root: Path,
        reference: str,
        source: str,
        findings: list[ArtifactValidationFinding],
    ) -> None:
        candidate = Path(reference)
        if candidate.is_absolute() or ".." in candidate.parts:
            return
        if not (root / candidate).is_file():
            findings.append(
                ArtifactValidationFinding(
                    category="artifact_lineage",
                    message="referenced artifact does not exist",
                    path=source,
                )
            )


__all__ = ["ArtifactConsistencyValidator", "ArtifactValidationFinding", "ArtifactValidationReport"]
