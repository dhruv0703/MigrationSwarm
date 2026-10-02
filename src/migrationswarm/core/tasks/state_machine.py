"""Deterministic task lifecycle transitions."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import ClassVar

from migrationswarm.core.tasks.enums import TaskStatus
from migrationswarm.core.tasks.exceptions import TaskRetryLimitError, TaskTransitionError
from migrationswarm.core.tasks.models import Task


class TaskStateMachine:
    """Validate and apply the task lifecycle transition table."""

    _ALLOWED_TRANSITIONS: ClassVar[Mapping[TaskStatus, frozenset[TaskStatus]]] = {
        TaskStatus.PENDING: frozenset({TaskStatus.READY}),
        TaskStatus.READY: frozenset({TaskStatus.RUNNING}),
        TaskStatus.RUNNING: frozenset({TaskStatus.VERIFYING, TaskStatus.FAILED}),
        TaskStatus.VERIFYING: frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED}),
        TaskStatus.FAILED: frozenset({TaskStatus.READY, TaskStatus.HUMAN_REVIEW}),
        TaskStatus.HUMAN_REVIEW: frozenset({TaskStatus.READY}),
        TaskStatus.COMPLETED: frozenset(),
    }

    @classmethod
    def transition(cls, task: Task, target: TaskStatus) -> Task:
        """Apply a valid transition and return the updated task."""
        current = task.status
        allowed_targets = cls._ALLOWED_TRANSITIONS[current]

        if target not in allowed_targets:
            raise TaskTransitionError(
                f"Invalid task transition: {current.value} -> {target.value}"
            )

        if current is TaskStatus.FAILED and target is TaskStatus.READY:
            if task.attempt >= task.max_attempts:
                raise TaskRetryLimitError(
                    f"Task has exhausted max_attempts={task.max_attempts}; "
                    "transition FAILED -> HUMAN_REVIEW instead"
                )
            task.attempt += 1

        task.status = target
        task.updated_at = datetime.now(UTC)
        return task
