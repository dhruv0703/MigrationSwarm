"""Pydantic contracts exchanged with model providers."""

from typing import Any

from pydantic import BaseModel, Field

from migrationswarm.core.models.enums import ModelCapability, ModelRole


class ModelMessage(BaseModel):
    """One provider-neutral chat message."""

    role: ModelRole
    content: str = Field(min_length=1)


class ModelRequest(BaseModel):
    """A validated request independent of any provider SDK."""

    messages: list[ModelMessage] = Field(min_length=1)
    capability: ModelCapability
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, gt=0)
    response_format: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    """Normalized response exposed to the rest of MigrationSwarm."""

    provider: str
    model: str
    content: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float = Field(default=0.0, ge=0.0)
    finish_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelDefinition(BaseModel):
    """Logical model catalog entry, separate from provider code."""

    logical_name: str
    provider: str
    provider_model_id: str = Field(min_length=1)
    capabilities: frozenset[ModelCapability] = Field(min_length=1)
    enabled: bool = True
    priority: int = Field(default=100, ge=0)
    context_window: int | None = Field(default=None, gt=0)
    notes: str | None = None
