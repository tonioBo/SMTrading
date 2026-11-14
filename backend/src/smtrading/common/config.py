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
    # e2e only: live adapters talk to the in-memory FakeLiveBroker (the live gate stays on)
    live_fake_broker: bool = False
    # «Istruzioni agenti» seed folder (docs/paperclip-agents); None = the checkout's
    agent_docs_dir: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
