"""Provider-neutral model contracts, registries, and routing."""

from migrationswarm.core.models.enums import ModelCapability, ModelErrorCategory, ModelRole
from migrationswarm.core.models.exceptions import (
    DuplicateModelError,
    DuplicateProviderError,
    ModelAuthenticationError,
    ModelInvalidRequestError,
    ModelInvalidResponseError,
    ModelProviderError,
    ModelRateLimitError,
    ModelRoutingError,
    ModelTimeoutError,
    ModelTransientError,
    ModelUnavailableError,
    NoModelAvailableError,
    UnknownModelError,
    UnknownProviderError,
)
from migrationswarm.core.models.models import (
    ModelDefinition,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)
from migrationswarm.core.models.provider import ModelProvider
from migrationswarm.core.models.provider_registry import ProviderRegistry, ProviderStatus
from migrationswarm.core.models.registry import ModelRegistry
from migrationswarm.core.models.router import ModelRouter

__all__ = [
    "DuplicateModelError",
    "DuplicateProviderError",
    "ModelAuthenticationError",
    "ModelCapability",
    "ModelDefinition",
    "ModelErrorCategory",
    "ModelMessage",
    "ModelProvider",
    "ModelProviderError",
    "ModelInvalidRequestError",
    "ModelInvalidResponseError",
    "ModelRateLimitError",
    "ModelRegistry",
    "ModelRequest",
    "ModelResponse",
    "ModelRole",
    "ModelRouter",
    "ModelRoutingError",
    "ModelTimeoutError",
    "ModelTransientError",
    "ModelUnavailableError",
    "NoModelAvailableError",
    "ProviderRegistry",
    "ProviderStatus",
    "UnknownModelError",
    "UnknownProviderError",
]
