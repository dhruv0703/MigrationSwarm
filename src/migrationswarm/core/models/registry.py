"""Central logical model registry and defaults."""

from migrationswarm.config import Settings, get_settings
from migrationswarm.core.models.enums import ModelCapability
from migrationswarm.core.models.exceptions import DuplicateModelError, UnknownModelError
from migrationswarm.core.models.models import ModelDefinition


class ModelRegistry:
    """Store logical model definitions separately from provider implementations."""

    def __init__(self, models: list[ModelDefinition] | None = None) -> None:
        self._models: dict[str, ModelDefinition] = {}
        for model in models or []:
            self.register(model)

    def register(self, model: ModelDefinition) -> None:
        """Register a logical model, rejecting duplicate names."""
        if model.logical_name in self._models:
            raise DuplicateModelError(f"Model already registered: {model.logical_name}")
        self._models[model.logical_name] = model

    def get(self, logical_name: str) -> ModelDefinition:
        """Return one logical model definition."""
        try:
            return self._models[logical_name]
        except KeyError as error:
            raise UnknownModelError(f"Unknown model: {logical_name}") from error

    def list_models(self) -> tuple[ModelDefinition, ...]:
        """Return models in deterministic logical-name order."""
        return tuple(self._models[name] for name in sorted(self._models))

    def for_capability(self, capability: ModelCapability) -> tuple[ModelDefinition, ...]:
        """Return enabled models for a capability in priority order."""
        return tuple(
            sorted(
                (
                    model
                    for model in self._models.values()
                    if model.enabled and capability in model.capabilities
                ),
                key=lambda model: (model.priority, model.logical_name),
            )
        )


def default_model_registry(settings: Settings | None = None) -> ModelRegistry:
    """Build the centralized initial model catalog from settings."""
    runtime = settings or get_settings()
    models = [
        ModelDefinition(
            logical_name="groq-reasoning",
            provider="groq",
            provider_model_id=runtime.groq_reasoning_model,
            capabilities=frozenset(
                {
                    ModelCapability.REASONING,
                    ModelCapability.ARCHITECTURE,
                    ModelCapability.VERIFICATION,
                }
            ),
            priority=10,
            notes="Strong general reasoning model.",
        ),
        ModelDefinition(
            logical_name="groq-fast",
            provider="groq",
            provider_model_id=runtime.groq_fast_model,
            capabilities=frozenset(
                {ModelCapability.CLASSIFICATION, ModelCapability.SUMMARIZATION}
            ),
            priority=20,
            notes="Fast lightweight model.",
        ),
        ModelDefinition(
            logical_name="groq-qwen",
            provider="groq",
            provider_model_id=runtime.groq_qwen_model,
            capabilities=frozenset({ModelCapability.CODING, ModelCapability.REASONING}),
            priority=30,
            notes="Qwen-family coding model hosted by Groq.",
        ),
        ModelDefinition(
            logical_name="qwen-coder",
            provider="siliconflow",
            provider_model_id=runtime.siliconflow_qwen_model,
            capabilities=frozenset({ModelCapability.CODING, ModelCapability.REASONING}),
            priority=30,
            notes="Qwen-family coding model.",
        ),
        ModelDefinition(
            logical_name="deepseek-reasoning",
            provider="siliconflow",
            provider_model_id=runtime.siliconflow_deepseek_model,
            capabilities=frozenset({ModelCapability.REASONING, ModelCapability.VERIFICATION}),
            priority=40,
            notes="DeepSeek-family reasoning model.",
        ),
    ]
    if runtime.siliconflow_glm_model:
        models.append(
            ModelDefinition(
                logical_name="glm-general",
                provider="siliconflow",
                provider_model_id=runtime.siliconflow_glm_model,
                capabilities=frozenset(
                    {ModelCapability.REASONING, ModelCapability.SUMMARIZATION}
                ),
                priority=50,
                notes="Optional GLM-family model.",
            )
        )
    return ModelRegistry(models)
