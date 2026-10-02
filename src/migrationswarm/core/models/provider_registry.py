"""In-memory provider registry."""

from dataclasses import dataclass

from migrationswarm.core.models.exceptions import DuplicateProviderError, UnknownProviderError
from migrationswarm.core.models.provider import ModelProvider


@dataclass(frozen=True)
class ProviderStatus:
    """Safe process-local provider configuration and health state."""

    configured: bool
    available: bool
    healthy: bool | None

    @property
    def eligible(self) -> bool:
        """Return whether routing may select this provider."""
        return self.configured and self.available


class ProviderRegistry:
    """Register and retrieve providers by stable provider name."""

    def __init__(self, providers: list[ModelProvider] | None = None) -> None:
        self._providers: dict[str, ModelProvider] = {}
        self._invalid: set[str] = set()
        self._healthy: set[str] = set()
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: ModelProvider) -> None:
        """Register a provider, rejecting duplicate names."""
        if provider.name in self._providers:
            raise DuplicateProviderError(f"Provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def get(self, name: str) -> ModelProvider:
        """Return a provider by name."""
        try:
            return self._providers[name]
        except KeyError as error:
            raise UnknownProviderError(f"Unknown provider: {name}") from error

    def list_providers(self) -> tuple[ModelProvider, ...]:
        """Return providers in deterministic name order."""
        return tuple(self._providers[name] for name in sorted(self._providers))

    def status(self, name: str) -> ProviderStatus:
        """Return safe configuration and current-process health information."""
        provider = self.get(name)
        configured = provider.credentials_available
        invalid = name in self._invalid
        return ProviderStatus(
            configured=configured,
            available=configured and not invalid,
            healthy=True if name in self._healthy else (False if invalid else None),
        )

    def eligible(self, name: str) -> bool:
        """Return whether a provider can currently be selected."""
        return self.status(name).eligible

    def mark_healthy(self, name: str) -> None:
        """Record a successful explicit provider/model check."""
        self.get(name)
        self._invalid.discard(name)
        self._healthy.add(name)

    def mark_invalid(self, name: str) -> None:
        """Exclude a provider after authentication failure for this registry run."""
        self.get(name)
        self._invalid.add(name)
        self._healthy.discard(name)
