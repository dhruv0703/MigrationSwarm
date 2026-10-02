"""Domain exceptions for model registration, providers, and routing."""

from typing import Any, ClassVar

from migrationswarm.core.models.enums import ModelErrorCategory


class ModelProviderError(RuntimeError):
    """Base error returned by a model provider."""

    retryable = False
    category: ClassVar[ModelErrorCategory] = ModelErrorCategory.UNKNOWN


class ModelInvalidRequestError(ModelProviderError):
    """Raised when the provider rejects the request as malformed."""


class ModelAuthenticationError(ModelProviderError):
    """Raised when credentials are missing or rejected."""

    category = ModelErrorCategory.AUTHENTICATION


class ModelRateLimitError(ModelProviderError):
    """Raised when a provider rate limit is reached."""

    retryable = True
    category = ModelErrorCategory.RATE_LIMITED


class ModelUnavailableError(ModelProviderError):
    """Raised for transient provider or network availability failures."""

    retryable = True
    category = ModelErrorCategory.MODEL_UNAVAILABLE


class ModelTimeoutError(ModelUnavailableError):
    """Raised when a provider request exceeds its timeout."""

    category = ModelErrorCategory.TIMEOUT


class ModelInvalidResponseError(ModelProviderError):
    """Raised when a provider returns an unusable response."""

    retryable = True
    category = ModelErrorCategory.INVALID_RESPONSE


class ModelTransientError(ModelProviderError):
    """Raised for a bounded retryable failure without a more precise category."""

    retryable = True
    category = ModelErrorCategory.TRANSIENT


class NoModelAvailableError(ModelProviderError):
    """Raised when no enabled model can satisfy a request."""


class ModelRoutingError(
    NoModelAvailableError, ModelUnavailableError, ModelAuthenticationError
):
    """Raised when every eligible routing candidate is exhausted."""

    category = ModelErrorCategory.UNKNOWN

    def __init__(
        self,
        message: str,
        *,
        capability: str,
        routing_trace: list[dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.capability = capability
        self.routing_trace = routing_trace


class DuplicateProviderError(ValueError):
    """Raised when a provider name is registered twice."""


class UnknownProviderError(ValueError):
    """Raised when a provider name is not registered."""


class DuplicateModelError(ValueError):
    """Raised when a logical model name is registered twice."""


class UnknownModelError(ValueError):
    """Raised when a logical model name is not registered."""
