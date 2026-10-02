"""Enumerations used by the task domain."""

from enum import StrEnum


class TaskStatus(StrEnum):
    """Lifecycle states available to a task."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    HUMAN_REVIEW = "human_review"


class TaskType(StrEnum):
    """Generic task types used by the future orchestrator."""

    REPOSITORY_ANALYSIS = "repository_analysis"
    DEPENDENCY_ANALYSIS = "dependency_analysis"
    ARCHITECTURE_ANALYSIS = "architecture_analysis"
    SERVICE_BOUNDARY_ANALYSIS = "service_boundary_analysis"
    MIGRATION_PLANNING = "migration_planning"
    CODE_REFACTOR = "code_refactor"
    TEST = "test"
    DEBUG = "debug"
    VERIFY = "verify"
    SERVICE_EXTRACTION = "service_extraction"
