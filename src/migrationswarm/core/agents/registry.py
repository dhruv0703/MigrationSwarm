"""In-memory registry of available agents."""

from migrationswarm.core.agents.base import BaseAgent
from migrationswarm.core.agents.capabilities import AgentCapability
from migrationswarm.core.agents.exceptions import DuplicateAgentError, UnknownAgentError


class AgentRegistry:
    """Register and query agents deterministically by name and capability."""

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        """Register an agent, rejecting duplicate names."""
        if agent.name in self._agents:
            raise DuplicateAgentError(f"Agent name already registered: {agent.name}")
        self._agents[agent.name] = agent

    def get(self, name: str) -> BaseAgent:
        """Return a registered agent by name."""
        try:
            return self._agents[name]
        except KeyError as error:
            raise UnknownAgentError(f"Unknown agent: {name}") from error

    def list_agents(self) -> tuple[BaseAgent, ...]:
        """Return all registered agents in deterministic name order."""
        return tuple(self._agents[name] for name in sorted(self._agents))

    def find_by_capability(self, capability: AgentCapability) -> tuple[BaseAgent, ...]:
        """Return registered agents supporting a capability."""
        return tuple(
            agent for agent in self.list_agents() if agent.supports(capability)
        )
