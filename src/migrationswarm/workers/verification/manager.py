"""Bounded local verification worker management."""

from collections.abc import Callable
from threading import Event, Thread

from migrationswarm.workers.exceptions import WorkerConfigurationError
from migrationswarm.workers.models import WorkerConfig, WorkerSnapshot, WorkerStatus
from migrationswarm.workers.verification.worker import VerificationWorker

VerificationWorkerFactory = Callable[[WorkerConfig], VerificationWorker]


class VerificationWorkerManager:
    """Run one or two verification workers without an external worker framework."""

    def __init__(
        self,
        worker_factory: VerificationWorkerFactory,
        *,
        max_workers: int = 1,
    ) -> None:
        if not 1 <= max_workers <= 2:
            raise WorkerConfigurationError("verification_workers must be between 1 and 2")
        self.worker_factory = worker_factory
        self.max_workers = max_workers
        self.workers: list[VerificationWorker] = []
        self._threads: list[Thread] = []
        self._stop_event = Event()

    def start(self) -> None:
        if self._threads:
            return
        self.workers = [self.worker_factory(WorkerConfig()) for _ in range(self.max_workers)]
        self._threads = [
            Thread(
                target=worker.run,
                args=(self._stop_event,),
                name=f"migrationswarm-verification-{index}",
            )
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
        return sum(
            worker.status in {WorkerStatus.CLAIMING, WorkerStatus.RUNNING}
            for worker in self.workers
        )

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(
            worker.error_type for worker in self.workers if worker.error_type is not None
        )

    def snapshots(self) -> tuple[WorkerSnapshot, ...]:
        return tuple(worker.snapshot() for worker in self.workers)


__all__ = ["VerificationWorkerManager"]
