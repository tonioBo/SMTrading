import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents import service as agents
from smtrading.agents.models import McpAuditLog
from smtrading.common.db import make_engine
from smtrading.corporate.snapshots import local_date
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.events import discard_post_commit
from smtrading.fees.models import FeeRule, FeeScope, FeeSide
from smtrading.ledger.models import Account, AccountKind, JournalLine
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio, deposit, withdraw
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed

D = Decimal
PORTFOLIO_TOOLS = (
    "get_portfolio", "get_positions", "get_transactions", "get_performance", "get_risk_limits",
)


class _Db:
    def __init__(self, url: str) -> None:
        self.engine = make_engine(url)

    async def __aenter__(self) -> AsyncSession:
        self.conn = await self.engine.connect()
        self.session = AsyncSession(bind=self.conn, expire_on_commit=False)
        return self.session

    async def __aexit__(self, *exc) -> None:
        await self.session.close()
        await self.conn.close()
        await self.engine.dispose()


async def _fill(s, qs, portfolio_id, symbol, side, qty):
    o = Order(
        portfolio_id=portfolio_id, symbol=symbol, side=side, type=OrderType.market,
        qty=D(qty), status=OrderStatus.new,
    )
    s.add(o)
    await s.flush()
    quote = (await qs.get_quotes([symbol]))[symbol]
    assert await execute_fill(s, qs, o.id, quote=quote) is not None


@pytest.fixture
async def book(mcp_server):
    """Demo: long 100 ENI.MI bought at 15 (now 16); Other: unassigned, 5000 EUR."""
    qs = mcp_server.qs
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote("ENI.MI", D(15), D(15), D(15))
    async with _Db(mcp_server.db_url) as s:
        await run_seed(s)
        await _fill(s, qs, mcp_server.demo_id, "ENI.MI", OrderSide.buy, 100)
        other = await create_portfolio(s, name="Other", initial_cash=D(5000))
        await s.commit()
        discard_post_commit(s)
    qs.set_quote("ENI.MI", D(16), D(16), D(16))
    return {"other_id": other.id}


async def _add_day_open(db_url, portfolio_id, equity):
    async with _Db(db_url) as s:
        p = await s.get(Portfolio, portfolio_id)
        now = datetime.now(UTC)
        s.add(EquitySnapshot(
            portfolio_id=portfolio_id, ts=now, kind=SnapshotKind.day_open,
            local_date=local_date(p, now), equity_eur=D(equity), cash_eur=D(equity),
            exposure_eur=D(0), margin_used_eur=D(0),
        ))
        await s.commit()


async def test_get_portfolio_exact_values(mcp_server, mcp_call, book):
    await _add_day_open(mcp_server.db_url, mcp_server.demo_id, "100000")
    is_error, p = await mcp_call(
        mcp_server.url, mcp_server.token, "get_portfolio",
        {"portfolio_id": str(mcp_server.demo_id)},
    )
    assert not is_error, p
    # fee 1 + 0.1% x 1500 = 2.50 EUR
    assert p["portfolio_id"] == str(mcp_server.demo_id)
    assert p["base_currency"] == "EUR"
    assert D(p["cash"]["EUR"]) == D("98497.50")
    assert p["cash_eur"] == "98497.5"
    assert p["equity_eur"] == "100097.5"
    assert p["exposure_eur"] == "1600"
    assert p["initial_req_eur"] == "1600"
    assert p["maint_req_eur"] == "800"
    assert p["warn_req_eur"] == "1200"
    assert p["buying_power_eur"] == "98497.5"
    assert p["margin_level"] == "ok"
    assert p["stale_symbols"] == []
    assert p["status"] == "active"
    assert "mode" not in p  # mcp_hide_mode defaults to on
    assert p["reduce_only_reason"] is None
    assert D(p["leverage"]) == D(1)
    assert p["day_pnl_eur"] == "97.5"
    assert p["day_pnl_pct"] == "0.0975"
    [pos] = p["positions"]
    assert D(pos["qty"]) == D(100) and D(pos["mark"]) == D(16)
    assert D(pos["unrealized_pnl_eur"]) == D(100)


async def test_day_pnl_excludes_cash_flows(mcp_server, mcp_call, book):
    await _add_day_open(mcp_server.db_url, mcp_server.demo_id, "100000")
    async with _Db(mcp_server.db_url) as s:
        await deposit(s, mcp_server.demo_id, D(1000), "EUR", author_id="test")
        await withdraw(s, mcp_server.demo_id, D(200), "EUR", author_id="test")
        await s.commit()
        discard_post_commit(s)
    _, p = await mcp_call(
        mcp_server.url, mcp_server.token, "get_portfolio",
        {"portfolio_id": str(mcp_server.demo_id)},
    )
    assert p["equity_eur"] == "100897.5"  # +800 net deposited
    assert p["day_pnl_eur"] == "97.5"  # only the trading result
    assert p["day_pnl_pct"] == "0.0975"


async def test_get_portfolio_without_day_open(mcp_server, mcp_call, book):
    _, p = await mcp_call(
        mcp_server.url, mcp_server.token, "get_portfolio",
        {"portfolio_id": str(mcp_server.demo_id)},
    )
    assert p["day_pnl_eur"] is None and p["day_pnl_pct"] is None


async def test_get_positions_long_and_short(mcp_server, mcp_call, book):
    qs = mcp_server.qs
    qs.set_quote("AAPL", D(200), D(200), D(200))
    async with _Db(mcp_server.db_url) as s:
        await _fill(s, qs, mcp_server.demo_id, "AAPL", OrderSide.sell, 10)
        await s.commit()
        discard_post_commit(s)
    is_error, positions = await mcp_call(
        mcp_server.url, mcp_server.token, "get_positions",
        {"portfolio_id": str(mcp_server.demo_id)},
    )
    assert not is_error, positions
    by_symbol = {p["symbol"]: p for p in positions}
    assert by_symbol["ENI.MI"]["side"] == "long"
    assert by_symbol["AAPL"]["side"] == "short"
    assert D(by_symbol["AAPL"]["qty"]) == D(-10)
    assert by_symbol["AAPL"]["currency"] == "USD"
    assert D(by_symbol["AAPL"]["market_value"]) == D(-2000)
    assert D(by_symbol["AAPL"]["market_value_eur"]) == D(-1600)


async def test_transactions_only_portfolio_lines(mcp_server, mcp_call, book):
    is_error, txs = await mcp_call(
        mcp_server.url, mcp_server.token, "get_transactions",
        {"portfolio_id": str(mcp_server.demo_id)},
    )
    assert not is_error, txs
    assert [t["type"] for t in txs] == ["TRADE", "DEPOSIT"]
    assert txs[0]["seq"] > txs[1]["seq"]
    assert set(txs[0]) == {"id", "seq", "type", "created_at", "reason", "order_id", "lines"}
    # system accounts (equity, fees) are not portfolio lines
    deposit_lines = txs[1]["lines"]
    assert [(ln["asset"], ln["kind"], D(ln["quantity"])) for ln in deposit_lines] == [
        ("EUR", "cash", D(100000))
    ]
    trade = {(ln["asset"], ln["kind"]): ln for ln in txs[0]["lines"]}
    assert D(trade[("ENI.MI", "position")]["quantity"]) == D(100)
    assert D(trade[("ENI.MI", "position")]["unit_price"]) == D(15)
    assert all(k in ("cash", "position") for _, k in trade)
    # trade + 2.50 fee: the counter-lines on system accounts are not returned
    assert sorted((ln["asset"], ln["kind"], D(ln["quantity"])) for ln in txs[0]["lines"]) == [
        ("ENI.MI", "position", D(100)), ("EUR", "cash", D("-1500")), ("EUR", "cash", D("-2.50")),
    ]
    async with _Db(mcp_server.db_url) as s:
        kinds = set(await s.scalars(
            select(Account.kind).join(JournalLine, JournalLine.account_id == Account.id)
            .where(JournalLine.journal_id == UUID(txs[0]["id"]))
        ))
    assert {AccountKind.system_fees, AccountKind.cash, AccountKind.position} <= kinds
    assert kinds - {AccountKind.cash, AccountKind.position}  # system lines exist in the DB

    # filters and cursor
    _, deposits = await mcp_call(
        mcp_server.url, mcp_server.token, "get_transactions",
        {"portfolio_id": str(mcp_server.demo_id), "type": "DEPOSIT"},
    )
    assert [t["type"] for t in deposits] == ["DEPOSIT"]
    _, older = await mcp_call(
        mcp_server.url, mcp_server.token, "get_transactions",
        {"portfolio_id": str(mcp_server.demo_id), "before_seq": txs[0]["seq"], "limit": 5},
    )
    assert [t["id"] for t in older] == [txs[1]["id"]]
    _, one = await mcp_call(
        mcp_server.url, mcp_server.token, "get_transactions",
        {"portfolio_id": str(mcp_server.demo_id), "limit": 1},
    )
    assert [t["id"] for t in one] == [txs[0]["id"]]
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    _, none = await mcp_call(
        mcp_server.url, mcp_server.token, "get_transactions",
        {"portfolio_id": str(mcp_server.demo_id), "from_ts": future},
    )
    assert none == []
    _, upto = await mcp_call(
        mcp_server.url, mcp_server.token, "get_transactions",
        {"portfolio_id": str(mcp_server.demo_id), "to_ts": future, "type": ["TRADE"]},
    )
    assert [t["type"] for t in upto] == ["TRADE"]


@pytest.mark.parametrize("args", [
    {"type": "NOPE"}, {"limit": 0}, {"limit": 501}, {"from_ts": "yesterday"},
])
async def test_transactions_invalid_arguments(mcp_server, mcp_call, args):
    is_error, err = await mcp_call(
        mcp_server.url, mcp_server.token, "get_transactions",
        {"portfolio_id": str(mcp_server.demo_id), **args},
    )
    assert is_error and err["code"] == "INVALID_ORDER"


async def test_get_performance(mcp_server, mcp_call, book):
    now = datetime.now(UTC)
    async with _Db(mcp_server.db_url) as s:
        for ts, eq in ((now - timedelta(days=2), "100"), (now - timedelta(days=1), "95"),
                       (now - timedelta(hours=1), "110")):
            s.add(EquitySnapshot(
                portfolio_id=mcp_server.demo_id, ts=ts, kind=SnapshotKind.periodic,
                equity_eur=D(eq), cash_eur=D(eq), exposure_eur=D(0), margin_used_eur=D(0),
            ))
        await s.commit()
    is_error, perf = await mcp_call(
        mcp_server.url, mcp_server.token, "get_performance",
        {"portfolio_id": str(mcp_server.demo_id)},  # default 1m
    )
    assert not is_error, perf
    assert [p["equity_eur"] for p in perf["series"]] == ["100", "95", "110"]
    assert perf["return_pct"] == "10"
    assert perf["max_drawdown_pct"] == "-5"
    assert perf["sharpe"] is None
    is_error, err = await mcp_call(
        mcp_server.url, mcp_server.token, "get_performance",
        {"portfolio_id": str(mcp_server.demo_id), "period": "5y"},
    )
    assert is_error and err["code"] == "INVALID_ORDER"


async def test_get_risk_limits(mcp_server, mcp_call, book):
    await _add_day_open(mcp_server.db_url, mcp_server.demo_id, "100200")
    async with _Db(mcp_server.db_url) as s:
        await deposit(s, mcp_server.demo_id, D(50), "EUR", author_id="test")  # a cash flow, not a gain
        await set_kill_switch(s, True)
        await s.commit()
        discard_post_commit(s)
    is_error, r = await mcp_call(
        mcp_server.url, mcp_server.token, "get_risk_limits",
        {"portfolio_id": str(mcp_server.demo_id)},
    )
    assert not is_error, r
    assert r["status"] == "active"
    assert D(r["leverage"]) == D(1)
    assert D(r["maintenance_margin_pct"]) == D(50)
    assert D(r["warning_margin_pct"]) == D(75)
    assert r["profile"]["name"] == "Default"
    assert D(r["profile"]["max_daily_loss_pct"]) == D(10)
    assert r["profile"]["short_allowed"] is True
    assert r["profile"]["description"].startswith("Libero")
    assert r["margin"] == {
        "equity_eur": "100147.5", "maint_req_eur": "800", "warn_req_eur": "1200",
        "buying_power_eur": "98547.5", "margin_level": "ok",
    }
    assert r["distance_to_maintenance_eur"] == "99347.5"
    # loss = 100200 - 100147.50 + 50 deposited
    assert r["daily_loss"] == {
        "day_open_equity_eur": "100200", "current_eur": "102.5", "limit_eur": "10020",
    }
    assert r["kill_switch"] is True


@pytest.mark.parametrize("tool", PORTFOLIO_TOOLS)
async def test_unassigned_portfolio_is_forbidden(mcp_server, mcp_call, book, tool):
    is_error, err = await mcp_call(
        mcp_server.url, mcp_server.token, tool, {"portfolio_id": str(book["other_id"])}
    )
    assert is_error
    assert err["code"] == "FORBIDDEN"


async def test_read_permission_is_enough(mcp_server, mcp_call, book):
    async with _Db(mcp_server.db_url) as s:
        await agents.assign_portfolio(s, mcp_server.agent_id, book["other_id"], "read")
        await s.commit()
    for tool in PORTFOLIO_TOOLS:
        is_error, payload = await mcp_call(
            mcp_server.url, mcp_server.token, tool, {"portfolio_id": str(book["other_id"])}
        )
        assert not is_error, (tool, payload)


# --- resources ------------------------------------------------------------------------


async def _read_resource(url: str, token: str, uri: str):
    """The parsed JSON content; a read error is returned as the MCPError (raising it
    inside the client task group would wrap it in an ExceptionGroup)."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.shared.exceptions import MCPError

    async with (
        create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as cs,
    ):
        await cs.initialize()
        try:
            result = await cs.read_resource(uri)
        except MCPError as e:
            return e
    [content] = result.contents
    return json.loads(content.text)


async def _audit_methods(db_url: str) -> list[tuple[str, str]]:
    async with _Db(db_url) as s:
        rows = await s.execute(
            select(McpAuditLog.method, McpAuditLog.outcome).order_by(McpAuditLog.id)
        )
        return [tuple(r) for r in rows.all()]


async def test_portfolio_resource_matches_get_portfolio(mcp_server, mcp_call, book):
    uri = f"portfolio://{mcp_server.demo_id}"
    res = await _read_resource(mcp_server.url, mcp_server.token, uri)
    _, tool = await mcp_call(
        mcp_server.url, mcp_server.token, "get_portfolio",
        {"portfolio_id": str(mcp_server.demo_id)},
    )
    res.pop("ts"), tool.pop("ts")
    assert res == tool
    assert (f"resource:{uri}", "ok") in await _audit_methods(mcp_server.db_url)


async def test_portfolio_resource_forbidden(mcp_server, book):
    from mcp.shared.exceptions import MCPError

    uri = f"portfolio://{book['other_id']}"
    err = await _read_resource(mcp_server.url, mcp_server.token, uri)
    assert isinstance(err, MCPError)
    assert json.loads(err.message)["code"] == "FORBIDDEN"
    assert (f"resource:{uri}", "FORBIDDEN") in await _audit_methods(mcp_server.db_url)


async def test_fees_schedule_resource(mcp_server, book):
    async with _Db(mcp_server.db_url) as s:
        s.add(FeeRule(
            name="crypto", scope=FeeScope.asset_class, scope_ref="crypto", side=FeeSide.buy,
            currency="EUR", fixed=D(0), pct=D("0.5"),
        ))
        s.add(FeeRule(  # another portfolio's rule: never shown
            name="other", scope=FeeScope.portfolio, scope_ref=str(book["other_id"]),
            side=FeeSide.both, currency="EUR", fixed=D(9), pct=D(0),
        ))
        await s.commit()
    res = await _read_resource(mcp_server.url, mcp_server.token, "fees://schedule")
    [entry] = res["portfolios"]
    assert entry["portfolio_id"] == str(mcp_server.demo_id)
    rules = entry["rules"]
    assert set(rules) == {"us_equity", "eu_equity", "crypto", "forex"}
    default = rules["us_equity"]["buy"]
    assert D(default["fixed"]) == D(1) and D(default["pct"]) == D("0.1")
    assert default["scope"] == "global" and default["currency"] == "EUR"
    assert D(rules["crypto"]["buy"]["pct"]) == D("0.5")
    assert rules["crypto"]["buy"]["scope"] == "asset_class"
    assert rules["crypto"]["sell"]["id"] == default["id"]
    assert ("resource:fees://schedule", "ok") in await _audit_methods(mcp_server.db_url)


async def test_markets_hours_resource(mcp_server):
    res = await _read_resource(mcp_server.url, mcp_server.token, "markets://hours")
    by_market = {m["market"]: m for m in res}
    assert set(by_market) == {"NASDAQ", "NYSE", "MTA", "XETRA", "EURONEXT_PARIS", "CRYPTO", "FX"}
    assert by_market["CRYPTO"] == {
        "market": "CRYPTO", "is_open": True, "next_open": None, "next_close": None,
    }
    assert ("resource:markets://hours", "ok") in await _audit_methods(mcp_server.db_url)


async def test_archived_portfolio_is_invisible_to_agents(mcp_server, mcp_call, book):
    from smtrading.admin.portfolio_ops import archive_portfolio
    from smtrading.engine.service import cancel_all_orders

    async with _Db(mcp_server.db_url) as s:
        await agents.assign_portfolio(s, mcp_server.agent_id, book["other_id"], "trade")
        await cancel_all_orders(s, mcp_server.demo_id, by="admin")
        await archive_portfolio(s, mcp_server.demo_id, by="admin")
        await s.commit()
        discard_post_commit(s)
    url, token = mcp_server.url, mcp_server.token
    is_error, listed = await mcp_call(url, token, "list_portfolios")
    assert not is_error and [p["name"] for p in listed] == ["Other"]
    assert all("mode" not in p for p in listed)  # mcp_hide_mode defaults to on
    demo = {"portfolio_id": str(mcp_server.demo_id)}
    for tool, args in (
        ("get_portfolio", demo), ("get_positions", demo),
        ("place_order", {**demo, "symbol": "ENI.MI", "side": "buy", "type": "market",
                         "qty": "1"}),
    ):
        is_error, payload = await mcp_call(url, token, tool, args)
        assert is_error and payload["code"] == "NOT_FOUND", (tool, payload)
    async with _Db(mcp_server.db_url) as s:  # the assignment is kept
        assert await agents.get_permission(s, mcp_server.agent_id, mcp_server.demo_id) == "trade"
        assert await s.scalar(select(func.count()).select_from(Order).where(
            Order.portfolio_id == mcp_server.demo_id, Order.status == OrderStatus.rejected
        )) == 0


async def _set_hide_mode(db_url, on):
    from sqlalchemy.dialects.postgresql import insert

    from smtrading.risk.models import AppSetting

    async with _Db(db_url) as s:
        await s.execute(insert(AppSetting).values(key="mcp_hide_mode", value={"on": on})
                        .on_conflict_do_update(index_elements=["key"], set_={"value": {"on": on}}))
        await s.commit()


async def test_mcp_hide_mode_setting(mcp_server, mcp_call, book):
    """On (default): no agent-visible payload mentions simulazione/paper/demo; off: mode shows."""
    import json

    url, token = mcp_server.url, mcp_server.token
    async with _Db(mcp_server.db_url) as s:  # the seeded name "Demo" would be a false positive
        (await s.get(Portfolio, mcp_server.demo_id)).name = "Principale"
        await s.commit()
    args = {"portfolio_id": str(mcp_server.demo_id)}
    calls = (("list_portfolios", None), ("get_portfolio", args), ("get_positions", args),
             ("get_risk_limits", args), ("get_transactions", args))
    for tool, a in calls:
        is_error, out = await mcp_call(url, token, tool, a)
        assert not is_error, out
        low = json.dumps(out).lower()
        assert not any(w in low for w in ("simulaz", "paper", "demo")), (tool, out)

    await _set_hide_mode(mcp_server.db_url, False)
    _, listed = await mcp_call(url, token, "list_portfolios")
    assert {p["mode"] for p in listed} == {"simulazione"}
    _, got = await mcp_call(url, token, "get_portfolio", args)
    assert got["mode"] == "simulazione"


async def _live_link_for_demo(db_url, demo_id, state):
    """A real-money link row (canary keys, never used: no network) linked to Demo."""
    from smtrading.brokers.live.account import encrypt_live_config
    from smtrading.brokers.models import BrokerLink

    async with _Db(db_url) as s:
        link = BrokerLink(
            name="Conto", kind="t212_live", live_state=state, health_failures=0,
            config_encrypted=encrypt_live_config(
                {"api_key": "CANARYKEYID0001", "api_secret": "CANARYSECRET-do-not-use"}),
        )
        s.add(link)
        await s.flush()
        demo = await s.get(Portfolio, demo_id)
        demo.broker_link_id = link.id
        demo.name = "Principale"  # the seeded name "Demo" would be a false positive
        await s.commit()
        return link.id


async def _set_live_state(db_url, link_id, state):
    from smtrading.brokers.models import BrokerLink

    async with _Db(db_url) as s:
        (await s.get(BrokerLink, link_id)).live_state = state
        await s.commit()


async def test_portfolio_dto_mode_reale_and_live_state(mcp_server, mcp_call, book):
    """Agents see `reale` (and live_state) for an armed real-money portfolio even with
    mcp_hide_mode on; a link not armed sends nothing: hidden like any other mode."""
    url, token, demo = mcp_server.url, mcp_server.token, mcp_server.demo_id
    args = {"portfolio_id": str(demo)}
    link_id = await _live_link_for_demo(mcp_server.db_url, demo, "armed")
    _, listed = await mcp_call(url, token, "list_portfolios")
    assert [(p["mode"], p["live_state"]) for p in listed] == [("reale", "armed")]
    _, got = await mcp_call(url, token, "get_portfolio", args)
    assert got["mode"] == "reale" and got["live_state"] == "armed"

    for state in ("dry_run", "configured"):
        await _set_live_state(mcp_server.db_url, link_id, state)
        _, listed = await mcp_call(url, token, "list_portfolios")
        assert all("mode" not in p and "live_state" not in p for p in listed), listed
        _, got = await mcp_call(url, token, "get_portfolio", args)
        assert "mode" not in got and "live_state" not in got
        low = json.dumps([listed, got]).lower()
        assert not any(w in low for w in ("simulaz", "paper", "demo", "dry_run")), low

    await _set_hide_mode(mcp_server.db_url, False)
    _, got = await mcp_call(url, token, "get_portfolio", args)
    assert got["mode"] == "paper" and got["live_state"] == "configured"


async def test_mcp_order_on_suspended_real_portfolio_live_not_armed(mcp_server, mcp_call, book):
    await _live_link_for_demo(mcp_server.db_url, mcp_server.demo_id, "configured")
    is_error, out = await mcp_call(mcp_server.url, mcp_server.token, "place_order", {
        "portfolio_id": str(mcp_server.demo_id), "symbol": "ENI.MI", "side": "buy",
        "type": "market", "qty": "1"})
    assert is_error and out["code"] == "LIVE_NOT_ARMED", out
    assert "CANARY" not in json.dumps(out)


async def test_dry_run_rejection_is_agent_safe(mcp_server, mcp_call, book, monkeypatch):
    """R8.11: with mcp_hide_mode on, an order rejected by the dry run reaches the agent as
    a generic broker rejection: no LIVE_DRY_RUN, no «prova a secco», no payload; the
    stored order (portal, Telegram) keeps the real code."""
    from smtrading.brokers.live.protect import record_live_order_activity
    from smtrading.brokers.live.t212_live import T212LiveAdapter
    from smtrading.brokers.sync import DRY_RUN_MESSAGE
    from smtrading.engine.models import BrokerState
    from smtrading.engine.service import reject_broker_order
    from smtrading.mcp.dto import AGENT_REJECT_CODE, AGENT_REJECT_MESSAGE

    url, token, demo = mcp_server.url, mcp_server.token, mcp_server.demo_id
    link_id = await _live_link_for_demo(mcp_server.db_url, demo, "dry_run")
    async with _Db(mcp_server.db_url) as s:
        o = Order(portfolio_id=demo, agent_id=str(mcp_server.agent_id), symbol="ENI.MI",
                  side=OrderSide.sell, type=OrderType.market, qty=D(1),
                  status=OrderStatus.pending, broker_link_id=link_id,
                  broker_status=BrokerState.TO_SEND)
        s.add(o)
        await s.flush()
        await reject_broker_order(s, o, "LIVE_DRY_RUN", DRY_RUN_MESSAGE)
        await record_live_order_activity(s, o, "dry_run", message=DRY_RUN_MESSAGE,
                                         payload={"symbol": "ENI.MI", "qty": "1"})
        await s.commit()
        discard_post_commit(s)
        order_id = str(o.id)

    calls = (("get_order", {"order_id": order_id}),
             ("list_orders", {"portfolio_id": str(demo)}),
             ("get_activity_feed", None),
             ("get_portfolio", {"portfolio_id": str(demo)}),
             ("list_portfolios", None))
    payloads = []
    for tool, args in calls:
        is_error, out = await mcp_call(url, token, tool, args)
        assert not is_error, out
        payloads.append(out)
        low = json.dumps(out, ensure_ascii=False).lower()
        assert not any(w in low for w in ("prova", "dry", "secco")), (tool, low)
    got = payloads[0]
    assert got["status"] == "rejected" and got["reject_code"] == AGENT_REJECT_CODE
    assert got["reject_message"] == AGENT_REJECT_MESSAGE
    feed = payloads[2]["items"]
    assert any(i["type"] == "live.order.rejected" for i in feed), feed
    # an MCP order on the dry-run portfolio is accepted (it is rejected later, at the
    # send). Routability of ENI.MI on Trading 212 depends on the process-wide instrument
    # cache other tests may have filled (the fake lists US tickers only): pinned here.
    monkeypatch.setattr(T212LiveAdapter, "routable", lambda self, order, instrument: True)
    is_error, placed = await mcp_call(url, token, "place_order", {
        "portfolio_id": str(demo), "symbol": "ENI.MI", "side": "sell", "type": "market",
        "qty": "1"})
    assert not is_error, placed
    assert placed["status"] in ("pending", "queued"), placed
    low = json.dumps(placed, ensure_ascii=False).lower()
    assert not any(w in low for w in ("prova", "dry", "secco")), low

    async with _Db(mcp_server.db_url) as s:  # humans keep the real code
        stored = await s.get(Order, UUID(order_id))
        assert stored.reject_code == "LIVE_DRY_RUN"
    await _set_hide_mode(mcp_server.db_url, False)
    _, got = await mcp_call(url, token, "get_order", {"order_id": order_id})
    assert got["reject_code"] == "LIVE_DRY_RUN"
