"""Bounded, capability-aware model routing with explicit fallback policy."""

from time import monotonic, sleep
from typing import Any

import structlog

from migrationswarm.config import get_settings
from migrationswarm.core.models.exceptions import (
    ModelAuthenticationError,
    ModelInvalidRequestError,
    ModelProviderError,
    ModelRoutingError,
    NoModelAvailableError,
)
from migrationswarm.core.models.models import ModelDefinition, ModelRequest, ModelResponse
from migrationswarm.core.models.provider_registry import ProviderRegistry
from migrationswarm.core.models.registry import ModelRegistry

logger = structlog.get_logger(__name__)


class ModelRouter:
    """Select eligible models by capability and an explicit fallback policy."""

    def __init__(
        self,
        model_registry: ModelRegistry,
        provider_registry: ProviderRegistry,
        *,
        max_attempts_per_model: int = 2,
        backoff_seconds: float = 0.0,
        allow_cross_provider_fallback: bool | None = None,
    ) -> None:
        if max_attempts_per_model < 1:
            raise ValueError("max_attempts_per_model must be at least 1")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")
        self.model_registry = model_registry
        self.provider_registry = provider_registry
        self.max_attempts_per_model = max_attempts_per_model
        self.backoff_seconds = backoff_seconds
        self.allow_cross_provider_fallback = (
            get_settings().allow_cross_provider_fallback
            if allow_cross_provider_fallback is None
            else allow_cross_provider_fallback
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        """Route one request with bounded retries and safe routing metadata."""
        candidates = self.model_registry.for_capability(request.capability)
        if not candidates:
            raise self._routing_failure(request, [], "No enabled model supports capability")

        ordered = self._ordered_candidates(candidates)
        primary_provider = ordered[0].provider
        trace: list[dict[str, Any]] = []
        last_error: ModelProviderError | None = None

        for definition in ordered:
            try:
                provider = self.provider_registry.get(definition.provider)
            except ValueError as error:
                last_error = NoModelAvailableError(str(error))
                trace.append(
                    self._trace_entry(
                        definition,
                        primary_provider=primary_provider,
                        status="skipped",
                        reason="provider_not_registered",
                        retry_count=0,
                    )
                )
                continue

            status = self.provider_registry.status(provider.name)
            if not status.eligible:
                last_error = ModelAuthenticationError(
                    f"Provider is not eligible: {provider.name}"
                )
                trace.append(
                    self._trace_entry(
                        definition,
                        primary_provider=primary_provider,
                        status="skipped",
                        reason=(
                            "credentials_unavailable"
                            if not status.configured
                            else "provider_ineligible"
                        ),
                        retry_count=0,
                    )
                )
                continue

            for attempt in range(1, self.max_attempts_per_model + 1):
                started = monotonic()
                try:
                    response = provider.generate(request, definition.provider_model_id)
                except ModelAuthenticationError as error:
                    last_error = error
                    self.provider_registry.mark_invalid(provider.name)
                    trace.append(
                        self._trace_entry(
                            definition,
                            primary_provider=primary_provider,
                            status="failed",
                            reason="authentication",
                            retry_count=attempt - 1,
                        )
                    )
                    self._log_failure(provider.name, definition, request, error, started)
                    break
                except ModelInvalidRequestError as error:
                    trace.append(
                        self._trace_entry(
                            definition,
                            primary_provider=primary_provider,
                            status="failed",
                            reason="invalid_request",
                            retry_count=attempt - 1,
                        )
                    )
                    self._log_failure(provider.name, definition, request, error, started)
                    raise
                except ModelProviderError as error:
                    last_error = error
                    trace.append(
                        self._trace_entry(
                            definition,
                            primary_provider=primary_provider,
                            status="failed",
                            reason=error.category.value,
                            retry_count=attempt - 1,
                        )
                    )
                    self._log_failure(provider.name, definition, request, error, started)
                    if not error.retryable or attempt == self.max_attempts_per_model:
                        break
                    if self.backoff_seconds:
                        sleep(self.backoff_seconds * attempt)
                    continue

                latency_ms = response.latency_ms or (monotonic() - started) * 1000
                trace.append(
                    self._trace_entry(
                        definition,
                        primary_provider=primary_provider,
                        status="success",
                        reason="completed",
                        retry_count=attempt - 1,
                    )
                )
                telemetry = {
                    "provider": provider.name,
                    "model": definition.provider_model_id,
                    "capability": request.capability.value,
                    "latency_ms": latency_ms,
                    "success": True,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                }
                routing = self._routing_metadata(
                    request.capability.value,
                    primary_provider,
                    trace,
                    outcome="success",
                    final_provider=provider.name,
                    final_model=definition.provider_model_id,
                )
                logger.info("model_call_completed", **telemetry)
                return response.model_copy(
                    update={
                        "provider": provider.name,
                        "model": definition.provider_model_id,
                        "latency_ms": latency_ms,
                        "metadata": {
                            **response.metadata,
                            "telemetry": telemetry,
                            "routing": routing,
                        },
                    }
                )

        message = (
            f"No eligible model completed capability: {request.capability.value}"
            if last_error is not None
            else f"No available provider can serve capability: {request.capability.value}"
        )
        raise self._routing_failure(request, trace, message)

    def route(self, request: ModelRequest) -> ModelResponse:
        """Route alias for the primary generate method."""
        return self.generate(request)

    def _ordered_candidates(
        self, candidates: tuple[ModelDefinition, ...]
    ) -> tuple[ModelDefinition, ...]:
        """Keep same-provider fallbacks ahead of optional cross-provider candidates."""
        primary_provider = candidates[0].provider
        same_provider = tuple(item for item in candidates if item.provider == primary_provider)
        if not self.allow_cross_provider_fallback:
            return same_provider
        cross_provider = tuple(item for item in candidates if item.provider != primary_provider)
        return same_provider + cross_provider

    def _routing_failure(
        self, request: ModelRequest, trace: list[dict[str, Any]], message: str
    ) -> ModelRoutingError:
        primary_provider = trace[0]["provider"] if trace else None
        return ModelRoutingError(
            message,
            capability=request.capability.value,
            routing_trace=self._routing_metadata(
                request.capability.value,
                primary_provider,
                trace,
                outcome="exhausted",
            )["attempts"],
        )

    def _routing_metadata(
        self,
        capability: str,
        primary_provider: str | None,
        trace: list[dict[str, Any]],
        *,
        outcome: str,
        final_provider: str | None = None,
        final_model: str | None = None,
    ) -> dict[str, Any]:
        return {
            "requested_capability": capability,
            "primary_provider": primary_provider,
            "cross_provider_fallback_enabled": self.allow_cross_provider_fallback,
            "attempts": [dict(item) for item in trace],
            "outcome": outcome,
            "final_provider": final_provider,
            "final_model": final_model,
        }

    @staticmethod
    def _trace_entry(
        definition: ModelDefinition,
        *,
        primary_provider: str,
        status: str,
        reason: str,
        retry_count: int,
    ) -> dict[str, Any]:
        return {
            "logical_name": definition.logical_name,
            "provider": definition.provider,
            "model": definition.provider_model_id,
            "status": status,
            "reason": reason,
            "retry_count": retry_count,
            "provider_changed": definition.provider != primary_provider,
        }

    @staticmethod
    def _log_failure(
        provider: str,
        definition: ModelDefinition,
        request: ModelRequest,
        error: ModelProviderError,
        started: float,
    ) -> None:
        logger.warning(
            "model_call_failed",
            provider=provider,
            model=definition.provider_model_id,
            capability=request.capability.value,
            error_type=type(error).__name__,
            error_category=error.category.value,
            retryable=error.retryable,
            latency_ms=(monotonic() - started) * 1000,
        )
