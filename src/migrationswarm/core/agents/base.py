"""Abstract execution contract for future agents."""

from abc import ABC, abstractmethod
from collections.abc import Collection
from typing import ClassVar

from migrationswarm.core.agents.capabilities import AgentCapability
from migrationswarm.core.agents.models import AgentContext, AgentResult
from migrationswarm.core.tasks.models import Task


class BaseAgent(ABC):
    """Synchronous, provider-independent contract for a task worker."""

    name: ClassVar[str]
    capabilities: ClassVar[Collection[AgentCapability]]

    @abstractmethod
    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        """Execute a task and return a structured result."""

    def supports(self, capability: AgentCapability) -> bool:
        """Return whether this agent advertises the requested capability."""
        return capability in self.capabilities
