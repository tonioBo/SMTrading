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
    # where the agents' AGENTS.md files for Paperclip's External mode are written
    agent_docs_export_dir: str = "/agent-instructions"
    # the api health endpoint the worker waits for before its alerting jobs start
    api_health_url: str = "http://api:8000/health"
    # Neurograph's «serie» compartment for get_forecast (e.g. http://192.168.4.4:18088/api/serie
    # over the WireGuard VPN); None = the tool answers PROVIDER_UNAVAILABLE
    neurograph_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
