"""Unit tests for the task domain and lifecycle."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from migrationswarm.core.tasks import (
    Task,
    TaskRetryLimitError,
    TaskStateMachine,
    TaskStatus,
    TaskTransitionError,
    TaskType,
)


def make_task(**overrides: Any) -> Task:
    """Build a valid task with optional test-specific overrides."""
    values: dict[str, Any] = {
        "project_id": uuid4(),
        "task_type": TaskType.REPOSITORY_ANALYSIS,
        "title": "Analyze repository",
        "description": "Inspect the repository structure.",
    }
    values.update(overrides)
    return Task(**values)


def test_task_defaults_and_generated_identifiers() -> None:
    """Tasks receive independent UUIDs and expected lifecycle defaults."""
    first = make_task()
    second = make_task()

    assert isinstance(first.id, UUID)
    assert isinstance(first.project_id, UUID)
    assert first.id != second.id
    assert first.status is TaskStatus.PENDING
    assert first.dependencies == []
    assert first.assigned_agent is None
    assert first.attempt == 0
    assert first.max_attempts == 3


def test_task_timestamps_are_timezone_aware_utc() -> None:
    """Generated timestamps are aware and normalized to UTC."""
    task = make_task()

    assert task.created_at.tzinfo is UTC
    assert task.updated_at.tzinfo is UTC
    assert task.created_at.utcoffset() == UTC.utcoffset(task.created_at)
    assert task.updated_at.utcoffset() == UTC.utcoffset(task.updated_at)


def test_naive_timestamp_is_rejected() -> None:
    """Naive timestamps cannot enter the task domain."""
    with pytest.raises(ValueError, match="timezone-aware"):
        make_task(created_at=datetime(2026, 1, 1))


def test_dependency_lists_are_independent() -> None:
    """Each task gets its own dependency list."""
    first = make_task()
    second = make_task()

    first.dependencies.append(uuid4())

    assert first.dependencies
    assert second.dependencies == []


def test_valid_transition_sequence_updates_status_and_timestamp() -> None:
    """A task can follow the normal execution and verification path."""
    task = make_task()
    original_updated_at = task.updated_at

    for status in (
        TaskStatus.READY,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.COMPLETED,
    ):
        TaskStateMachine.transition(task, status)

    assert task.status is TaskStatus.COMPLETED
    assert task.updated_at >= original_updated_at


@pytest.mark.parametrize(
    ("current", "target"),
    [(TaskStatus.RUNNING, TaskStatus.FAILED), (TaskStatus.VERIFYING, TaskStatus.FAILED)],
)
def test_failure_transitions_are_valid(current: TaskStatus, target: TaskStatus) -> None:
    """Execution and verification failures enter the failed state."""
    task = make_task(status=current)

    TaskStateMachine.transition(task, target)

    assert task.status is TaskStatus.FAILED


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.PENDING, TaskStatus.RUNNING),
        (TaskStatus.READY, TaskStatus.PENDING),
        (TaskStatus.RUNNING, TaskStatus.COMPLETED),
        (TaskStatus.VERIFYING, TaskStatus.RUNNING),
        (TaskStatus.FAILED, TaskStatus.COMPLETED),
        (TaskStatus.HUMAN_REVIEW, TaskStatus.RUNNING),
    ],
)
def test_invalid_transition_raises_domain_error(
    current: TaskStatus, target: TaskStatus
) -> None:
    """Transitions outside the explicit table are rejected clearly."""
    task = make_task(status=current)

    with pytest.raises(TaskTransitionError, match="Invalid task transition"):
        TaskStateMachine.transition(task, target)

    assert task.status is current


@pytest.mark.parametrize("target", list(TaskStatus))
def test_completed_is_terminal(target: TaskStatus) -> None:
    """A completed task cannot transition to any state, including itself."""
    task = make_task(status=TaskStatus.COMPLETED)

    with pytest.raises(TaskTransitionError, match="completed"):
        TaskStateMachine.transition(task, target)

    assert task.status is TaskStatus.COMPLETED


def test_failed_to_ready_increments_attempt() -> None:
    """A retry moves a failed task to ready and increments its attempt."""
    task = make_task(status=TaskStatus.FAILED)

    TaskStateMachine.transition(task, TaskStatus.READY)

    assert task.status is TaskStatus.READY
    assert task.attempt == 1


def test_retry_is_rejected_when_max_attempts_is_reached() -> None:
    """An exhausted task cannot be retried and points to human review."""
    task = make_task(status=TaskStatus.FAILED, attempt=3, max_attempts=3)
    original_updated_at = task.updated_at

    with pytest.raises(TaskRetryLimitError, match="HUMAN_REVIEW"):
        TaskStateMachine.transition(task, TaskStatus.READY)

    assert task.status is TaskStatus.FAILED
    assert task.attempt == 3
    assert task.updated_at == original_updated_at


def test_human_review_can_recover_task_to_ready() -> None:
    """Human review can return a failed task to ready without a retry count bump."""
    task = make_task(status=TaskStatus.FAILED, attempt=3, max_attempts=3)

    TaskStateMachine.transition(task, TaskStatus.HUMAN_REVIEW)
    TaskStateMachine.transition(task, TaskStatus.READY)

    assert task.status is TaskStatus.READY
    assert task.attempt == 3
