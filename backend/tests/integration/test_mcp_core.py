import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from smtrading.agents import service as agents
from smtrading.agents.models import McpAuditLog
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.runtime import run_tool


async def _audit_rows(db_url: str, method: str | None = None) -> list[McpAuditLog]:
    engine = make_engine(db_url)
    try:
        async with engine.connect() as conn:
            stmt = select(McpAuditLog).order_by(McpAuditLog.id)
            if method is not None:
                stmt = stmt.where(McpAuditLog.method == method)
            return list((await conn.execute(stmt)).all())
    finally:
        await engine.dispose()


async def test_list_tools_contains_list_portfolios(mcp_server, mcp_list_tools):
    names = await mcp_list_tools(mcp_server.url, mcp_server.token)
    assert "list_portfolios" in names


async def test_missing_or_bad_token_is_401_before_mcp(mcp_server):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient() as http:
        for path in ("/mcp", "/mcp/"):
            r = await http.post(mcp_server.base_url + path, json=body, headers=headers)
            assert r.status_code == 401
            assert r.json() == {"code": "FORBIDDEN", "message": "invalid token"}
        bad = {**headers, "Authorization": "Bearer smt_zzzzzzzz_" + "A" * 40}
        r = await http.post(mcp_server.url, json=body, headers=bad)
        assert r.status_code == 401
        assert r.json()["code"] == "FORBIDDEN"
        # other routes are untouched by the middleware
        assert (await http.get(mcp_server.base_url + "/health")).status_code in (200, 503)


async def test_trailing_slash_path_works(mcp_server, mcp_call):
    is_error, payload = await mcp_call(mcp_server.url + "/", mcp_server.token, "list_portfolios")
    assert not is_error
    assert [p["name"] for p in payload] == ["Demo"]


async def _set_hide_mode(db_url: str, on: bool) -> None:
    from sqlalchemy.dialects.postgresql import insert

    from smtrading.risk.models import AppSetting

    engine = make_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                insert(AppSetting).values(key="mcp_hide_mode", value={"on": on})
                .on_conflict_do_update(index_elements=["key"], set_={"value": {"on": on}}))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("hide_mode", [None, False], ids=["hide-mode-default-on", "hide-mode-off"])
async def test_list_portfolios_only_assigned(mcp_server, mcp_call, hide_mode):
    """With `mcp_hide_mode` on (the seeded default) a simulated portfolio has no `mode`;
    with the setting off the agent sees `simulazione`."""
    from smtrading.portfolios.service import create_portfolio

    if hide_mode is not None:
        await _set_hide_mode(mcp_server.db_url, hide_mode)

    engine = make_engine(mcp_server.db_url)
    try:
        async with engine.connect() as conn:
            from sqlalchemy.ext.asyncio import AsyncSession

            async with AsyncSession(bind=conn) as s:
                other = await create_portfolio(s, name="Other", initial_cash=Decimal(10))
                ro = await create_portfolio(s, name="ReadOnly", initial_cash=Decimal(10))
                await agents.assign_portfolio(s, mcp_server.agent_id, ro.id, "read")
                other_id = other.id
                await s.commit()
    finally:
        await engine.dispose()

    is_error, payload = await mcp_call(mcp_server.url, mcp_server.token, "list_portfolios")
    assert not is_error
    by_name = {p["name"]: p for p in payload}
    assert set(by_name) == {"Demo", "ReadOnly"}
    assert str(other_id) not in {p["id"] for p in payload}
    assert by_name["Demo"] == {
        "id": str(mcp_server.demo_id),
        "name": "Demo",
        "base_currency": "EUR",
        "status": "active",
        "in_totals": True,
        **({} if hide_mode is None else {"mode": "simulazione", "live_state": None}),
        "permission": "trade",
    }
    assert by_name["ReadOnly"]["permission"] == "read"


async def test_unexpected_exception_is_internal_and_audited(mcp_server, mcp_call):
    async def _boom(ctx):
        raise RuntimeError("secret detail")

    @mcp_server.mcp.tool()
    async def test_boom(x: int = 0):
        return await run_tool("test_boom", {"x": x}, _boom)

    is_error, payload = await mcp_call(mcp_server.url, mcp_server.token, "test_boom", {"x": 3})
    assert is_error
    assert payload == {"code": "INTERNAL", "message": "internal error", "details": {}}
    rows = await _audit_rows(mcp_server.db_url, "test_boom")
    assert len(rows) == 1
    assert rows[0].outcome == "INTERNAL"
    assert rows[0].agent_id == mcp_server.agent_id
    assert rows[0].params == {"x": 3}


async def test_smt_error_maps_to_json_error(mcp_server, mcp_call):
    async def _denied(ctx):
        raise SMTError(ErrorCode.NOT_FOUND, "nope", {"id": "x"})

    async def _bad_value(ctx):
        from smtrading.mcp.dto import parse_decimal

        parse_decimal("abc", "qty")

    @mcp_server.mcp.tool()
    async def test_denied():
        return await run_tool("test_denied", {}, _denied)

    @mcp_server.mcp.tool()
    async def test_bad_value():
        return await run_tool("test_bad_value", {}, _bad_value)

    is_error, payload = await mcp_call(mcp_server.url, mcp_server.token, "test_denied")
    assert is_error
    assert payload == {"code": "NOT_FOUND", "message": "nope", "details": {"id": "x"}}
    is_error, payload = await mcp_call(mcp_server.url, mcp_server.token, "test_bad_value")
    assert is_error
    assert payload["code"] == "INVALID_ORDER"
    assert payload["details"]["field"] == "qty"
    assert [r.outcome for r in await _audit_rows(mcp_server.db_url, "test_denied")] == [
        "NOT_FOUND"
    ]


async def test_revoked_agent_gets_401_on_next_call(mcp_server, mcp_call):
    is_error, _ = await mcp_call(mcp_server.url, mcp_server.token, "list_portfolios")
    assert not is_error  # token now cached

    engine = make_engine(mcp_server.db_url)
    try:
        async with engine.connect() as conn:
            from sqlalchemy.ext.asyncio import AsyncSession

            async with AsyncSession(bind=conn) as s:
                await agents.set_agent_active(s, mcp_server.agent_id, False)
                await s.commit()
    finally:
        await engine.dispose()

    async with httpx.AsyncClient() as http:
        r = await http.post(
            mcp_server.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {mcp_server.token}",
            },
        )
    assert r.status_code == 401
    assert r.json() == {"code": "FORBIDDEN", "message": "invalid token"}


async def test_every_call_is_audited(mcp_server, mcp_call):
    for _ in range(3):
        await mcp_call(mcp_server.url, mcp_server.token, "list_portfolios")
    rows = await _audit_rows(mcp_server.db_url, "list_portfolios")
    assert len(rows) == 3
    assert all(r.outcome == "ok" and r.agent_id == mcp_server.agent_id for r in rows)
    assert all(r.latency_ms >= 0 for r in rows)
    assert all(mcp_server.token not in json.dumps(r.params) for r in rows)


async def test_auth_updates_last_seen(mcp_server, mcp_call):
    from smtrading.agents.models import Agent

    await mcp_call(mcp_server.url, mcp_server.token, "list_portfolios")
    engine = make_engine(mcp_server.db_url)
    try:
        async with engine.connect() as conn:
            seen = await conn.scalar(
                select(func.count()).select_from(Agent).where(
                    Agent.id == mcp_server.agent_id, Agent.last_seen_at.is_not(None)
                )
            )
    finally:
        await engine.dispose()
    assert seen == 1


class _RecordingRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


async def _runner_setup(db_url: str):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smtrading.engine.quotesource import FakeQuoteSource
    from smtrading.mcp.runtime import McpRuntime

    engine = make_engine(db_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        agent, _ = await agents.create_agent(s, "runner")
        await s.commit()
    redis = _RecordingRedis()
    return engine, McpRuntime(sm, redis, FakeQuoteSource(), None), agent.id


async def test_run_tool_commit_semantics(fresh_db):
    from smtrading.engine.quotesource import MemoQuoteSource
    from smtrading.events import queue_event
    from smtrading.mcp import runtime as rt
    from smtrading.mcp.auth import current_agent
    from smtrading.portfolios.models import Portfolio

    engine, runtime, agent_id = await _runner_setup(fresh_db)
    prev = rt.get_runtime_or_none()
    rt.set_runtime(runtime)
    token = current_agent.set(agent_id)
    seen = []
    try:
        def make(name: str, fail: bool, persisted: bool = False):
            async def fn(ctx):
                seen.append(ctx)
                ctx.session.add(Portfolio(name=name))
                queue_event(ctx.session, "evt", {"name": name})
                if fail:
                    raise SMTError(ErrorCode.INSUFFICIENT_FUNDS, "no", persisted=persisted)
                return {"name": name}
            return fn

        ok = await run_tool("t_ok", {}, make("ok", False))
        assert not ok.is_error and json.loads(ok.content[0].text) == {"name": "ok"}
        bad = await run_tool("t_bad", {}, make("rolled", True, persisted=True))
        assert bad.is_error
        kept = await run_tool(
            "t_kept", {}, make("kept", True, persisted=True), commit_on_error=True
        )
        assert kept.is_error and json.loads(kept.content[0].text)["code"] == "INSUFFICIENT_FUNDS"
        # commit_on_error keeps persisted rejections only: any other error rolls back
        other = await run_tool("t_other", {}, make("other", True), commit_on_error=True)
        assert other.is_error

        async with runtime.sessionmaker() as s:
            names = set(await s.scalars(select(Portfolio.name)))
        assert "ok" in names and "kept" in names
        assert "rolled" not in names and "other" not in names
        published = [json.loads(m)["name"] for _, m in runtime.redis.published]
        assert published == ["ok", "kept"]
        # each call gets its own memo over the runtime quote source
        assert all(isinstance(c.qs, MemoQuoteSource) for c in seen)
        assert len({id(c.qs) for c in seen}) == 4
        assert all(c.agent_id == agent_id and c.router is None for c in seen)
    finally:
        current_agent.reset(token)
        rt.set_runtime(prev)
        await engine.dispose()


async def test_run_tool_without_agent_is_forbidden(fresh_db):
    from smtrading.mcp import runtime as rt

    engine, runtime, _ = await _runner_setup(fresh_db)
    prev = rt.get_runtime_or_none()
    rt.set_runtime(runtime)
    try:
        async def fn(ctx):
            raise AssertionError("must not run")

        res = await run_tool("t_noagent", {}, fn)
        assert res.is_error
        assert json.loads(res.content[0].text)["code"] == "FORBIDDEN"
        rows = await _audit_rows(fresh_db, "t_noagent")
        assert [(r.outcome, r.agent_id) for r in rows] == [("FORBIDDEN", None)]
    finally:
        rt.set_runtime(prev)
        await engine.dispose()


@pytest.mark.parametrize(("tool", "args", "field"), [
    ("get_quote", {}, "symbols"),
    ("get_triggered_alerts", {"limit": 1.5}, "limit"),
    ("get_portfolio", {"portfolio_id": 7}, "portfolio_id"),
])
async def test_schema_errors_are_json_invalid_order_and_audited(
    mcp_server, mcp_call, tool, args, field
):
    is_error, payload = await mcp_call(mcp_server.url, mcp_server.token, tool, args)
    assert is_error
    assert payload["code"] == "INVALID_ORDER"
    assert payload["details"] == {"field": field}
    assert payload["message"].startswith(f"{field}: ")
    rows = await _audit_rows(mcp_server.db_url, tool)
    assert [(r.outcome, r.agent_id, r.params) for r in rows] == [
        ("INVALID_ORDER", mcp_server.agent_id, args)
    ]


async def test_two_agents_interleaved_keep_their_identity(mcp_server, mcp_call):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    from smtrading.portfolios.service import create_portfolio

    engine = make_engine(mcp_server.db_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            p2 = await create_portfolio(s, name="Second", initial_cash=Decimal(10))
            a2, token2 = await agents.create_agent(s, "second-agent")
            await agents.assign_portfolio(s, a2.id, p2.id, "read")
            await s.commit()
    finally:
        await engine.dispose()

    who = [(mcp_server.token, "Demo"), (token2, "Second")] * 5
    results = await asyncio.gather(
        *(mcp_call(mcp_server.url, tok, "list_portfolios") for tok, _ in who)
    )
    for (_, expected), (is_error, payload) in zip(who, results, strict=True):
        assert not is_error
        assert [p["name"] for p in payload] == [expected]
    rows = await _audit_rows(mcp_server.db_url, "list_portfolios")
    by_agent = {}
    for r in rows:
        by_agent[r.agent_id] = by_agent.get(r.agent_id, 0) + 1
    assert by_agent == {mcp_server.agent_id: 5, a2.id: 5}


async def test_value_error_is_generic_invalid_argument(mcp_server, mcp_call, caplog):
    async def _bad(ctx):
        raise ValueError("internal detail 42")

    @mcp_server.mcp.tool()
    async def test_value_error():
        return await run_tool("test_value_error", {}, _bad)

    with caplog.at_level("WARNING", logger="smtrading.mcp"):
        is_error, payload = await mcp_call(mcp_server.url, mcp_server.token, "test_value_error")
    assert is_error
    assert payload == {"code": "INVALID_ORDER", "message": "invalid argument", "details": {}}
    assert "internal detail 42" in caplog.text


def test_tool_text_rejects_nan():
    from smtrading.mcp.runtime import _text

    with pytest.raises(ValueError):
        _text({"x": float("nan")})
