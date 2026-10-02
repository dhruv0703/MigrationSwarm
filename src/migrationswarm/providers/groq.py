"""Groq model provider."""

import httpx

from migrationswarm.config import Settings, get_settings
from migrationswarm.providers.openai_compatible import OpenAICompatibleProvider, secret_value


class GroqProvider(OpenAICompatibleProvider):
    """Synchronous Groq chat-completions integration."""

    name = "groq"
    default_base_url = "https://api.groq.com/openai/v1"
    json_reasoning_format = "hidden"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        settings: Settings | None = None,
    ) -> None:
        runtime = settings or get_settings()
        super().__init__(
            api_key=api_key if api_key is not None else secret_value(runtime.groq_api_key),
            base_url=base_url if base_url is not None else runtime.groq_base_url,
            timeout=timeout,
            client=client,
            transport=transport,
        )
