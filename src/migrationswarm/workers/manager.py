"""Bounded local thread management for workers."""

from collections.abc import Callable, Sequence
from threading import Event, Thread

from migrationswarm.workers.exceptions import WorkerConfigurationError
from migrationswarm.workers.models import WorkerConfig, WorkerSnapshot
from migrationswarm.workers.worker import Worker

WorkerFactory = Callable[[WorkerConfig], Worker]


class WorkerManager:
    """Start and stop a small bounded set of local worker threads."""

    def __init__(
        self,
        worker_factory: WorkerFactory | None = None,
        *,
        workers: Sequence[Worker] | None = None,
        max_workers: int = 2,
    ) -> None:
        if not 1 <= max_workers <= 4:
            raise WorkerConfigurationError("max_workers must be between 1 and 4")
        if worker_factory is None and workers is None:
            raise WorkerConfigurationError("worker_factory or workers is required")
        if workers is not None and len(workers) > max_workers:
            raise WorkerConfigurationError("workers cannot exceed max_workers")
        self.max_workers = max_workers
        self.worker_factory = worker_factory
        self.workers = list(workers or [])
        self._threads: list[Thread] = []
        self._stop_event = Event()

    def start(self) -> None:
        """Start the configured workers once."""
        if self._threads:
            return
        if not self.workers:
            assert self.worker_factory is not None
            self.workers = [
                self.worker_factory(WorkerConfig()) for _ in range(self.max_workers)
            ]
        self._threads = [
            Thread(target=worker.run, args=(self._stop_event,), name=f"migrationswarm-{index}")
            for index, worker in enumerate(self.workers)
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        for worker in self.workers:
            worker.stop()

    def join(self, timeout: float | None = None) -> None:
        for thread in self._threads:
            thread.join(timeout)

    @property
    def active_count(self) -> int:
        return sum(worker.status.value in {"claiming", "running"} for worker in self.workers)

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(
            worker.error_type for worker in self.workers if worker.error_type is not None
        )

    def snapshots(self) -> tuple[WorkerSnapshot, ...]:
        return tuple(worker.snapshot() for worker in self.workers)


__all__ = ["WorkerManager"]
