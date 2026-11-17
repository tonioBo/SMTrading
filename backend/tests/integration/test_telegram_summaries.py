"""Scheduled Telegram summaries: night digest, daily and weekly summary, ranking. Nothing is
sent to Telegram here: the summaries only fill the outbox (the sender is tested apart)."""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity, Decision
from smtrading.activity.service import log_decision
from smtrading.agents.service import assign_portfolio, create_agent
from smtrading.common.db import make_engine
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Fill, OrderSide, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import discard_post_commit
from smtrading.goals.models import Goal
from smtrading.jobs.runs import job_done
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.seed import run_seed
from smtrading.telegram import config as tg_config
from smtrading.telegram.client import API_URL
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.models import TelegramOutbox
from smtrading.telegram.planner import TelegramPlanner
from smtrading.telegram.render import MAX_LEN, utf16_len
from smtrading.telegram.sender import OutboxSender
from smtrading.telegram.summaries import (
    RankRow,
    SummaryScheduler,
    build_digest,
    build_summary,
    ranking,
    ranking_table,
)

D = Decimal
TZ = "Europe/Rome"
ROME = ZoneInfo(TZ)
TOKEN = "123456789:AAHsecretSECRETsecret_SECRET-1234567"
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open (order placement)


def rome(day: int, h: int, m: int = 0, s: int = 0) -> datetime:
    """A local instant of March 2027 (CET), in UTC; the 12th is a Friday."""
    return datetime(2027, 3, day, h, m, s, tzinfo=ROME).astimezone(UTC)


FRI, THU = 12, 11


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture(autouse=True)
def _clear_warned():
    tg_config._warned.clear()
    yield
    tg_config._warned.clear()


@pytest.fixture
async def maker(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        # the seeded "Demo" portfolio is archived, so the ranking holds the tests' ones only
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))
        await s.commit()
    yield sm
    await engine.dispose()


async def _configure(maker, **kw) -> None:
    cfg = TelegramConfig(bot_token=TOKEN, chat_id="-100123", enabled=True)
    for k, v in kw.items():
        setattr(cfg, k, v)
    async with maker() as s:
        await save_config(s, cfg)
        await s.commit()


async def _portfolio(maker, name: str, cash=0) -> uuid.UUID:
    async with maker() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(cash))
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _archive(maker, pid) -> None:
    async with maker() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(status=PortfolioStatus.archived))
        await s.commit()


async def _decisions(maker, rows: list[dict], *, at: datetime, agent: str = "CEO") -> None:
    """Decisions logged by `agent`, `at` + 1 minute each in the given order."""
    async with maker() as s:
        a, _ = await create_agent(s, agent)
        for pid in {kw["portfolio_id"] for kw in rows if kw.get("portfolio_id")}:
            await assign_portfolio(s, a.id, pid, "read")
        for i, kw in enumerate(rows):
            d = await log_decision(s, a.id, **kw)
            d.created_at = at + timedelta(minutes=i)
        discard_post_commit(s)
        await s.commit()


async def _snaps(maker, pid, points: list[tuple[datetime, str]]) -> None:
    async with maker() as s:
        for ts, equity in points:
            s.add(EquitySnapshot(portfolio_id=pid, ts=ts, kind=SnapshotKind.periodic,
                                 equity_eur=D(equity), cash_eur=D(equity),
                                 exposure_eur=D(0), margin_used_eur=D(0)))
        await s.commit()


async def _fill(maker, pid, at: datetime) -> None:
    """A real sim fill (order + ledger), moved to `at` (fill and its activity row)."""
    qs = FakeQuoteSource()
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    async with maker() as s:
        order = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
            qty=D(1), limit_price=D("195.00"),
        ), now=OPEN)
        qs.set_quote("AAPL", D("194.00"), D("194.50"), None)
        quote = (await qs.get_quotes(["AAPL"]))["AAPL"]
        await execute_fill(s, qs, order.id, quote=quote)
        await s.flush()
        await s.execute(update(Fill).where(Fill.order_id == order.id).values(created_at=at))
        await s.execute(update(Activity).where(Activity.data["order_id"].astext == str(order.id))
                        .values(ts=at))
        discard_post_commit(s)
        await s.commit()


async def _activity(maker, *, type="order.filled", level="executed", summary="Ordine eseguito",
                    ts: datetime, portfolio_id=None, telegram=None) -> int:
    async with maker() as s:
        a = Activity(type=type, level=level, summary=summary, ts=ts, data={},
                     portfolio_id=portfolio_id, telegram=telegram)
        s.add(a)
        await s.commit()
        return a.id


async def _outbox(maker) -> list[TelegramOutbox]:
    async with maker() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


def _scheduler(maker, clock) -> SummaryScheduler:
    return SummaryScheduler(maker, clock=clock, tz=TZ)


# -- ranking -------------------------------------------------------------------------


async def test_ranking_order_and_none_last(maker):
    now = rome(FRI, 18)
    a = await _portfolio(maker, "Test 250 A")
    b = await _portfolio(maker, "Test 250 B", cash=1000)
    await _portfolio(maker, "Test 250 C")  # no snapshot at all
    d = await _portfolio(maker, "Test 250 D")
    e = await _portfolio(maker, "Test 250 E (archiviato)")
    h = timedelta(hours=1)
    await _snaps(maker, a, [(now - 5 * h, "100"), (now - h, "102")])
    await _snaps(maker, b, [(now - 5 * h, "100"), (now - 3 * h, "101"), (now - h, "99")])
    await _snaps(maker, d, [(now - 5 * h, "100"), (now - h, "100.5")])
    await _snaps(maker, e, [(now - 5 * h, "100"), (now - h, "150")])
    await _snaps(maker, a, [(now - 30 * h, "10")])  # outside 1d: ignored
    since = rome(FRI, 0)
    await _fill(maker, b, now - 2 * h)
    await _fill(maker, b, now - 3 * h)
    await _fill(maker, b, since - h)  # yesterday: not counted
    await _archive(maker, e)

    async with maker() as s:
        rows = await ranking(s, period="1d", since=since, now=now)
    assert [r.portfolio for r in rows] == ["Test 250 A", "Test 250 D", "Test 250 B",
                                           "Test 250 C"]
    assert rows[0] == RankRow("Test 250 A", D("2.0000"), D("0.0000"), 0)
    assert rows[1].return_pct == D("0.5000")
    assert rows[2] == RankRow("Test 250 B", D("-1.0000"), D("-1.9802"), 2)
    assert rows[3] == RankRow("Test 250 C", None, None, 0)


def test_ranking_table_format():
    table = ranking_table([
        RankRow("Test 250 A", D("2.0049"), D("0"), 3),
        RankRow("Un nome molto lungo che va troncato", D("-0.5"), D("-12.345"), 0),
        RankRow("Mille", D("1234.5"), D("-0.004"), 12),
        RankRow("Vuoto", None, None, 0),
    ])
    lines = table.split("\n")
    assert lines[0].split() == ["Portafoglio", "Rend.", "%", "Max", "DD", "%", "Op."]
    assert lines[1].split() == ["Test", "250", "A", "+2,00", "0,00", "3"]
    assert lines[2].startswith("Un nome molto lung ")  # 18 characters
    assert lines[2].split()[-3:] == ["-0,50", "-12,34", "0"]  # half-even
    assert lines[3].split()[-3:] == ["+1.234,50", "0,00", "12"]
    assert lines[4].split()[-3:] == ["—", "—", "0"]
    assert len({len(line) for line in lines}) == 1  # fixed width
    assert "nessun portafoglio" in ranking_table([])


# -- daily / weekly ------------------------------------------------------------------


async def test_daily_summary_at_1800_once(maker):
    await _configure(maker)
    pid = await _portfolio(maker, "Test 250 A", cash=1000)
    await _snaps(maker, pid, [(rome(FRI, 9), "250"), (rome(FRI, 17), "255")])
    await _fill(maker, pid, rome(FRI, 10, 15))
    await _activity(maker, type="order.rejected", level="warning",
                    summary="Ordine rifiutato: TSLA", ts=rome(FRI, 11), portfolio_id=pid)
    await _activity(maker, type="kill_switch", level="critical", summary="Kill switch attivo",
                    ts=rome(FRI, 12))
    await _activity(maker, type="order.filled", summary="Ieri", ts=rome(THU, 20),
                    portfolio_id=pid)  # outside the day
    async with maker() as s:
        agent, _ = await create_agent(s, "Analyst")
        await log_decision(s, agent.id, kind="proposal", summary="Compra AAPL")
        await log_decision(s, agent.id, kind="proposal", summary="Compra MSFT")
        await log_decision(s, agent.id, kind="veto", summary="No")
        await s.execute(update(Decision).values(created_at=rome(FRI, 13)))
        discard_post_commit(s)
        await s.commit()

    clock = Clock(rome(FRI, 17, 59, 59))
    await _scheduler(maker, clock).tick()
    assert await _outbox(maker) == []

    clock.t = rome(FRI, 18)
    scheduler = _scheduler(maker, clock)
    await scheduler.tick()
    [msg] = await _outbox(maker)
    assert (msg.category, msg.level, msg.source, msg.status) == ("daily", "info", "system",
                                                                  "pending")
    text = msg.text
    assert text.startswith("📊 Riepilogo giornaliero — 12/03/2027")
    assert "Ordini eseguiti: 1 · rifiutati: 1 · eventi critici: 1 · avvisi: 0" in text
    assert "10:15 Ordine eseguito: buy 1 AAPL" in text
    assert "Test 250 A" in text and "Ordine rifiutato: TSLA" in text
    assert "Kill switch attivo" in text and "Ieri" not in text
    assert "🧭 Decisioni di oggi (3)\nVeti e approvazioni\n• Compra AAPL — generale (Analyst)\n" in text
    assert "• Compra MSFT — generale (Analyst)\n❌ No — generale (Analyst)" in text
    [pre] = msg.entities
    table = text[len(text) - pre["length"]:]  # no emoji inside the table
    assert pre["type"] == "pre" and pre["offset"] == utf16_len(text) - pre["length"]
    assert table.startswith("Portafoglio")
    assert "+2,00" in table

    await scheduler.tick()
    clock.t = rome(FRI, 18, 5)
    await _scheduler(maker, clock).tick()  # restart: a new instance
    assert len(await _outbox(maker)) == 1


async def test_weekly_only_friday_1830(maker):
    await _configure(maker)
    await _portfolio(maker, "Test 250 A")
    clock = Clock(rome(THU, 18, 30))
    await _scheduler(maker, clock).tick()
    assert [m.category for m in await _outbox(maker)] == ["daily"]

    clock.t = rome(FRI, 18, 29)
    scheduler = _scheduler(maker, clock)
    await scheduler.tick()
    assert [m.category for m in await _outbox(maker)] == ["daily", "daily"]

    clock.t = rome(FRI, 18, 30)
    await scheduler.tick()
    await scheduler.tick()
    out = await _outbox(maker)
    assert [m.category for m in out] == ["daily", "daily", "weekly"]
    assert out[-1].text.startswith("📈 Riepilogo settimanale — 05/03–12/03/2027")
    async with maker() as s:
        assert await job_done(s, "telegram.weekly", "2027-03-12")


async def test_summary_skipped_after_2300(maker, caplog):
    await _configure(maker)
    clock = Clock(rome(FRI, 23))
    with caplog.at_level(logging.WARNING, logger="smtrading.telegram.summaries"):
        await _scheduler(maker, clock).tick()
    assert await _outbox(maker) == []
    skipped = [r.getMessage() for r in caplog.records]
    assert len(skipped) == 3  # digest, daily and weekly of the day
    assert any("telegram.daily of 2027-03-12 skipped" in m for m in skipped)
    async with maker() as s:
        assert await job_done(s, "telegram.daily", "2027-03-12")
    # the next day before 18:00: nothing is recovered
    clock.t = rome(13, 0, 30)
    await _scheduler(maker, clock).tick()
    assert await _outbox(maker) == []


async def test_not_ready_past_cutoff_warns_once(maker, caplog):
    """Telegram not ready when the 23:00 cutoff passes: one WARNING per summary and day,
    nothing claimed or sent."""
    await _configure(maker, enabled=False)
    scheduler = _scheduler(maker, Clock(rome(FRI, 23, 5)))
    with caplog.at_level(logging.WARNING, logger="smtrading.telegram.summaries"):
        await scheduler.tick()
        await scheduler.tick()
    messages = [r.getMessage() for r in caplog.records
                if r.name == "smtrading.telegram.summaries"]
    assert len(messages) == 3  # digest, daily and weekly, once each
    assert any("telegram.daily of 2027-03-12" in m and "not ready" in m for m in messages)
    assert await _outbox(maker) == []
    async with maker() as s:
        assert not await job_done(s, "telegram.daily", "2027-03-12")


async def test_worker_down_at_1800_sends_once_when_back(maker):
    await _configure(maker)
    clock = Clock(rome(FRI, 21, 40))
    await _scheduler(maker, clock).tick()
    assert sorted(m.category for m in await _outbox(maker)) == ["daily", "weekly"]


async def test_not_ready_config_no_summary(maker):
    clock = Clock(rome(FRI, 18, 45))
    await _scheduler(maker, clock).tick()  # never configured
    await _configure(maker, enabled=False)
    await _scheduler(maker, clock).tick()
    assert await _outbox(maker) == []
    async with maker() as s:
        assert not await job_done(s, "telegram.daily", "2027-03-12")
    # enabled later the same day: it goes out then
    await _configure(maker)
    clock.t = rome(FRI, 19)
    await _scheduler(maker, clock).tick()
    assert sorted(m.category for m in await _outbox(maker)) == ["daily", "weekly"]


async def test_paused_config_queues_summary_paused(maker):
    await _configure(maker, paused=True)
    await _scheduler(maker, Clock(rome(THU, 18))).tick()
    [msg] = await _outbox(maker)
    assert (msg.category, msg.status) == ("daily", "paused")


async def test_summary_leaves_out_archived_portfolios(maker):
    live = await _portfolio(maker, "Test 250 A")
    gone = await _portfolio(maker, "Vecchio")
    await _activity(maker, summary="Ordine eseguito: vivo", ts=rome(FRI, 10),
                    portfolio_id=live)
    await _activity(maker, summary="Ordine eseguito: archiviato", ts=rome(FRI, 10),
                    portfolio_id=gone)
    await _decisions(maker, [{"kind": "strategy", "summary": "Strategia archiviata",
                              "portfolio_id": gone}], at=rome(FRI, 11))
    await _archive(maker, gone)
    async with maker() as s:
        [(text, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ)
    assert "vivo" in text and "archiviato" not in text and "Vecchio" not in text
    assert "Decisioni di oggi: nessuna" in text and "Strategia archiviata" not in text


async def test_long_summary_split_with_pre_entities(maker):
    now = rome(FRI, 18)
    pids = [await _portfolio(maker, f"Test 250 n.{i:02d}") for i in range(60)]
    for i, pid in enumerate(pids):
        await _snaps(maker, pid, [(now - timedelta(hours=5), "250"),
                                  (now - timedelta(hours=1), str(250 + i))])
    for i in range(25):
        await _activity(maker, summary=f"🔴 Ordine eseguito n.{i} " + "x" * 180,
                        ts=now - timedelta(minutes=30 + i), portfolio_id=pids[i])
    async with maker() as s:
        parts = await build_summary(s, "daily", now, tz=TZ)
    assert len(parts) >= 2
    pre_lines: list[str] = []
    for text, entities in parts:
        assert utf16_len(text) <= MAX_LEN
        if entities is None:
            continue
        [e] = entities
        units = text.encode("utf-16-le")
        block = units[2 * e["offset"]:2 * (e["offset"] + e["length"])].decode("utf-16-le")
        assert e["offset"] + e["length"] == utf16_len(text)  # the block ends the part
        assert block == block.strip("\n") and block.split("\n")[-1].strip()
        pre_lines += block.split("\n")
    assert pre_lines[0].startswith("Portafoglio")
    assert len(pre_lines) == 61  # header + 60 portfolios, none lost or duplicated
    assert pre_lines[1].startswith("Test 250 n.59")  # best return first
    assert "… e altri 5" in "".join(text for text, _ in parts)  # sections are capped at 20 lines


# -- digest --------------------------------------------------------------------------


async def test_digest_at_0700_collects_night_events(maker):
    await _configure(maker)
    pid = await _portfolio(maker, "Test 250 A")
    first = await _activity(maker, summary="Ordine eseguito: buy 1 AAPL", ts=rome(THU, 23, 10),
                            portfolio_id=pid, telegram="digest")
    second = await _activity(maker, type="order.rejected", level="warning",
                             summary="Ordine rifiutato: TSLA", ts=rome(FRI, 2, 5),
                             telegram="digest")
    other = await _activity(maker, summary="già inviato", ts=rome(FRI, 1), telegram="queued")

    clock = Clock(rome(FRI, 6, 59, 59))
    await _scheduler(maker, clock).tick()
    assert await _outbox(maker) == []

    clock.t = rome(FRI, 7)
    await _scheduler(maker, clock).tick()
    [msg] = await _outbox(maker)
    assert (msg.category, msg.level, msg.entities) == ("digest", "info", None)
    assert msg.text.startswith("🌙 Riepilogo della notte")
    assert "23:10 Ordine eseguito: buy 1 AAPL — Test 250 A" in msg.text
    assert "02:05 Ordine rifiutato: TSLA" in msg.text
    assert msg.text.index("🟠 Avvisi") < msg.text.index("🟢 Ordini eseguiti")
    assert "già inviato" not in msg.text
    async with maker() as s:
        rows = {a.id: a for a in await s.scalars(select(Activity))}
    assert (rows[first].telegram, rows[first].outbox_id) == ("queued", msg.id)
    assert (rows[second].telegram, rows[second].outbox_id) == ("queued", msg.id)
    assert rows[other].outbox_id is None

    clock.t = rome(FRI, 7, 30)
    await _scheduler(maker, clock).tick()
    assert len(await _outbox(maker)) == 1


async def test_digest_archived_portfolio_rule(maker):
    """One rule with the summaries: a portfolio archived during the night is left out of the
    digest; its rows are marked `skipped` (never sent), not left as `digest`."""
    await _configure(maker)
    live = await _portfolio(maker, "Test 250 A")
    gone = await _portfolio(maker, "Vecchio")
    kept = await _activity(maker, summary="Ordine eseguito: vivo", ts=rome(THU, 23, 10),
                           portfolio_id=live, telegram="digest")
    dropped = await _activity(maker, summary="Ordine eseguito: archiviato",
                              ts=rome(THU, 23, 20), portfolio_id=gone, telegram="digest")
    global_row = await _activity(maker, type="kill_switch", level="warning",
                                 summary="Kill switch disattivato", ts=rome(FRI, 1),
                                 telegram="digest")
    await _archive(maker, gone)
    async with maker() as s:
        [(text, _)] = await build_digest(s, rome(FRI, 7), tz=TZ)
        await s.commit()
    assert "vivo" in text and "Kill switch disattivato" in text
    assert "archiviato" not in text and "Vecchio" not in text
    async with maker() as s:
        rows = {a.id: a.telegram for a in await s.scalars(select(Activity))}
    assert (rows[kept], rows[dropped], rows[global_row]) == ("queued", "skipped", "queued")


async def test_digest_only_archived_sends_nothing(maker):
    await _configure(maker)
    gone = await _portfolio(maker, "Vecchio")
    row = await _activity(maker, ts=rome(THU, 23, 10), portfolio_id=gone, telegram="digest")
    await _archive(maker, gone)
    async with maker() as s:
        assert await build_digest(s, rome(FRI, 7), tz=TZ) is None
        await s.commit()
    async with maker() as s:
        assert (await s.get(Activity, row)).telegram == "skipped"
    assert await _outbox(maker) == []


@respx.mock
async def test_night_fill_reaches_group_in_0700_digest(maker):
    """Across the tasks: a fill at 23:30 → planner `digest` → SummaryScheduler at 07:00 →
    OutboxSender sends it with sound (disable_notification false)."""
    await _configure(maker)
    pid = await _portfolio(maker, "Test 250 A", cash=10_000)
    await _fill(maker, pid, rome(THU, 23, 30))
    clock = Clock(rome(THU, 23, 30, 5))
    assert await TelegramPlanner(maker, clock=clock, tz=TZ).tick() == 1
    async with maker() as s:
        [row] = list(await s.scalars(select(Activity).where(Activity.type == "order.filled")))
    assert row.telegram == "digest"
    assert await _outbox(maker) == []

    route = respx.post(f"{API_URL}/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 7}}))
    clock.t = rome(FRI, 7, 0, 10)
    await _scheduler(maker, clock).tick()
    async with httpx.AsyncClient() as http:
        sender = OutboxSender(maker, http, clock=clock, tz=TZ)
        assert await sender.tick() == 1
    [body] = [json.loads(c.request.content) for c in route.calls]
    assert body["text"].startswith("🌙 Riepilogo della notte")
    assert "23:30" in body["text"] and "Test 250 A" in body["text"]
    assert body["disable_notification"] is False
    [msg] = await _outbox(maker)
    assert (msg.category, msg.status, msg.telegram_message_id) == ("digest", "sent", 7)


async def test_digest_without_events_sends_nothing(maker):
    await _configure(maker)
    await _scheduler(maker, Clock(rome(FRI, 7))).tick()
    assert await _outbox(maker) == []
    async with maker() as s:
        assert await build_digest(s, rome(FRI, 7), tz=TZ) is None
        assert await job_done(s, "telegram.digest", "2027-03-12")


async def test_daily_summary_groups_decisions(maker):
    pid = await _portfolio(maker, "Prudente")
    await _decisions(maker, [
        {"kind": "veto", "summary": "Veto su TSLA", "portfolio_id": pid,
         "outcome": "respinta", "subject": "acquisto TSLA", "reason": "troppo volatile"},
        {"kind": "strategy", "summary": "s1", "portfolio_id": pid, "outcome": "approvata",
         "subject": "incrocio medie mobili 10/30 su SPY, QQQ e DIA",
         "reason": "trend confermato", "details": {"ref": "STR-001"}},
        {"kind": "risk_rule", "summary": "Stop loss al 5% per tutti", "outcome": "informativa"},
        {"kind": "strategy", "summary": "Momentum su NVDA", "portfolio_id": pid},
    ], at=rome(FRI, 9))
    await _decisions(maker, [{"kind": "strategy", "summary": "Ieri"}], at=rome(THU, 20),
                     agent="Analyst")
    async with maker() as s:
        [(text, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ)
    assert "Ieri" not in text
    assert ("\n🧭 Decisioni di oggi (4)\n"
            "Strategie\n"
            "✅ STR-001 · incrocio medie mobili 10/30 su SPY, QQQ e DIA — Prudente — "
            "trend confermato (CEO)\n"
            "• Momentum su NVDA — Prudente (CEO)\n"
            "Rischio\n"
            "ℹ️ Stop loss al 5% per tutti — generale (CEO)\n"
            "Veti e approvazioni\n"
            "❌ acquisto TSLA — Prudente — troppo volatile (CEO)\n") in text
    assert "Allocazioni" not in text and "Altro" not in text


async def test_decision_of_a_removed_agent_is_still_listed(maker):
    """An agent row gone (outer join): the decision stays, «Agente rimosso» (plan 10, M4)."""
    await _decisions(maker, [{"kind": "strategy", "summary": "Momentum su NVDA"}],
                     at=rome(FRI, 9), agent="Ex")
    async with maker() as s:  # no delete path exists: the FK is bypassed to simulate one
        await s.execute(sql("SET LOCAL session_replication_role = replica"))
        await s.execute(sql("DELETE FROM agents WHERE name = 'Ex'"))
        await s.commit()
    async with maker() as s:
        [(out, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ)
    assert "🧭 Decisioni di oggi (1)\nStrategie\n• Momentum su NVDA — generale " \
           "(Agente rimosso)\n" in out


async def test_decisions_capped_at_15(maker):
    await _decisions(maker, [{"kind": "other", "summary": f"Decisione n.{i:02d}"}
                             for i in range(18)], at=rome(FRI, 9))
    async with maker() as s:
        [(text, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ)
    assert "🧭 Decisioni di oggi (18)\nAltro\n" in text
    shown = [ln for ln in text.splitlines() if ln.startswith("• Decisione n.")]
    assert len(shown) == 15 and shown[-1].startswith("• Decisione n.14")
    assert "• Decisione n.14 — generale (CEO)\n… e altre 3\n" in text


async def test_weekly_decisions_section(maker):
    await _decisions(maker, [{"kind": "allocation", "summary": "Più peso su ETF",
                              "outcome": "modificata"}], at=rome(9, 10))
    await _decisions(maker, [{"kind": "approval", "summary": "Ok al ribilanciamento"}],
                     at=rome(FRI, 10), agent="Risk")
    await _decisions(maker, [{"kind": "other", "summary": "Troppo vecchia"}],
                     at=rome(4, 10), agent="Old")
    async with maker() as s:
        [(text, _)] = await build_summary(s, "weekly", rome(FRI, 18, 30), tz=TZ)
        [(empty, _)] = await build_summary(s, "weekly", rome(FRI, 18, 30) + timedelta(days=30),
                                           tz=TZ)
    assert ("🧭 Decisioni della settimana (2)\n"
            "Allocazioni\n"
            "09/03 ✏️ Più peso su ETF — generale (CEO)\n"
            "Veti e approvazioni\n"
            "12/03 ✅ Ok al ribilanciamento — generale (Risk)\n") in text
    assert "Troppo vecchia" not in text
    assert "Decisioni della settimana: nessuna" in empty


# -- weekly goal -----------------------------------------------------------------------

MON = 8  # Monday 8 March 2027


async def _day_open(maker, pid, day: int, equity: str) -> None:
    async with maker() as s:
        s.add(EquitySnapshot(portfolio_id=pid, ts=rome(day, 0, 1), kind=SnapshotKind.day_open,
                             local_date=rome(day, 12).astimezone(ROME).date(),
                             equity_eur=D(equity), cash_eur=D(equity), exposure_eur=D(0),
                             margin_used_eur=D(0)))
        await s.commit()


def _goal(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("🎯")]


async def test_goal_line_in_daily_and_weekly(maker):
    a = await _portfolio(maker, "Test 260 A")
    b = await _portfolio(maker, "Test 260 B")  # created on Wednesday: no Monday day_open
    e = await _portfolio(maker, "Test 260 E")
    await _day_open(maker, a, 1, "500")  # last week's Monday: ignored
    await _day_open(maker, a, MON, "600")
    await _snaps(maker, a, [(rome(MON, 12), "610"), (rome(FRI, 17), "603")])
    await _snaps(maker, b, [(rome(10, 11), "400"), (rome(11, 15), "401.20")])
    await _day_open(maker, e, MON, "9000")
    await _archive(maker, e)
    async with maker() as s:
        [(daily, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ)
        [(weekly, _)] = await build_summary(s, "weekly", rome(FRI, 18, 30), tz=TZ)
    line = ("🎯 Obiettivo settimanale +2%: 1.000,00 → 1.020,00 € — attuale 1.004,20 € "
            "(+0,42%) · in ritardo")
    assert _goal(daily) == [line] and _goal(weekly) == [line]
    assert daily.split("\n")[2] == line  # right under the counts


async def test_goal_off_custom_pct_and_missing_data(maker):
    async with maker() as s:
        [(text, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ)
    assert _goal(text) == [
        "🎯 Obiettivo settimanale +2%: dati del periodo non ancora disponibili"]
    a = await _portfolio(maker, "Test 260 A")
    await _snaps(maker, a, [(rome(FRI, 9), "100"), (rome(FRI, 17), "99")])
    async with maker() as s:
        await s.execute(update(Goal).values(target_pct=D("1.5")))
        await s.commit()
        [(text, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ)
        assert _goal(text) == [("🎯 Obiettivo settimanale +1,5%: 100,00 → 101,50 € — "
                                "attuale 99,00 € (-1,00%) · in ritardo")]
        await s.execute(update(Goal).values(active=False))
        await s.commit()
        [(text, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ)
    assert _goal(text) == []


async def test_one_line_per_goal_and_archived_portfolio_hidden(maker):
    from datetime import date

    a = await _portfolio(maker, "Test 260 A")
    e = await _portfolio(maker, "Test 260 E")
    await _day_open(maker, a, 1, "1000")  # 1 March: the month's start
    await _snaps(maker, a, [(rome(FRI, 17), "1030")])
    async with maker() as s:
        s.add(Goal(name="mensile", horizon="month", start_date=date(2027, 3, 1),
                   recurring=True, scope="portfolio", portfolio_id=a,
                   target_pct=D("3"), kind="min", active=True))
        s.add(Goal(name="tetto", horizon="quarter", start_date=date(2027, 1, 1),
                   recurring=True, scope="total", target_pct=D("2"), kind="cap",
                   active=True))
        s.add(Goal(name="aprile", horizon="month", start_date=date(2027, 4, 1),
                   recurring=False, scope="total", target_pct=D("1"), kind="min",
                   active=True))
        s.add(Goal(name="febbraio", horizon="month", start_date=date(2027, 2, 1),
                   recurring=False, scope="total", target_pct=D("1"), kind="min",
                   active=True))  # over: no line
        s.add(Goal(name="archiviato", horizon="week", start_date=date(2027, 3, 8),
                   recurring=False, scope="portfolio", portfolio_id=e,
                   target_pct=D("1"), kind="min", active=True))
        await s.commit()
    await _archive(maker, e)
    async with maker() as s:
        [(text, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ)
    # the week: no Monday day_open, so its first snapshot of the week (Friday 17:00)
    assert _goal(text) == [
        ("🎯 Obiettivo settimanale +2%: 1.030,00 → 1.050,60 € — attuale 1.030,00 € "
        "(0,00%) · in ritardo"),
        "🎯 Obiettivo aprile +1%: dal 01/04/2027",
        ("🎯 Obiettivo mensile +3% · Test 260 A: 1.000,00 → 1.030,00 € — attuale 1.030,00 € "
        "(+3,00%) · raggiunto"),
        ("🎯 Obiettivo tetto +2% (tetto): 1.000,00 → 1.020,00 € — attuale 1.030,00 € "
        "(+3,00%) · oltre il tetto"),
    ]


async def test_period_end_line_once(maker):
    """The «🏁» line of a period over goes in the daily summary of its last day (or of a
    later one within a week), once; never for a period over before the goal existed."""
    a = await _portfolio(maker, "Test 260 A")
    await _day_open(maker, a, MON, "1000")
    await _snaps(maker, a, [(rome(FRI, 17), "1025")])
    async with maker() as s:
        goal_id = await s.scalar(select(Goal.id))
        await s.execute(update(Goal).values(created_at=rome(1, 9)))
        await s.commit()
        [(friday, _)] = await build_summary(s, "daily", rome(FRI, 18), tz=TZ,
                                            period_ends=True)
        assert [ln for ln in friday.splitlines() if ln.startswith("🏁")] == []
        # Sunday 14 March, the week's last day (last week, 1–7 March, has no data)
        [(sunday, _)] = await build_summary(s, "daily", rome(14, 18), tz=TZ,
                                            period_ends=True)
        await s.commit()
        assert [ln for ln in sunday.splitlines() if ln.startswith("🏁")] == [
            "🏁 Obiettivo settimanale: raggiunto (+2,50% su +2%)"]
        assert await job_done(s, "goals.period_end", f"{goal_id}:2027-03-01:2027-03-07")
        assert await job_done(s, "goals.period_end", f"{goal_id}:2027-03-08:2027-03-14")
        [(monday, _)] = await build_summary(s, "daily", rome(15, 18), tz=TZ,
                                            period_ends=True)
        assert [ln for ln in monday.splitlines() if ln.startswith("🏁")] == []
        # a goal created this week: last week is never announced
        await _snaps(maker, a, [(rome(4, 12), "990")])
        await s.execute(update(Goal).values(created_at=rome(15, 9)))
        await s.execute(sql("delete from job_runs where job = 'goals.period_end'"))
        await s.commit()
        [(monday, _)] = await build_summary(s, "daily", rome(15, 18), tz=TZ,
                                            period_ends=True)
        assert [ln for ln in monday.splitlines() if ln.startswith("🏁")] == []
