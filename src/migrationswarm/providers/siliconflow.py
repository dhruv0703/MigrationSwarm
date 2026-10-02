"""SiliconFlow model provider."""

import httpx

from migrationswarm.config import Settings, get_settings
from migrationswarm.providers.openai_compatible import OpenAICompatibleProvider, secret_value


class SiliconFlowProvider(OpenAICompatibleProvider):
    """Synchronous SiliconFlow chat-completions integration."""

    name = "siliconflow"
    default_base_url = "https://api.siliconflow.cn/v1"

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
            api_key=api_key
            if api_key is not None
            else secret_value(runtime.siliconflow_api_key),
            base_url=base_url if base_url is not None else runtime.siliconflow_base_url,
            timeout=timeout,
            client=client,
            transport=transport,
        )
