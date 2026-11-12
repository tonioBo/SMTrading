from smtrading.common.config import get_settings
from smtrading.common.errors import ErrorCode, SMTError


def test_smterror_to_dict():
    e = SMTError(ErrorCode.INSUFFICIENT_FUNDS, "no cash", {"need": "10"})
    assert e.to_dict() == {
        "code": "INSUFFICIENT_FUNDS",
        "message": "no cash",
        "details": {"need": "10"},
    }


def test_settings_from_env(monkeypatch):
    env = {
        "SMT_DATABASE_URL": "postgresql+asyncpg://x",
        "SMT_REDIS_URL": "redis://r",
        "SMT_MASTER_KEY": "k",
        "SMT_ADMIN_EMAIL": "a@b.c",
        "SMT_ADMIN_PASSWORD": "p",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    s = get_settings()
    assert s.database_url == "postgresql+asyncpg://x" and s.tz == "Europe/Rome"
    get_settings.cache_clear()
