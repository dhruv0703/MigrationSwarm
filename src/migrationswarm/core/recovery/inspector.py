"""Deterministic inspection of potentially interrupted durable work."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from migrationswarm.core.tasks import Task, TaskStatus


class RecoveryRecommendation(StrEnum):
    """Read-only recommendations; none of these actions is performed automatically."""

    NO_ACTION = "no_action"
    ACTIVE = "active"
    HUMAN_REVIEW = "human_review"


class RecoveryFinding(BaseModel):
    """One durable task requiring inspection after a possible process interruption."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    status: TaskStatus
    recommendation: RecoveryRecommendation
    reason: str = Field(min_length=1)
    worktree_preserved: bool = False
    active_heartbeat: bool = False
    lock_present: bool = False


class RecoveryInspector:
    """Inspect task/heartbeat/lock state without changing durable or transient state."""

    def inspect(
        self,
        tasks: Iterable[Task],
        *,
        active_task_ids: Collection[UUID] = (),
        locked_task_ids: Collection[UUID] = (),
        preserved_worktree_ids: Collection[UUID] = (),
    ) -> tuple[RecoveryFinding, ...]:
        """Return deterministic findings for RUNNING and VERIFYING tasks."""
        active = set(active_task_ids)
        locked = set(locked_task_ids)
        worktrees = set(preserved_worktree_ids)
        findings: list[RecoveryFinding] = []
        for task in sorted(tasks, key=lambda item: str(item.id)):
            if task.status is TaskStatus.RUNNING:
                has_heartbeat = task.id in active
                findings.append(
                    RecoveryFinding(
                        task_id=task.id,
                        status=task.status,
                        recommendation=(
                            RecoveryRecommendation.ACTIVE
                            if has_heartbeat
                            else RecoveryRecommendation.HUMAN_REVIEW
                        ),
                        reason=(
                            "A live worker heartbeat still claims this task."
                            if has_heartbeat
                            else (
                                "RUNNING task has no live worker heartbeat; replay is not "
                                "automatic."
                            )
                        ),
                        worktree_preserved=task.id in worktrees,
                        active_heartbeat=has_heartbeat,
                        lock_present=task.id in locked,
                    )
                )
            elif task.status is TaskStatus.VERIFYING:
                findings.append(
                    RecoveryFinding(
                        task_id=task.id,
                        status=task.status,
                        recommendation=RecoveryRecommendation.HUMAN_REVIEW,
                        reason=(
                            "Verification state is preserved; a verifier must decide explicitly."
                        ),
                        worktree_preserved=task.id in worktrees,
                        active_heartbeat=task.id in active,
                        lock_present=task.id in locked,
                    )
                )
        return tuple(findings)


__all__ = ["RecoveryFinding", "RecoveryInspector", "RecoveryRecommendation"]
