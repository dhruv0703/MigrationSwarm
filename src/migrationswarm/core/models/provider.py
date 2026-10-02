"""Abstract provider contract."""

from abc import ABC, abstractmethod
from typing import ClassVar

from migrationswarm.core.models.models import ModelRequest, ModelResponse


class ModelProvider(ABC):
    """Synchronous provider interface used by the model router."""

    name: ClassVar[str]

    @property
    def credentials_available(self) -> bool:
        """Return whether this provider has the credentials it needs."""
        return True

    @abstractmethod
    def generate(self, request: ModelRequest, model: str) -> ModelResponse:
        """Generate one normalized response using a provider model ID."""

    def check(self, model: str) -> ModelResponse:
        """Perform a minimal connectivity check through the normal contract."""
        from migrationswarm.core.models.enums import ModelCapability, ModelRole
        from migrationswarm.core.models.models import ModelMessage

        request = ModelRequest(
            messages=[ModelMessage(role=ModelRole.USER, content="ping")],
            capability=ModelCapability.VERIFICATION,
            temperature=0.0,
            max_tokens=1,
        )
        return self.generate(request, model)
