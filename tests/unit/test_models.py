"""Unit tests for provider-neutral model routing and integrations."""

import json
from collections.abc import Callable
from typing import ClassVar

import httpx
import pytest
from typer.testing import CliRunner

from migrationswarm.cli.main import app
from migrationswarm.core.models import (
    DuplicateProviderError,
    ModelAuthenticationError,
    ModelCapability,
    ModelDefinition,
    ModelErrorCategory,
    ModelInvalidRequestError,
    ModelInvalidResponseError,
    ModelMessage,
    ModelProvider,
    ModelProviderError,
    ModelRateLimitError,
    ModelRegistry,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelRouter,
    ModelRoutingError,
    ModelTimeoutError,
    ModelUnavailableError,
    NoModelAvailableError,
    ProviderRegistry,
)
from migrationswarm.providers.groq import GroqProvider
from migrationswarm.providers.siliconflow import SiliconFlowProvider

runner = CliRunner()


def request(capability: ModelCapability = ModelCapability.REASONING) -> ModelRequest:
    """Create a small valid model request."""
    return ModelRequest(
        messages=[ModelMessage(role=ModelRole.USER, content="hello")],
        capability=capability,
        max_tokens=4,
    )


def response(provider: str, model: str = "model") -> ModelResponse:
    """Create a normalized fake response."""
    return ModelResponse(
        provider=provider,
        model=model,
        content="ok",
        input_tokens=2,
        output_tokens=1,
        latency_ms=1.5,
        finish_reason="stop",
    )


class FakeProvider(ModelProvider):
    """Test-only provider with scripted responses or errors."""

    name: ClassVar[str] = "fake"

    def __init__(
        self,
        outcomes: list[ModelResponse | Exception] | None = None,
        *,
        credentials: bool = True,
    ) -> None:
        self.outcomes = list(outcomes or [response(self.name)])
        self.credentials = credentials
        self.calls = 0

    @property
    def credentials_available(self) -> bool:
        return self.credentials

    def generate(self, request: ModelRequest, model: str) -> ModelResponse:
        del request, model
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class DuplicateFakeProvider(FakeProvider):
    """Test provider used to verify duplicate registration."""

    name = "fake"


class FirstFakeProvider(FakeProvider):
    """Named fake provider for fallback tests."""

    name = "first"


class SecondFakeProvider(FakeProvider):
    """Named fake provider for fallback tests."""

    name = "second"


class RetryFakeProvider(FakeProvider):
    """Named fake provider for retry tests."""

    name = "retry"


class AuthFakeProvider(FakeProvider):
    """Named fake provider for authentication tests."""

    name = "auth"


class GroqFakeProvider(FakeProvider):
    """Named fake provider for CLI connectivity tests."""

    name = "groq"


def definition(
    logical_name: str,
    provider: str = "fake",
    capability: ModelCapability = ModelCapability.REASONING,
    *,
    priority: int = 10,
    enabled: bool = True,
) -> ModelDefinition:
    """Create a compact logical model definition."""
    return ModelDefinition(
        logical_name=logical_name,
        provider=provider,
        provider_model_id=f"provider-{logical_name}",
        capabilities=frozenset({capability}),
        priority=priority,
        enabled=enabled,
    )


def test_model_request_and_response_validation() -> None:
    """Provider-neutral request/response models enforce useful bounds."""
    valid = request()
    assert valid.temperature == 0.2
    assert valid.metadata == {}
    assert response("fake").metadata == {}

    with pytest.raises(ValueError):
        ModelRequest(messages=[], capability=ModelCapability.REASONING)
    with pytest.raises(ValueError):
        ModelRequest(
            messages=[ModelMessage(role=ModelRole.USER, content="hello")],
            capability=ModelCapability.REASONING,
            temperature=3.0,
        )
    with pytest.raises(ValueError):
        ModelResponse(provider="fake", model="m", content="x", input_tokens=-1)


def test_provider_registry_registration_and_duplicates() -> None:
    """Providers are uniquely registered and deterministically listed."""
    provider = FirstFakeProvider()
    registry = ProviderRegistry([provider])
    assert registry.get("first") is provider
    assert registry.list_providers() == (provider,)

    with pytest.raises(DuplicateProviderError, match="already registered"):
        registry.register(FirstFakeProvider())


def test_model_registry_lookup_capability_priority_and_disabled_models() -> None:
    """Logical models are filtered by capability and priority."""
    registry = ModelRegistry(
        [
            definition("slow", priority=30),
            definition("fast", priority=10),
            definition("disabled", priority=0, enabled=False),
            definition("coding", capability=ModelCapability.CODING, priority=1),
        ]
    )

    assert registry.get("fast").provider_model_id == "provider-fast"
    assert [model.logical_name for model in registry.for_capability(ModelCapability.REASONING)] == [
        "fast",
        "slow",
    ]
    assert [model.logical_name for model in registry.for_capability(ModelCapability.CODING)] == [
        "coding"
    ]


def test_default_groq_model_definitions_and_capabilities() -> None:
    """The configured Groq catalog uses currently available logical models."""
    from migrationswarm.config import Settings
    from migrationswarm.core.models.registry import default_model_registry

    registry = default_model_registry(Settings())
    groq_models = {
        model.logical_name: model
        for model in registry.list_models()
        if model.provider == "groq"
    }

    assert set(groq_models) == {"groq-reasoning", "groq-fast", "groq-qwen"}
    assert groq_models["groq-reasoning"].provider_model_id == "openai/gpt-oss-120b"
    assert groq_models["groq-reasoning"].capabilities == frozenset(
        {
            ModelCapability.REASONING,
            ModelCapability.ARCHITECTURE,
            ModelCapability.VERIFICATION,
        }
    )
    assert groq_models["groq-fast"].provider_model_id == "openai/gpt-oss-20b"
    assert groq_models["groq-fast"].capabilities == frozenset(
        {ModelCapability.CLASSIFICATION, ModelCapability.SUMMARIZATION}
    )
    assert groq_models["groq-qwen"].provider_model_id == "qwen/qwen3.8-27b"
    assert groq_models["groq-qwen"].capabilities == frozenset(
        {ModelCapability.CODING, ModelCapability.REASONING}
    )
    assert registry.get("groq-reasoning") is groq_models["groq-reasoning"]
    assert registry.for_capability(ModelCapability.VERIFICATION)[0].logical_name == (
        "groq-reasoning"
    )


def _success_transport(provider_model: str) -> Callable[[httpx.Request], httpx.Response]:
    """Return a transport handler that validates an OpenAI-compatible request."""
    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        assert http_request.url.path == "/v1/chat/completions"
        assert http_request.headers["authorization"] == "Bearer secret"
        assert payload["model"] == provider_model
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        assert payload["max_tokens"] == 4
        return httpx.Response(
            200,
            json={
                "model": provider_model,
                "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    return handler


@pytest.mark.parametrize(
    ("provider_type", "base_url"),
    [
        (GroqProvider, "https://groq.test/v1"),
        (SiliconFlowProvider, "https://siliconflow.test/v1"),
    ],
)
def test_provider_request_construction_and_response_normalization(
    provider_type: type[GroqProvider] | type[SiliconFlowProvider], base_url: str
) -> None:
    """Both HTTP integrations normalize the same compatible response shape."""
    provider = provider_type(
        api_key="secret",
        base_url=base_url,
        transport=httpx.MockTransport(_success_transport("test-model")),
    )

    result = provider.generate(request(), "test-model")

    assert result.provider == provider.name
    assert result.model == "test-model"
    assert result.content == "answer"
    assert result.input_tokens == 5
    assert result.output_tokens == 2
    assert result.finish_reason == "stop"
    assert result.latency_ms >= 0


def test_groq_json_requests_hide_reasoning_tokens() -> None:
    """Groq JSON mode must use a compatible reasoning format."""
    captured: dict[str, object] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(http_request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}]},
        )

    GroqProvider(
        api_key="secret",
        transport=httpx.MockTransport(handler),
    ).generate(
        ModelRequest(
            messages=[ModelMessage(role=ModelRole.USER, content="Return JSON")],
            capability=ModelCapability.REASONING,
            response_format={"type": "json_object"},
        ),
        "openai/gpt-oss-120b",
    )

    assert captured["reasoning_format"] == "hidden"


def test_missing_api_key_and_authentication_failures_are_safe() -> None:
    """Missing or rejected credentials never expose the key or make a request."""
    provider = GroqProvider(api_key="")
    with pytest.raises(ModelAuthenticationError, match="Missing API key") as missing:
        provider.generate(request(), "model")
    assert "secret" not in str(missing.value)

    auth_provider = GroqProvider(
        api_key="secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"error": "no"})),
    )
    with pytest.raises(ModelAuthenticationError) as rejected:
        auth_provider.generate(request(), "model")
    assert "secret" not in str(rejected.value)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, None, ModelRateLimitError),
        (503, None, ModelProviderError),
        (413, {"error": {"code": "rate_limit_exceeded"}}, ModelRateLimitError),
    ],
)
def test_rate_limit_and_transient_failures_are_classified(
    status: int, body: dict[str, object] | None, expected: type[ModelProviderError]
) -> None:
    """Provider statuses map to bounded-retry error types."""
    provider = GroqProvider(
        api_key="secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
    )

    with pytest.raises(expected) as failure:
        provider.generate(request(), "model")
    assert failure.value.retryable


def test_json_validation_failure_is_classified_as_bounded_invalid_response() -> None:
    """Provider-side JSON generation failures are retryable but bounded."""
    provider = GroqProvider(
        api_key="secret",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400,
                json={"error": {"code": "json_validate_failed"}},
            )
        ),
    )

    with pytest.raises(ModelInvalidResponseError) as failure:
        provider.generate(request(), "openai/gpt-oss-120b")

    assert failure.value.category is ModelErrorCategory.INVALID_RESPONSE
    assert failure.value.retryable is True


def test_timeout_is_classified_as_retryable() -> None:
    """HTTP timeouts become safe retryable provider errors."""
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network timeout")

    provider = GroqProvider(api_key="secret", transport=httpx.MockTransport(timeout))
    with pytest.raises(ModelTimeoutError) as failure:
        provider.generate(request(), "model")
    assert failure.value.retryable


def test_router_fallback_and_telemetry() -> None:
    """A transient primary failure falls back to the next prioritized model."""
    first = FirstFakeProvider([ModelUnavailableError("temporary")])
    second = SecondFakeProvider([response("second")])
    models = ModelRegistry(
        [definition("primary", "first", priority=1), definition("fallback", "second", priority=2)]
    )
    providers = ProviderRegistry([first, second])
    result = ModelRouter(
        models,
        providers,
        max_attempts_per_model=1,
        allow_cross_provider_fallback=True,
    ).generate(request())

    assert result.provider == "second"
    assert result.metadata["telemetry"]["success"] is True
    assert result.metadata["telemetry"]["capability"] == "reasoning"
    assert first.calls == 1
    assert second.calls == 1
    assert result.metadata["routing"]["cross_provider_fallback_enabled"] is True
    assert result.metadata["routing"]["attempts"][0]["reason"] == "model_unavailable"


def test_router_retries_transient_failures_only_up_to_limit() -> None:
    """Transient failures are retried a finite number of times."""
    provider = RetryFakeProvider(
        [ModelUnavailableError("one"), ModelUnavailableError("two")]
    )
    router = ModelRouter(
        ModelRegistry([definition("retry-model", "retry")]),
        ProviderRegistry([provider]),
        max_attempts_per_model=2,
    )

    with pytest.raises(ModelUnavailableError):
        router.generate(request())
    assert provider.calls == 2


def test_router_does_not_retry_authentication_failures() -> None:
    """Authentication failures do not consume retry attempts."""
    provider = AuthFakeProvider([ModelAuthenticationError("bad credentials")])
    router = ModelRouter(
        ModelRegistry([definition("auth-model", "auth")]), ProviderRegistry([provider])
    )

    with pytest.raises(ModelAuthenticationError):
        router.generate(request())
    assert provider.calls == 1


def test_provider_status_distinguishes_configuration_availability_and_health() -> None:
    """Provider state is safe, local, and does not require a network probe."""
    configured = FirstFakeProvider()
    missing = SecondFakeProvider(credentials=False)
    registry = ProviderRegistry([configured, missing])

    assert registry.status("first").configured is True
    assert registry.status("first").available is True
    assert registry.status("first").healthy is None
    assert registry.status("first").eligible is True
    assert registry.status("second").configured is False
    assert registry.status("second").eligible is False

    registry.mark_healthy("first")
    assert registry.status("first").healthy is True
    registry.mark_invalid("first")
    assert registry.status("first").available is False
    assert registry.status("first").eligible is False


def test_model_error_categories_are_explicit() -> None:
    """Provider failures expose safe machine-readable classifications."""
    assert ModelRateLimitError.category is ModelErrorCategory.RATE_LIMITED
    assert ModelAuthenticationError.category is ModelErrorCategory.AUTHENTICATION
    assert ModelTimeoutError.category is ModelErrorCategory.TIMEOUT
    assert ModelUnavailableError.category is ModelErrorCategory.MODEL_UNAVAILABLE


def test_missing_credentials_never_call_that_provider_as_fallback() -> None:
    """An unconfigured primary is skipped while an eligible fallback may run."""
    missing = FirstFakeProvider(credentials=False)
    eligible = SecondFakeProvider([response("second")])
    router = ModelRouter(
        ModelRegistry(
            [
                definition("missing", "first", priority=1),
                definition("eligible", "second", priority=2),
            ]
        ),
        ProviderRegistry([missing, eligible]),
        allow_cross_provider_fallback=True,
        max_attempts_per_model=1,
    )

    result = router.generate(request())

    assert result.provider == "second"
    assert missing.calls == 0
    assert result.metadata["routing"]["attempts"][0]["reason"] == "credentials_unavailable"


def test_same_provider_fallback_is_preferred() -> None:
    """A same-provider candidate wins before an eligible cross-provider candidate."""
    primary = FirstFakeProvider([ModelUnavailableError("temporary"), response("first")])
    cross_provider = SecondFakeProvider([response("second")])
    router = ModelRouter(
        ModelRegistry(
            [
                definition("primary", "first", priority=1),
                definition("same", "first", priority=2),
                definition("cross", "second", priority=3),
            ]
        ),
        ProviderRegistry([primary, cross_provider]),
        allow_cross_provider_fallback=True,
        max_attempts_per_model=1,
    )

    result = router.generate(request())

    assert result.provider == "first"
    assert cross_provider.calls == 0


def test_cross_provider_fallback_is_disabled_by_default() -> None:
    """A failed provider does not silently hand work to another provider."""
    primary = FirstFakeProvider([ModelUnavailableError("temporary")])
    fallback = SecondFakeProvider([response("second")])
    router = ModelRouter(
        ModelRegistry(
            [
                definition("primary", "first", priority=1),
                definition("fallback", "second", priority=2),
            ]
        ),
        ProviderRegistry([primary, fallback]),
        max_attempts_per_model=1,
    )

    with pytest.raises(ModelRoutingError) as failure:
        router.generate(request())

    assert fallback.calls == 0
    assert failure.value.routing_trace[0]["provider"] == "first"


def test_cross_provider_fallback_can_be_enabled_explicitly() -> None:
    """Explicit policy enables only configured, eligible cross-provider candidates."""
    primary = FirstFakeProvider([ModelUnavailableError("temporary")])
    fallback = SecondFakeProvider([response("second")])
    result = ModelRouter(
        ModelRegistry(
            [
                definition("primary", "first", priority=1),
                definition("fallback", "second", priority=2),
            ]
        ),
        ProviderRegistry([primary, fallback]),
        max_attempts_per_model=1,
        allow_cross_provider_fallback=True,
    ).generate(request())

    assert result.provider == "second"
    assert result.metadata["routing"]["attempts"][-1]["provider_changed"] is True


def test_authentication_failure_marks_provider_ineligible_for_the_run() -> None:
    """Authentication failure excludes all later models for that provider."""
    invalid = AuthFakeProvider([ModelAuthenticationError("bad credentials")])
    fallback = SecondFakeProvider([response("second")])
    router = ModelRouter(
        ModelRegistry(
            [
                definition("auth-one", "auth", priority=1),
                definition("auth-two", "auth", priority=2),
                definition("fallback", "second", priority=3),
            ]
        ),
        ProviderRegistry([invalid, fallback]),
        max_attempts_per_model=2,
        allow_cross_provider_fallback=True,
    )

    result = router.generate(request())

    assert result.provider == "second"
    assert invalid.calls == 1
    assert any(
        item["reason"] == "provider_ineligible"
        for item in result.metadata["routing"]["attempts"]
    )


def test_all_candidates_exhausted_returns_structured_failure_without_prompt() -> None:
    """Exhaustion contains a safe routing trace and no request content."""
    provider = RetryFakeProvider([ModelRateLimitError("quota")])
    router = ModelRouter(
        ModelRegistry([definition("rate-limited", "retry")]),
        ProviderRegistry([provider]),
        max_attempts_per_model=2,
    )

    with pytest.raises(ModelRoutingError) as failure:
        router.generate(
            ModelRequest(
                messages=[ModelMessage(role=ModelRole.USER, content="top-secret prompt")],
                capability=ModelCapability.REASONING,
                max_tokens=4,
            )
        )

    serialized = json.dumps(failure.value.routing_trace).lower()
    assert provider.calls == 2
    assert "top-secret" not in serialized
    assert failure.value.routing_trace[0]["reason"] == "rate_limited"


def test_capability_mismatch_is_rejected_without_calling_provider() -> None:
    """Routing never downgrades a request to an unrelated capability."""
    provider = FirstFakeProvider()
    router = ModelRouter(
        ModelRegistry([definition("coding-only", "first", capability=ModelCapability.CODING)]),
        ProviderRegistry([provider]),
    )

    with pytest.raises(ModelRoutingError):
        router.generate(request(ModelCapability.REASONING))
    assert provider.calls == 0


def test_router_does_not_fallback_on_invalid_application_request() -> None:
    """Malformed requests fail immediately instead of hiding behind a fallback."""
    first = FirstFakeProvider([ModelInvalidRequestError("invalid request")])
    second = SecondFakeProvider([response("second")])
    router = ModelRouter(
        ModelRegistry(
            [
                definition("primary", "first", priority=1),
                definition("fallback", "second", priority=2),
            ]
        ),
        ProviderRegistry([first, second]),
        max_attempts_per_model=2,
    )

    with pytest.raises(ModelInvalidRequestError):
        router.route(request())
    assert first.calls == 1
    assert second.calls == 0


def test_router_rejects_requests_with_no_available_model() -> None:
    """No capability match or unavailable provider produces a clear final error."""
    with pytest.raises(NoModelAvailableError):
        ModelRouter(ModelRegistry(), ProviderRegistry()).generate(request())

    with pytest.raises(NoModelAvailableError):
        ModelRouter(
            ModelRegistry([definition("missing", "missing")]), ProviderRegistry()
        ).generate(request())


def test_default_registry_uses_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """API keys and model IDs are read from environment-backed settings."""
    monkeypatch.setenv("GROQ_API_KEY", "secret")
    monkeypatch.setenv("MIGRATIONSWARM_GROQ_FAST_MODEL", "custom-fast")
    from migrationswarm.config import Settings
    from migrationswarm.core.models.registry import default_model_registry
    from migrationswarm.providers.registry import default_provider_registry

    settings = Settings()
    assert settings.groq_api_key is not None
    assert settings.groq_api_key.get_secret_value() == "secret"
    assert default_model_registry(settings).get("groq-fast").provider_model_id == "custom-fast"
    assert default_provider_registry(settings).get("groq").credentials_available


def test_cross_provider_fallback_setting_is_conservative_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-provider fallback defaults off and can be enabled by configuration."""
    from migrationswarm.config import Settings

    assert Settings(_env_file=None).allow_cross_provider_fallback is False  # type: ignore[call-arg]
    monkeypatch.setenv("MODEL_ALLOW_CROSS_PROVIDER_FALLBACK", "true")
    assert Settings(_env_file=None).allow_cross_provider_fallback is True  # type: ignore[call-arg]


def test_models_cli_does_not_call_providers() -> None:
    """The models command reports configured models and credential state only."""
    result = runner.invoke(app, ["models"])

    assert result.exit_code == 0
    assert "MigrationSwarm Models" in result.stdout
    assert "groq-reasoning" in result.stdout
    assert "siliconflow" in result.stdout
    assert "Credentials" in result.stdout


def test_model_check_missing_credentials_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model check reports missing credentials without making a request."""
    monkeypatch.setenv("GROQ_API_KEY", "")
    from migrationswarm.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["model-check", "groq"])

    assert result.exit_code == 0
    assert "provider=groq" in result.stdout
    assert "model=openai/gpt-oss-120b" in result.stdout
    assert "status=credentials_unavailable" in result.stdout
    assert "latency_ms=n/a" in result.stdout


def test_model_check_uses_mocked_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model check can use an injected fake provider without network access."""
    from migrationswarm.cli import main as cli_main

    provider = GroqFakeProvider([response("fake")])
    monkeypatch.setattr(
        cli_main,
        "default_provider_registry",
        lambda settings: ProviderRegistry([provider]),
    )

    result = runner.invoke(app, ["model-check", "groq"])

    assert result.exit_code == 0
    assert "provider=groq" in result.stdout
    assert "model=openai/gpt-oss-120b" in result.stdout
    assert "status=ok" in result.stdout
    assert "latency_ms=" in result.stdout
    assert "secret" not in result.stdout
    assert provider.calls == 1


def test_model_check_can_select_a_capability_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lightweight check can validate the coding route without a live migration."""
    from migrationswarm.cli import main as cli_main

    provider = GroqFakeProvider([response("coding")])
    monkeypatch.setattr(
        cli_main,
        "default_model_registry",
        lambda settings: ModelRegistry(
            [definition("coding-model", "groq", capability=ModelCapability.CODING)]
        ),
    )
    monkeypatch.setattr(
        cli_main,
        "default_provider_registry",
        lambda settings: ProviderRegistry([provider]),
    )

    result = runner.invoke(app, ["model-check", "groq", "--capability", "coding"])

    assert result.exit_code == 0
    assert "model=provider-coding-model" in result.stdout
    assert "status=ok" in result.stdout
    assert provider.calls == 1
