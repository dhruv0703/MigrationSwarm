"""Small synchronous adapter for OpenAI-compatible chat-completions APIs."""

from __future__ import annotations

import json
import re
from time import monotonic
from typing import Any, ClassVar

import httpx
from pydantic import SecretStr

from migrationswarm.core.models.exceptions import (
    ModelAuthenticationError,
    ModelInvalidRequestError,
    ModelInvalidResponseError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from migrationswarm.core.models.models import ModelRequest, ModelResponse
from migrationswarm.core.models.provider import ModelProvider
from migrationswarm.core.security import MAX_MODEL_RESPONSE_BYTES, redact_text, require_bytes


def secret_value(value: str | SecretStr | None) -> str | None:
    """Convert a configured secret to its runtime value without exposing it."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


class OpenAICompatibleProvider(ModelProvider):
    """Normalize common chat-completions responses without exposing SDK types."""

    name: ClassVar[str]
    default_base_url: ClassVar[str]
    json_reasoning_format: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(
            base_url=f"{self.base_url}/",
            timeout=timeout,
            transport=transport,
        )
    @property
    def credentials_available(self) -> bool:
        """Return whether a non-empty API key is configured."""
        return bool(self._api_key)

    def generate(self, request: ModelRequest, model: str) -> ModelResponse:
        """Send one chat-completions request and normalize the response."""
        if not self.credentials_available:
            raise ModelAuthenticationError(f"Missing API key for provider: {self.name}")

        payload: dict[str, Any] = {
            "model": model,
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.response_format is not None:
            payload["response_format"] = request.response_format
            if self.json_reasoning_format is not None:
                payload["reasoning_format"] = self.json_reasoning_format
        started = monotonic()
        try:
            response = self._client.post(
                "chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.TimeoutException as error:
            raise ModelTimeoutError(f"Provider request timed out: {self.name}") from error
        except httpx.RequestError as error:
            raise ModelUnavailableError(f"Provider request unavailable: {self.name}") from error

        self._raise_for_status(response)
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_MODEL_RESPONSE_BYTES:
                    raise ModelInvalidResponseError("Provider response exceeded the size limit")
            except ValueError:
                pass
        try:
            require_bytes(response.content, MAX_MODEL_RESPONSE_BYTES, label="model response")
        except ValueError as error:
            raise ModelInvalidResponseError("Provider response exceeded the size limit") from error
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            content = message["content"]
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ModelInvalidResponseError(
                "Provider returned an invalid model response"
            ) from error

        if not isinstance(content, str):
            raise ModelInvalidResponseError("Provider returned unsupported message content")
        return ModelResponse(
            provider=self.name,
            model=str(data.get("model") or model),
            content=content,
            input_tokens=_token_count(usage, "prompt_tokens", "input_tokens"),
            output_tokens=_token_count(usage, "completion_tokens", "output_tokens"),
            latency_ms=(monotonic() - started) * 1000,
            finish_reason=choice.get("finish_reason"),
            metadata={
                "request_id": response.headers.get("x-request-id"),
            },
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        if response.status_code == 400 and _error_code(response) == "json_validate_failed":
            raise ModelInvalidResponseError(
                "Provider rejected the generated response format"
            )
        if response.status_code in {401, 403}:
            raise ModelAuthenticationError("Provider rejected authentication")
        if response.status_code == 429 or (
            response.status_code == 413 and _error_code(response) == "rate_limit_exceeded"
        ):
            detail = _safe_error_detail(response)
            suffix = f": {detail}" if detail else ""
            raise ModelRateLimitError(f"Provider rate limit reached{suffix}")
        if response.status_code in {408, 409, 425, 500, 502, 503, 504}:
            raise ModelUnavailableError(
                f"Provider temporarily unavailable (status {response.status_code})"
            )
        detail = _safe_error_detail(response)
        suffix = f": {detail}" if detail else ""
        raise ModelInvalidRequestError(
            f"Provider rejected the request (status {response.status_code}){suffix}"
        )


def _token_count(usage: object, *keys: str) -> int | None:
    """Read the first available integer token count from provider usage data."""
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _safe_error_detail(response: httpx.Response) -> str | None:
    """Extract only bounded provider error fields, never request content."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    fields = []
    for key in ("type", "code", "message"):
        value = error.get(key)
        if isinstance(value, str) and value:
            sanitized = re.sub(r"\s+", " ", value).strip()
            sanitized = redact_text(sanitized)
            fields.append(f"{key}={sanitized[:240]}")
    return ", ".join(fields) or None


def _error_code(response: httpx.Response) -> str | None:
    """Read only the structured provider error code for status classification."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return None
    code = payload["error"].get("code")
    return code if isinstance(code, str) else None
