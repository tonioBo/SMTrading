"""Startup warning when live_fake_broker is on: a WARNING and one «live» Telegram outbox
message per process boot; nothing in the activity feed (agents must not hear of it)."""

import logging

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.brokers.live import startup
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.telegram.models import TelegramOutbox


@pytest.fixture
async def sm(fresh_db, monkeypatch):
    for name, value in (("SMT_DATABASE_URL", fresh_db), ("SMT_REDIS_URL", "redis://h:1/0"),
                        ("SMT_MASTER_KEY", "test-master-key"),
                        ("SMT_ADMIN_EMAIL", "a@example.com"), ("SMT_ADMIN_PASSWORD", "p")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(startup, "_warned", set())
    engine = make_engine(fresh_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _rows(sm):
    async with sm() as s:
        rows = list(await s.scalars(select(TelegramOutbox)))
        acts = await s.scalar(select(func.count()).select_from(Activity))
    return rows, acts


async def test_fake_broker_on_warns_once_per_process(sm, monkeypatch, caplog):
    monkeypatch.setenv("SMT_LIVE_FAKE_BROKER", "true")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="smtrading.brokers.live.startup"):
        assert await startup.warn_if_fake_broker(sm, process="api") is True
        assert await startup.warn_if_fake_broker(sm, process="api") is True  # same boot
        assert await startup.warn_if_fake_broker(sm, process="worker") is True
    assert any("live_fake_broker is ON in api" in r.getMessage() for r in caplog.records)
    rows, acts = await _rows(sm)
    assert len(rows) == 2 and {r.category for r in rows} == {"live"}
    assert all("broker finto" in r.text for r in rows)
    assert acts == 0


async def test_fake_broker_off_says_nothing(sm, monkeypatch, caplog):
    monkeypatch.setenv("SMT_LIVE_FAKE_BROKER", "false")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING):
        assert await startup.warn_if_fake_broker(sm, process="api") is False
    assert not any("live_fake_broker" in r.getMessage() for r in caplog.records)
    assert (await _rows(sm))[0] == []


def test_api_and_worker_call_it_at_startup():
    import inspect

    from smtrading.api import main as api_main
    from smtrading.worker import main as worker_main

    assert 'warn_if_fake_broker(sessionmaker, process="api")' in inspect.getsource(api_main)
    assert 'warn_if_fake_broker(sessionmaker, process="worker")' in inspect.getsource(
        worker_main)
