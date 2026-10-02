"""Default provider construction from environment settings."""

from migrationswarm.config import Settings, get_settings
from migrationswarm.core.models import ProviderRegistry
from migrationswarm.providers.groq import GroqProvider
from migrationswarm.providers.siliconflow import SiliconFlowProvider


def default_provider_registry(settings: Settings | None = None) -> ProviderRegistry:
    """Build the configured provider set without making network requests."""
    runtime = settings or get_settings()
    return ProviderRegistry(
        [GroqProvider(settings=runtime), SiliconFlowProvider(settings=runtime)]
    )
