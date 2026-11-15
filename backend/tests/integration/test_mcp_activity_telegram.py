"""MCP decisions log, activity feed and send_telegram on the live fixture server."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Activity
from smtrading.agents import service as agents
from smtrading.agents.models import Agent, McpAuditLog
from smtrading.common.db import make_engine
from smtrading.portfolios.service import create_portfolio
from smtrading.telegram import config as tg_config
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.models import TelegramOutbox


@pytest.fixture(autouse=True)
def _clear_warned():
    tg_config._warned.clear()
    yield
    tg_config._warned.clear()


@pytest.fixture
async def env(mcp_server, mcp_call):
    engine = make_engine(mcp_server.db_url)

    async def call(tool, args=None, token=None):
        return await mcp_call(mcp_server.url, token or mcp_server.token, tool, args or {})

    async def db():
        return AsyncSession(engine, expire_on_commit=False)

    yield {"call": call, "engine": engine, "server": mcp_server, "db": db}
    await engine.dispose()


async def _set_notify(env, on: bool = True):
    async with await env["db"]() as s:
        await s.execute(update(Agent).where(Agent.id == env["server"].agent_id)
                        .values(can_notify=on))
        await s.commit()


async def _configure(env, *, enabled=True, paused=False):
    async with await env["db"]() as s:
        await save_config(s, TelegramConfig(bot_token="123456:ABCdef", chat_id="-1001",
                                            enabled=enabled, paused=paused))
        await s.commit()


async def _outbox(env) -> list[TelegramOutbox]:
    async with await env["db"]() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


# --- decisions ------------------------------------------------------------------------


async def test_log_decision_and_get_decisions(env):
    call, demo = env["call"], str(env["server"].demo_id)
    err, d1 = await call("log_decision", {"kind": "strategy", "summary": "Riduco crypto",
                                          "portfolio_id": demo, "symbols": ["btc-usd"],
                                          "details": {"weight": "0.1"}})
    assert not err, d1
    assert isinstance(d1["decision_id"], int) and d1["created_at"]
    err, d2 = await call("log_decision", {"kind": "veto", "summary": "No leva"})
    assert not err, d2

    err, res = await call("get_decisions")
    assert not err, res
    assert [d["id"] for d in res["decisions"]] == [d2["decision_id"], d1["decision_id"]]
    newest, oldest = res["decisions"]
    assert newest["portfolio_id"] is None and newest["portfolio"] is None
    assert oldest == {
        "id": d1["decision_id"], "agent": "test-agent", "agent_id": str(env["server"].agent_id),
        "portfolio": "Demo", "portfolio_id": demo, "kind": "strategy",
        "summary": "Riduco crypto", "symbols": ["BTC-USD"], "details": {"weight": "0.1"},
        "outcome": None, "reason": None, "subject": None, "created_at": d1["created_at"],
    }

    err, res = await call("get_decisions", {"kind": "veto"})
    assert not err and [d["id"] for d in res["decisions"]] == [d2["decision_id"]]
    err, res = await call("get_decisions", {"portfolio_id": demo})
    assert not err and [d["id"] for d in res["decisions"]] == [d1["decision_id"]]
    err, res = await call("get_decisions", {"agent": "test-agent", "limit": 1})
    assert not err and [d["id"] for d in res["decisions"]] == [d2["decision_id"]]
    err, res = await call("get_decisions", {"agent": "nobody"})
    assert not err and res["decisions"] == []

    err, res = await call("get_decisions", {"kind": "maybe"})
    assert err and res["code"] == "INVALID_ORDER" and res["details"]["field"] == "kind"
    err, res = await call("get_decisions", {"limit": 201})
    assert err and res["details"]["field"] == "limit"
    err, res = await call("log_decision", {"kind": "veto", "summary": "   "})
    assert err and res["details"]["field"] == "summary"


async def test_mcp_get_decisions_exposes_fields(env):
    call = env["call"]
    err, plain = await call("log_decision", {"kind": "veto", "summary": "No leva"})
    assert not err, plain
    err, d = await call("log_decision", {"kind": "approval", "summary": "Ok a AAPL",
                                         "outcome": "approvata", "subject": "Acquisto AAPL",
                                         "reason": " rischio basso "})
    assert not err, d
    err, res = await call("get_decisions")
    assert not err, res
    newest, older = res["decisions"][:2]
    assert newest["id"] == d["decision_id"]
    assert (newest["outcome"], newest["subject"], newest["reason"]) == (
        "approvata", "Acquisto AAPL", "rischio basso")
    assert older["id"] == plain["decision_id"]
    assert (older["outcome"], older["subject"], older["reason"]) == (None, None, None)
    err, res = await call("log_decision", {"kind": "veto", "summary": "x", "outcome": "ok"})
    assert err and res["code"] == "INVALID_ORDER" and res["details"]["field"] == "outcome"
    err, res = await call("log_decision", {"kind": "veto", "summary": "x",
                                           "subject": "s" * 201})
    assert err and res["details"]["field"] == "subject"


async def test_get_decisions_forbidden_portfolio(env):
    async with await env["db"]() as s:
        other = await create_portfolio(s, name="Other", initial_cash=Decimal(1000))
        await s.commit()
    err, res = await env["call"]("get_decisions", {"portfolio_id": str(other.id)})
    assert err and res["code"] == "FORBIDDEN"
    err, res = await env["call"]("log_decision", {"kind": "other", "summary": "x",
                                                  "portfolio_id": str(other.id)})
    assert err and res["code"] == "FORBIDDEN"


# --- activity feed --------------------------------------------------------------------


async def _add_activities(env, n: int, **kw) -> list[int]:
    ts = datetime.now(UTC) - timedelta(seconds=30)
    async with await env["db"]() as s:
        rows = [Activity(ts=ts + timedelta(milliseconds=i), type=kw.get("type", "test.event"),
                         level="info", portfolio_id=env["server"].demo_id,
                         summary=f"evento {i}", data={"i": i},
                         telegram=kw.get("telegram"))
                for i in range(n)]
        s.add_all(rows)
        await s.commit()
        return [r.id for r in rows]


async def test_feed_via_mcp_pages_like_alerts(env):
    call = env["call"]
    ids = await _add_activities(env, 5)
    err, first = await call("get_activity_feed", {"limit": 2})
    assert not err, first
    assert [i["id"] for i in first["items"]] == [ids[4], ids[3]]  # newest first
    assert first["next_after_id"] == ids[4]
    item = first["items"][0]
    assert item["portfolio"] == "Demo" and item["portfolio_id"] == str(env["server"].demo_id)
    assert item["summary"] == "evento 4" and item["data"] == {"i": 4}
    assert item["already_sent"] is False and isinstance(item["ts"], str)

    # page from the start (after_id=0) with limit 2 until the cursor is stable
    seen, cursor = [], 0
    for _ in range(10):
        err, page = await call("get_activity_feed", {"after_id": cursor, "limit": "2"})
        assert not err, page
        seen += [i["id"] for i in page["items"]]
        if page["next_after_id"] == cursor:
            assert page["items"] == []
            break
        cursor = page["next_after_id"]
    assert seen == ids and cursor == ids[-1]

    err, res = await call("get_activity_feed", {"after_id": ids[-1] + 1000})
    assert err and res["code"] == "INVALID_ORDER" and res["details"]["field"] == "after_id"
    err, res = await call("get_activity_feed", {"limit": 0})
    assert err and res["details"]["field"] == "limit"


async def test_feed_already_sent_for_fill(env):
    [sent] = await _add_activities(env, 1, type="order.filled", telegram="queued")
    [other] = await _add_activities(env, 1, type="order.filled", telegram="skipped")
    # no planner run yet (e.g. the worker is down): SMTrading will still send it
    [unplanned] = await _add_activities(env, 1, type="order.filled", telegram="pending")
    err, res = await env["call"]("get_activity_feed", {"after_id": sent - 1})
    assert not err, res
    by_id = {i["id"]: i for i in res["items"]}
    assert by_id[sent]["already_sent"] is True
    assert by_id[other]["already_sent"] is False
    assert by_id[unplanned]["already_sent"] is True


async def test_feed_hides_archived_portfolio_keeps_cursor(env):
    """Archiving a portfolio hides its feed items and decisions from the agent; a cursor on
    one of its rows stays valid."""
    call, demo = env["call"], env["server"].demo_id
    err, decided = await call("log_decision", {"kind": "strategy", "summary": "Su Demo",
                                               "portfolio_id": str(demo)})
    assert not err, decided
    [cursor] = await _add_activities(env, 1)  # on Demo
    async with await env["db"]() as s:
        await s.execute(text("update portfolios set status = 'archived' where id = :id"),
                        {"id": demo})
        newer = Activity(ts=datetime.now(UTC) - timedelta(seconds=10), type="kill_switch",
                         level="critical", summary="globale", data={})
        s.add(newer)
        await s.commit()
    err, res = await call("get_activity_feed", {"after_id": cursor})
    assert not err, res
    assert [i["id"] for i in res["items"]] == [newer.id] and res["next_after_id"] == newer.id
    err, res = await call("get_activity_feed", {"after_id": cursor - 1})
    assert not err and cursor not in [i["id"] for i in res["items"]]
    err, res = await call("get_decisions")
    assert not err and decided["decision_id"] not in [d["id"] for d in res["decisions"]]


# --- send_telegram --------------------------------------------------------------------


async def test_send_telegram_requires_notify(env):
    await _configure(env)
    err, res = await env["call"]("send_telegram", {"text": "ciao"})
    assert err and res["code"] == "FORBIDDEN" and "notify" in res["message"]
    assert await _outbox(env) == []
    await _set_notify(env)  # read from the database at call time
    err, res = await env["call"]("send_telegram", {"text": "ciao"})
    assert not err and res["status"] == "pending", res


async def test_send_telegram_verbatim_markup(env):
    await _configure(env)
    await _set_notify(env)
    raw = "<b>Grassetto</b> *_[link](http://x)_* @canale\x07\n\triga"
    err, res = await env["call"]("send_telegram", {"text": raw, "level": "warning"})
    assert not err, res
    [row] = await _outbox(env)
    assert row.id == res["outbox_id"] and res["status"] == "pending"
    assert row.text == "🟠 <b>Grassetto</b> *_[link](http://x)_* @canale\n\triga"
    assert row.source == f"agent:{env['server'].agent_id}" and row.category == "agent"
    assert row.level == "warning" and row.entities is None
    err, res = await env["call"]("send_telegram", {"text": "info"})
    assert not err
    assert (await _outbox(env))[-1].text == "info"  # no prefix for info


async def test_send_telegram_rate_limit_21st(env):
    await _configure(env)
    await _set_notify(env)
    call = env["call"]
    results = await asyncio.gather(*[call("send_telegram", {"text": f"m{i}"}) for i in range(25)])
    ok = [r for err, r in results if not err]
    limited = [r for err, r in results if err]
    assert len(ok) == 20
    assert len(limited) == 5 and all(r["code"] == "RATE_LIMITED" for r in limited)
    assert all(1 <= r["details"]["retry_after_s"] <= 60 for r in limited)
    assert len(await _outbox(env)) == 20
    err, res = await call("send_telegram", {"text": "21st"})
    assert err and res["code"] == "RATE_LIMITED"

    # older than 60 s: no longer counted
    async with await env["db"]() as s:
        await s.execute(update(TelegramOutbox).values(
            created_at=func.now() - text("interval '61 seconds'")))
        await s.commit()
    err, res = await call("send_telegram", {"text": "di nuovo"})
    assert not err, res


async def test_send_telegram_limits(env):
    await _configure(env)
    await _set_notify(env)
    call = env["call"]
    err, res = await call("send_telegram", {"text": "x" * 4097})
    assert err and res["code"] == "INVALID_ORDER" and res["details"]["field"] == "text"
    err, res = await call("send_telegram", {"text": "x" * 4096})
    assert not err, res
    err, res = await call("send_telegram", {"text": "x" * 4095, "level": "critical"})
    assert err and res["details"]["field"] == "text"  # the prefix counts
    err, res = await call("send_telegram", {"text": " \n\t \x00 "})
    assert err and res["code"] == "INVALID_ORDER" and res["details"]["field"] == "text"
    err, res = await call("send_telegram", {"text": "ciao", "level": "loud"})
    assert err and res["code"] == "INVALID_ORDER" and res["details"]["field"] == "level"
    err, res = await call("send_telegram", {"text": "ciao", "level": "executed"})
    assert err and res["details"]["field"] == "level"
    assert len(await _outbox(env)) == 1


async def test_send_telegram_not_configured(env):
    await _set_notify(env)
    err, res = await env["call"]("send_telegram", {"text": "ciao"})
    assert err and res["code"] == "PROVIDER_UNAVAILABLE"
    assert res["message"] == "Notifiche Telegram non configurate o disattivate"
    await _configure(env, enabled=False)
    err, res = await env["call"]("send_telegram", {"text": "ciao"})
    assert err and res["code"] == "PROVIDER_UNAVAILABLE"
    assert await _outbox(env) == []


async def test_send_telegram_paused_status(env):
    await _configure(env, paused=True)
    await _set_notify(env)
    err, res = await env["call"]("send_telegram", {"text": "ciao"})
    assert not err and res["status"] == "paused", res
    [row] = await _outbox(env)
    assert row.status == "paused"


async def test_audit_has_no_text(env):
    await _configure(env)
    await _set_notify(env)
    secret = "testo riservato da non registrare"
    err, res = await env["call"]("send_telegram", {"text": secret, "level": "warning"})
    assert not err, res
    err, res = await env["call"]("send_telegram", {"text": secret, "level": "loud"})
    assert err
    # refused by the argument schema before the tool body runs: same reduced params
    err, res = await env["call"]("send_telegram", {"text": secret, "level": 5})
    assert err and res["code"] == "INVALID_ORDER" and res["details"]["field"] == "level"
    async with await env["db"]() as s:
        rows = list(await s.scalars(select(McpAuditLog).where(McpAuditLog.method == "send_telegram")
                                    .order_by(McpAuditLog.id)))
    assert [r.params for r in rows] == [{"level": "warning", "length": len(secret)},
                                        {"level": "loud", "length": len(secret)},
                                        {"level": 5, "length": len(secret)}]
    assert all(secret not in json.dumps(r.params) for r in rows)


async def test_other_agent_without_notify(env):
    """Each agent's permission is its own: a second agent without notify is refused."""
    await _configure(env)
    await _set_notify(env)
    async with await env["db"]() as s:
        _, token = await agents.create_agent(s, "other-agent")
        await s.commit()
    err, res = await env["call"]("send_telegram", {"text": "ciao"}, token=token)
    assert err and res["code"] == "FORBIDDEN"
