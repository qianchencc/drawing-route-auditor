from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DRA_",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql://drawing_route_auditor:drawing_route_auditor_dev@localhost:55434/drawing_route_auditor"
    )
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    openai_base_url: str | None = Field(
        default=None, validation_alias="OPENAI_BASE_URL"
    )
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias="OPENAI_API_KEY"
    )
    vision_model: str | None = Field(default=None, validation_alias="MODEL")
    vision_timeout_seconds: int = Field(default=25, ge=1, le=120)
    render_dpi: int = Field(default=120, ge=72, le=300)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
