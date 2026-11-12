import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from smtrading.common.config import get_settings
from smtrading.common.db import get_sessionmaker, make_engine


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    get_settings.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16") as pg:
        url = pg.get_connection_url()
        # testcontainers returns postgresql+psycopg2://...; we use asyncpg
        yield "postgresql+asyncpg://" + url.split("://", 1)[1]


@pytest.fixture(scope="session")
def redis_url():
    with RedisContainer("redis:7") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


@pytest.fixture
async def app_client(pg_url, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()

    from smtrading.api.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await app.state.close_resources()


BACKEND_DIR = Path(__file__).resolve().parent.parent


def run_migrations(url: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", "-x", f"url={url}", "upgrade", "head"],
        cwd=BACKEND_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def migrated_db(pg_url):
    run_migrations(pg_url)
    return pg_url


@pytest.fixture
async def db_session(migrated_db):
    engine = make_engine(migrated_db)
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def fresh_db(pg_url):
    """A brand-new migrated database on the shared container, dropped at teardown.

    For tests that must commit (ledger tables are immutable, so committed rows could
    never be cleaned out of the shared migrated_db).
    """
    name = f"t_{uuid.uuid4().hex}"
    admin = create_async_engine(
        make_url(pg_url).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
        run_migrations(url)
        yield url
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture
async def portfolio(db_session):
    from smtrading.portfolios.models import Portfolio

    p = Portfolio(name="T")
    db_session.add(p)
    await db_session.flush()
    return p


@pytest.fixture
async def cash_and_equity(db_session, portfolio):
    """(cash EUR account of `portfolio`, system_equity EUR account)."""
    from smtrading.ledger.models import AccountKind
    from smtrading.ledger.service import get_or_create_account

    cash = await get_or_create_account(
        db_session, portfolio_id=portfolio.id, asset="EUR", kind=AccountKind.cash
    )
    eq = await get_or_create_account(
        db_session, portfolio_id=None, asset="EUR", kind=AccountKind.system_equity
    )
    return cash, eq


@pytest.fixture
async def posted_journal(db_session):
    """A portfolio, two accounts, one journal and two lines, inserted with raw SQL."""
    pid, a1, a2, jid = (uuid.uuid4() for _ in range(4))
    stmts = [
        (("insert into portfolios (id, name, base_currency, leverage, maintenance_margin_pct,"
         " warning_margin_pct, slippage_bps, status, timezone) values"
         " (:pid, :name, 'EUR', 1, 50, 75, 0, 'active', 'Europe/Rome')"),
         {"pid": pid, "name": f"p-{pid}"}),
        ("insert into accounts (id, portfolio_id, asset, kind) values (:a1, :pid, 'EUR', 'cash')",
         {"a1": a1, "pid": pid}),
        (("insert into accounts (id, portfolio_id, asset, kind) values"
         " (:a2, null, 'EUR', 'system_equity')"), {"a2": a2}),
        (("insert into journals (id, type, author_type, prev_hash, hash) values"
         " (:jid, 'DEPOSIT', 'system', :ph, :h)"),
         {"jid": jid, "ph": "0" * 64, "h": "a" * 64}),
        ("insert into journal_lines (journal_id, account_id, quantity) values (:jid, :a1, 100)",
         {"jid": jid, "a1": a1}),
        ("insert into journal_lines (journal_id, account_id, quantity) values (:jid, :a2, -100)",
         {"jid": jid, "a2": a2}),
    ]
    for sql, params in stmts:
        await db_session.execute(text(sql), params)
    return jid
