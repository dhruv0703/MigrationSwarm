"""Durable scheduler-to-Redis ready-task dispatch."""

from uuid import UUID

from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.tasks import TaskStatus, TaskType
from migrationswarm.persistence.db.repositories import TaskRepository
from migrationswarm.persistence.redis.queue import ReadyTaskQueue
from migrationswarm.workers.models import DispatchResult


class TaskDispatcher:
    """Promote dependency-ready tasks and publish only their IDs to Redis."""

    def __init__(self, task_repository: TaskRepository, queue: ReadyTaskQueue) -> None:
        self.task_repository = task_repository
        self.queue = queue

    def inspect(self, project_id: UUID) -> DispatchResult:
        """Inspect durable tasks without changing state or Redis."""
        tasks = self.task_repository.list(project_id=project_id)
        graph = TaskGraph(tasks)
        ready = tuple(
            task
            for task in graph.topological_order()
            if task.status is TaskStatus.READY and task.task_type is not TaskType.DEBUG
        )
        pending = tuple(
            task
            for task in tasks
            if task.status is TaskStatus.PENDING and task.task_type is not TaskType.DEBUG
        )
        blocked = tuple(task for task in pending if task not in graph.eligible_tasks())
        return DispatchResult(
            project_id=project_id,
            queue_name="execution",
            ready_task_ids=[task.id for task in ready],
            pending_count=len(pending),
            ready_count=len(ready),
            blocked_pending_count=len(blocked),
        )

    def dispatch(self, project_id: UUID, *, include_existing_ready: bool = True) -> DispatchResult:
        """Promote eligible pending tasks, persist them, and enqueue their IDs."""
        tasks = self.task_repository.list(project_id=project_id)
        original_status = {task.id: task.status for task in tasks}
        graph = TaskGraph(tasks)
        scheduler = TaskScheduler(graph)
        ready_tasks = scheduler.schedule()

        promoted = [
            task
            for task in tasks
            if original_status[task.id] is TaskStatus.PENDING
            and task.status is TaskStatus.READY
            and task.task_type is not TaskType.DEBUG
        ]
        for task in promoted:
            self.task_repository.update(task)

        enqueue_candidates = promoted
        if include_existing_ready:
            enqueue_candidates = [
                task for task in ready_tasks
                if task not in promoted and task.task_type is not TaskType.DEBUG
            ] + promoted
        enqueued: list[UUID] = []
        for task in sorted(enqueue_candidates, key=lambda item: str(item.id)):
            if self.queue.enqueue(task.id):
                enqueued.append(task.id)

        pending_count = sum(
            task.status is TaskStatus.PENDING and task.task_type is not TaskType.DEBUG
            for task in tasks
        )
        ready_count = sum(
            task.status is TaskStatus.READY and task.task_type is not TaskType.DEBUG
            for task in tasks
        )
        blocked_count = sum(
            task.status is TaskStatus.PENDING
            and task.task_type is not TaskType.DEBUG
            and task not in scheduler.graph.eligible_tasks()
            for task in tasks
        )
        return DispatchResult(
            project_id=project_id,
            queue_name="execution",
            promoted_task_ids=[task.id for task in promoted],
            ready_task_ids=[task.id for task in ready_tasks],
            enqueued_task_ids=enqueued,
            pending_count=pending_count,
            ready_count=ready_count,
            blocked_pending_count=blocked_count,
        )


__all__ = ["TaskDispatcher"]
