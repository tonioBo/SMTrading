import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.routing import Route

import smtrading.models  # noqa: F401  (complete mapper registry before any session)
from smtrading.admin.errors import install_error_handlers
from smtrading.admin.routes_activity_log import router as activity_log_router
from smtrading.admin.routes_agent_docs import router as agent_docs_router
from smtrading.admin.routes_agents import router as agents_router
from smtrading.admin.routes_auth import router as auth_router
from smtrading.admin.routes_brokers import router as brokers_router
from smtrading.admin.routes_connectors import router as connectors_router
from smtrading.admin.routes_dashboard import router as dashboard_router
from smtrading.admin.routes_equity_history import router as equity_history_router
from smtrading.admin.routes_fees import router as fees_router
from smtrading.admin.routes_halts import router as halts_router
from smtrading.admin.routes_live import emergency_router as live_emergency_router
from smtrading.admin.routes_live import router as live_router
from smtrading.admin.routes_markets import router as markets_router
from smtrading.admin.routes_orders import router as orders_router
from smtrading.admin.routes_paperclip import router as paperclip_router
from smtrading.admin.routes_portfolios import router as portfolios_router
from smtrading.admin.routes_risk import emergency_router as risk_emergency_router
from smtrading.admin.routes_risk import router as risk_router
from smtrading.admin.routes_signals import router as signals_router
from smtrading.admin.routes_system import router as system_router
from smtrading.admin.routes_telegram import router as telegram_router
from smtrading.admin.routes_users import router as users_router
from smtrading.admin.routes_watch import router as watch_router
from smtrading.admin.ws import router as ws_router
from smtrading.agentdocs import export as agent_docs_export
from smtrading.brokers.live.startup import warn_if_fake_broker
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.common.heartbeat import heartbeat_age_s
from smtrading.common.logsetup import configure_logging
from smtrading.instruments.closures import ClosureRefresher
from smtrading.marketdata.router import MarketDataRouter
from smtrading.mcp.auth import MCP_PATH, McpAuthMiddleware
from smtrading.mcp.runtime import McpRuntime, get_runtime, set_runtime
from smtrading.mcp.server import build_mcp, build_mcp_endpoint
from smtrading.useractivity.recorder import ActivityMiddleware

log = logging.getLogger(__name__)
HEARTBEAT_MAX_AGE_S = 30.0


class _Resources:
    """Lazily created engine/redis client; nothing connects at import or app creation."""

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._redis: Redis | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = make_engine(get_settings().database_url)
        return self._engine

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(get_settings().redis_url)
        return self._redis

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


BUILD_HEADER = "X-SMT-Build"


def build_id() -> str:
    """Set at image build (git sha or timestamp); the web bundle embeds the same id."""
    return os.environ.get("SMT_BUILD_ID") or "dev"


class _BuildHeader:
    """Pure ASGI: adds X-SMT-Build to every HTTP response (also errors and the MCP route)."""

    def __init__(self, app) -> None:
        self.app = app
        self.value = build_id().encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_header(message):
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", [])
                           if h[0].lower() != b"x-smt-build"]
                message = {**message, "headers": [*headers, (b"x-smt-build", self.value)]}
            await send(message)

        return await self.app(scope, receive, send_with_header)


RuntimeFactory = Callable[[async_sessionmaker[AsyncSession], Redis], McpRuntime]


async def _register_live_secrets(sessionmaker) -> None:
    """The live broker keys are masked in this process's logs from the start (they are
    registered again on every decrypt); best-effort: a database not reachable yet is
    not a reason to refuse to start."""
    from smtrading.brokers.live.account import register_live_secrets

    try:
        async with sessionmaker() as session:
            await register_live_secrets(session)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("live keys not registered at start")


async def _export_agent_docs(sessionmaker) -> None:
    """Startup export of the «Istruzioni agenti» for Paperclip (never fails the boot)."""
    try:
        async with sessionmaker() as session:
            await agent_docs_export.export_all(session)
    except Exception:
        logging.getLogger(__name__).warning("agent docs export at startup failed", exc_info=True)


def create_app(runtime_factory: RuntimeFactory | None = None) -> FastAPI:
    """`runtime_factory(sessionmaker, redis)` overrides the MCP runtime (tests inject a
    FakeQuoteSource); by default the MarketDataRouter is the MCP quote source."""
    # uvicorn has configured its own loggers (they keep their handlers, access log
    # included) but not the root: add a WARNING root handler so smtrading.* records go
    # through the redacting filter too, and mask secrets in uvicorn's handlers
    configure_logging(logging.WARNING,
                      extra_loggers=("uvicorn", "uvicorn.error", "uvicorn.access"))
    resources = _Resources()
    mcp = build_mcp()
    mcp_endpoint = build_mcp_endpoint(mcp)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sessionmaker = async_sessionmaker(resources.engine, expire_on_commit=False)
        await _register_live_secrets(sessionmaker)
        await warn_if_fake_broker(sessionmaker, process="api")
        await _export_agent_docs(sessionmaker)
        # manual market closures: loaded now, then every 60 s (an admin write reloads too)
        closures = asyncio.create_task(ClosureRefresher(sessionmaker).run())
        try:
            async with httpx.AsyncClient() as http:
                if runtime_factory is not None:
                    runtime = runtime_factory(sessionmaker, resources.redis)
                else:
                    router = MarketDataRouter(sessionmaker, resources.redis, http)
                    runtime = McpRuntime(sessionmaker, resources.redis, router, router)
                set_runtime(runtime)
                app.state.mcp_runtime = runtime
                app.state.sessionmaker = sessionmaker
                app.state.redis = resources.redis
                async with mcp.session_manager.run():
                    yield
        finally:
            closures.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await closures
            set_runtime(None)
            await resources.close()

    app = FastAPI(title="SMTrading", lifespan=lifespan)
    app.state.close_resources = resources.close
    app.state.mcp = mcp
    # raw ASGI routes (all methods) at /mcp and /mcp/: no slash redirect either way
    app.router.routes.append(Route(MCP_PATH, endpoint=mcp_endpoint))
    app.router.routes.append(Route(MCP_PATH + "/", endpoint=mcp_endpoint))
    # every unsafe /api request of a person goes to the «Registro attività»
    app.add_middleware(ActivityMiddleware)
    app.add_middleware(McpAuthMiddleware, get_sessionmaker=lambda: get_runtime().sessionmaker)
    app.add_middleware(_BuildHeader)
    install_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(equity_history_router)  # before /portfolios/{portfolio_id}
    app.include_router(portfolios_router)
    app.include_router(orders_router)
    app.include_router(agents_router)
    app.include_router(agent_docs_router)
    app.include_router(fees_router)
    app.include_router(risk_router)
    app.include_router(risk_emergency_router)
    app.include_router(halts_router)
    app.include_router(connectors_router)
    app.include_router(brokers_router)
    app.include_router(live_router)
    app.include_router(live_emergency_router)
    app.include_router(markets_router)
    app.include_router(system_router)
    app.include_router(dashboard_router)
    app.include_router(signals_router)
    app.include_router(watch_router)
    app.include_router(telegram_router)
    app.include_router(paperclip_router)
    app.include_router(users_router)
    app.include_router(activity_log_router)
    app.include_router(ws_router)

    @app.get("/health")
    async def health(response: Response) -> dict:
        db_ok = False
        redis_ok = False
        age: float | None = None
        try:
            async with resources.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            log.warning("health check failed", exc_info=True)
        try:
            await resources.redis.ping()
            redis_ok = True
            age = await heartbeat_age_s(resources.redis)
        except Exception:
            log.warning("health check failed", exc_info=True)
        healthy = db_ok and redis_ok and age is not None and age < HEARTBEAT_MAX_AGE_S
        if not healthy:
            response.status_code = 503
        return {
            "status": "ok" if healthy else "degraded",
            "db": db_ok,
            "redis": redis_ok,
            "worker_heartbeat_age_s": age,
        }

    return app
