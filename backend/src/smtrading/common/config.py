from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SMT_", env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    master_key: str
    tz: str = "Europe/Rome"
    admin_email: str
    admin_password: str


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
