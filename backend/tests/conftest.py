import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

import smtrading.models  # noqa: F401  (every table on Base.metadata, for single-file runs)
from smtrading.common.config import get_settings
from smtrading.common.db import get_sessionmaker, make_engine

LIVE_HOST_MESSAGE = "tentata connessione a un host live nei test"


@pytest.fixture(scope="session", autouse=True)
def _no_live_network():
    """No test ever resolves or connects to a real-money host (LIVE_HOSTS), nor to the
    eToro host (shared by its demo and real APIs: tests use the fake or respx only)."""
    import socket

    from smtrading.brokers.etoro_core import ETORO_URL
    from smtrading.brokers.live import LIVE_HOSTS, hostname

    blocked = LIVE_HOSTS | {hostname(ETORO_URL)}

    def is_live(host) -> bool:
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        return isinstance(host, str) and host.lower().rstrip(".") in blocked

    real_getaddrinfo, real_create_connection = socket.getaddrinfo, socket.create_connection

    def getaddrinfo(host, *args, **kwargs):
        if is_live(host):
            raise RuntimeError(LIVE_HOST_MESSAGE)
        return real_getaddrinfo(host, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        if is_live(address[0]):
            raise RuntimeError(LIVE_HOST_MESSAGE)
        return real_create_connection(address, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket, "getaddrinfo", getaddrinfo)
        mp.setattr(socket, "create_connection", create_connection)
        yield


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    get_settings.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture(autouse=True)
def _reset_activity_alert():
    """The «Registro attività» failure alert goes out once per process until a row is
    written again: every test starts with it armed."""
    from smtrading.useractivity import recorder

    recorder._alerted = False
    yield
    recorder._alerted = False


@pytest.fixture(autouse=True)
def _reset_t212_caches():
    """The Trading 212 (and eToro) instrument caches and rate-limit blocks are process-wide: reset in
    place (modules alias the cache objects) around every test, so what one test loaded
    (e.g. the fake's US-only tickers) never decides another's routability."""
    from smtrading.brokers import etoro_core, t212_core
    from smtrading.marketdata.providers import etoro as etoro_md

    def reset():
        for cache in t212_core._instrument_caches.values():
            cache.data.update(at=0.0, items=None, failed_at=None)
            cache.reverse.clear()
        t212_core._rate_blocked_until.clear()
        for etoro_cache in etoro_core._instrument_caches.values():
            etoro_cache.clear()
        etoro_core._rate_blocked_until.clear()
        etoro_md._blocked_until.clear()
        etoro_md._missing.clear()

    reset()
    yield
    reset()


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


# --- MCP server fixtures -------------------------------------------------------------


class McpTestServer:
    """A live uvicorn SMTrading API for MCP tests. Unpacks as (url, token).

    `url` is the MCP endpoint, `token` the bearer token of the fixture agent (trade on
    the "Demo" portfolio), `qs` the FakeQuoteSource every tool call sees (wrapped in a
    fresh MemoQuoteSource per call), `mcp` the MCPServer, for test-only tools.
    """

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)

    def __iter__(self):
        return iter((self.url, self.token))


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _discovery_fake():
    """FakeMarketData plus the router's probe_quote (on-demand instruments): the quote of
    any symbol set with set_quote, catalog or not."""
    from smtrading.engine.quotesource import FakeMarketData

    class DiscoveryFake(FakeMarketData):
        async def probe_quote(self, symbol, asset_class, provider_symbols=None):
            return (await self.get_quotes([symbol]))[symbol]

    return DiscoveryFake()


@pytest.fixture
async def mcp_server(fresh_db, redis_url, monkeypatch):
    import asyncio
    import threading
    import time
    from decimal import Decimal

    import uvicorn
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smtrading.agents import service as agents
    from smtrading.api.main import create_app
    from smtrading.mcp.runtime import McpRuntime
    from smtrading.portfolios.service import create_portfolio

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    agents.token_cache.clear()
    agents.rejected_tokens.clear()

    engine = make_engine(fresh_db)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        demo = await create_portfolio(s, name="Demo", initial_cash=Decimal(100000))
        agent, token = await agents.create_agent(s, "test-agent")
        await agents.assign_portfolio(s, agent.id, demo.id, "trade")
        await s.commit()
    await engine.dispose()

    qs = _discovery_fake()  # also the router of the market-data tools
    app = create_app(runtime_factory=lambda sm, redis: McpRuntime(sm, redis, qs, qs))
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("mcp test server did not start")
        await asyncio.sleep(0.02)
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield McpTestServer(
            base_url=base_url, url=f"{base_url}/mcp", token=token, agent_id=agent.id,
            demo_id=demo.id, db_url=fresh_db, qs=qs, mcp=app.state.mcp, app=app,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        agents.token_cache.clear()
        agents.rejected_tokens.clear()


def _mcp_session(url: str, token: str):
    import contextlib

    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    @contextlib.asynccontextmanager
    async def session():
        async with (
            create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http,
            streamable_http_client(url, http_client=http) as (read, write),
            ClientSession(read, write) as cs,
        ):
            await cs.initialize()
            yield cs

    return session()


async def mcp_call(url: str, token: str, tool: str, args: dict | None = None):
    """Call an MCP tool; returns (is_error, payload) with the JSON text content parsed."""
    import json

    async with _mcp_session(url, token) as cs:
        result = await cs.call_tool(tool, args or {})
    text = "".join(c.text for c in result.content if c.type == "text")
    return result.is_error, json.loads(text)


async def mcp_list_tools(url: str, token: str) -> list[str]:
    async with _mcp_session(url, token) as cs:
        return [t.name for t in (await cs.list_tools()).tools]


# exposed as fixtures so tests need not import from conftest
mcp_call_fixture = pytest.fixture(name="mcp_call")(lambda: mcp_call)
mcp_list_tools_fixture = pytest.fixture(name="mcp_list_tools")(lambda: mcp_list_tools)
