"""Enums shared by model providers and callers."""

from enum import StrEnum


class ModelCapability(StrEnum):
    """Kinds of model work available to future agents."""

    REASONING = "reasoning"
    CODING = "coding"
    ARCHITECTURE = "architecture"
    CLASSIFICATION = "classification"
    SUMMARIZATION = "summarization"
    VERIFICATION = "verification"


class ModelErrorCategory(StrEnum):
    """Safe categories for provider and routing failures."""

    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


class ModelRole(StrEnum):
    """Roles used in provider-neutral chat messages."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
