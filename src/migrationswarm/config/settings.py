"""Pydantic Settings configuration for MigrationSwarm."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or .env."""

    app_name: str = "migrationswarm"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = Field(
        default="sqlite:///./migrationswarm.db", validation_alias="DATABASE_URL"
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0", validation_alias="REDIS_URL"
    )
    verification_workers: int = Field(
        default=1, ge=1, le=2, validation_alias="MIGRATIONSWARM_VERIFICATION_WORKERS"
    )
    allow_cross_provider_fallback: bool = Field(
        default=False, validation_alias="MODEL_ALLOW_CROSS_PROVIDER_FALLBACK"
    )
    groq_api_key: SecretStr | None = Field(default=None, validation_alias="GROQ_API_KEY")
    siliconflow_api_key: SecretStr | None = Field(
        default=None, validation_alias="SILICONFLOW_API_KEY"
    )
    groq_base_url: str = Field(
        default="https://api.groq.com/openai/v1", validation_alias="GROQ_BASE_URL"
    )
    siliconflow_base_url: str = Field(
        default="https://api.siliconflow.cn/v1", validation_alias="SILICONFLOW_BASE_URL"
    )
    groq_reasoning_model: str = Field(
        default="openai/gpt-oss-120b",
        validation_alias="MIGRATIONSWARM_GROQ_REASONING_MODEL",
    )
    groq_fast_model: str = Field(
        default="openai/gpt-oss-20b",
        validation_alias="MIGRATIONSWARM_GROQ_FAST_MODEL",
    )
    groq_qwen_model: str = Field(
        default="qwen/qwen3.8-27b",
        validation_alias="MIGRATIONSWARM_GROQ_QWEN_MODEL",
    )
    siliconflow_qwen_model: str = Field(
        default="Qwen/Qwen2.5-72B-Instruct",
        validation_alias="MIGRATIONSWARM_SILICONFLOW_QWEN_MODEL",
    )
    siliconflow_deepseek_model: str = Field(
        default="deepseek-ai/DeepSeek-V3",
        validation_alias="MIGRATIONSWARM_SILICONFLOW_DEEPSEEK_MODEL",
    )
    siliconflow_glm_model: str | None = Field(
        default=None, validation_alias="MIGRATIONSWARM_SILICONFLOW_GLM_MODEL"
    )

    model_config = SettingsConfigDict(
        env_prefix="MIGRATIONSWARM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached application settings."""
    return Settings()
