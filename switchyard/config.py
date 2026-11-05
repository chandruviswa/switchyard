from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./switchyard.db"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "dev-only-secret-change-me-dev-only-secret"
    jwt_minutes: int = 60

    default_rate_limit_per_minute: int = 120
    login_attempts_per_minute: int = 5

    webhook_timeout_seconds: float = 10
    webhook_max_attempts: int = 6
    # Only for local testing. Lets webhooks point at localhost and plain http.
    webhook_allow_private_targets: bool = False

    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.environment == "production" and settings.jwt_secret.startswith("dev-only"):
        raise RuntimeError("Set JWT_SECRET before running in production.")
    return settings
