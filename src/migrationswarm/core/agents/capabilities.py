"""Capabilities that an agent can advertise."""

from enum import StrEnum


class AgentCapability(StrEnum):
    """Supported categories of work for agents."""

    REPOSITORY_ANALYSIS = "repository_analysis"
    DEPENDENCY_ANALYSIS = "dependency_analysis"
    ARCHITECTURE_ANALYSIS = "architecture_analysis"
    SERVICE_BOUNDARY_ANALYSIS = "service_boundary_analysis"
    MIGRATION_PLANNING = "migration_planning"
    CODE_REFACTOR = "code_refactor"
    TESTING = "testing"
    DEBUGGING = "debugging"
    VERIFICATION = "verification"
    SERVICE_EXTRACTION = "service_extraction"
