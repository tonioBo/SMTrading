"""Richer Telegram texts: the fill notification (strategy, stop/target, risk at stop in EUR)
and «📌 Posizioni aperte» in the daily and weekly summaries. Tests on the rendered text."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Decision
from smtrading.agents.service import create_agent
from smtrading.common.db import make_engine
from smtrading.engine.models import Order, OrderSide, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_bracket_order, place_order
from smtrading.events import discard_post_commit
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed
from smtrading.telegram import config as tg_config
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.details import money, pct_text, qty_text
from smtrading.telegram.models import TelegramOutbox
from smtrading.telegram.planner import TelegramPlanner
from smtrading.telegram.render import MAX_LEN, utf16_len
from smtrading.telegram.summaries import build_summary

D = Decimal
TZ = "Europe/Rome"
TOKEN = "123456789:AAHsecretSECRETsecret_SECRET-1234567"
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
NOW = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)  # 18:00 Rome
COID = "PROP-AGG-20261110-1500-1"


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
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))
        await save_config(s, TelegramConfig(bot_token=TOKEN, chat_id="-100123", enabled=True))
        await s.commit()
    yield sm
    await engine.dispose()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("199.90"), D("200.10"), D("200.00"))
    return q


async def _portfolio(maker, name="Aggressivo") -> uuid.UUID:
    async with maker() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(10000))
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _proposal(maker, pid, strategy="STR-103") -> None:
    async with maker() as s:
        agent, _ = await create_agent(s, f"PM {uuid.uuid4().hex[:6]}")
        s.add(Decision(agent_id=agent.id, portfolio_id=pid, kind="proposal",
                       summary="acquisto AAPL", details={"ref": COID,
                                                         "strategy_id": strategy}))
        await s.commit()


async def _bracket(maker, qs, pid, *, coid=COID) -> Order:
    async with maker() as s:
        entry, _, _ = await place_bracket_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(2), client_order_id=coid,
        ), take_profit=D(210), stop_loss=D("190.00"), now=OPEN)
        discard_post_commit(s)
        await s.commit()
        return entry


async def _market(maker, qs, pid) -> Order:
    async with maker() as s:
        order = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(1)), now=OPEN)
        discard_post_commit(s)
        await s.commit()
        return order


async def _texts(maker) -> list[str]:
    async with maker() as s:
        return list(await s.scalars(select(TelegramOutbox.text).order_by(TelegramOutbox.id)))


def test_formatting():
    assert money(D("1234.5"), "EUR") == "1.234,50 €"
    assert money(D("242.2"), "USD") == "242,20 USD"
    assert money(D("0.35"), "EUR", sign=True) == "+0,35 €"
    assert money(D("-0.354"), "EUR") == "-0,354 €"
    assert money(D("0.000123"), "USD") == "0,000123 USD"
    assert qty_text(D("0.510000")) == "0,51"
    assert pct_text(D("-0.2654")) == "-0,27%"
    assert pct_text(D("12")) == "+12,00%"


# -- fill notification ---------------------------------------------------------------


async def test_fill_with_strategy_bracket_and_risk_in_eur(maker, qs):
    pid = await _portfolio(maker)
    await _proposal(maker, pid)
    entry = await _bracket(maker, qs, pid)
    assert entry.avg_fill_price is not None
    await TelegramPlanner(maker, clock=Clock(NOW), tz=TZ, qs=qs).tick()
    [text] = await _texts(maker)
    risk_eur = (entry.avg_fill_price - D(190)) * 2 / D("1.25")
    lines = text.split("\n")
    assert lines[0] == "🟢 Ordine eseguito — Aggressivo"
    assert lines[1].startswith("Ordine eseguito: buy 2 AAPL")
    assert lines[2] == "Strategia: STR-103"
    assert lines[3] == "Stop 190,00 USD · obiettivo 210,00 USD"
    assert lines[4] == f"Rischio allo stop: {money(risk_eur, 'EUR')}"
    assert lines[4] == "Rischio allo stop: 16,16 €"  # (200,10 - 190) × 2 / 1,25


async def test_fill_risk_in_instrument_currency_without_fx(maker, qs):
    pid = await _portfolio(maker)
    await _bracket(maker, qs, pid, coid="manual-1")
    await TelegramPlanner(maker, clock=Clock(NOW), tz=TZ).tick()  # no quote source
    [text] = await _texts(maker)
    assert "Strategia" not in text
    assert "Stop 190,00 USD · obiettivo 210,00 USD" in text
    assert "Rischio allo stop: 20,20 USD" in text


async def test_plain_fill_has_no_extra_lines(maker, qs):
    pid = await _portfolio(maker)
    await _market(maker, qs, pid)
    await TelegramPlanner(maker, clock=Clock(NOW), tz=TZ, qs=qs).tick()
    [text] = await _texts(maker)
    assert "Strategia" not in text and "Stop" not in text and "Rischio" not in text
    assert len(text.split("\n")) == 3  # head, summary, time: as before


# -- «Posizioni aperte» ---------------------------------------------------------------


async def test_daily_and_weekly_list_open_positions(maker, qs):
    pid = await _portfolio(maker)
    await _bracket(maker, qs, pid)
    await _portfolio(maker, "Prudente")  # no position: no line
    for kind in ("daily", "weekly"):
        async with maker() as s:
            parts = await build_summary(s, kind, NOW, tz=TZ, qs=qs)
        text = "\n".join(t for t, _ in parts)
        assert "📌 Posizioni aperte (1)" in text
        # 2 × 200 (mid) = 400 USD = 320 €; P/L (200 - 200,10) × 2 = -0,20 USD = -0,16 €
        assert ("AAPL 2 · 320,00 € · -0,16 € (-0,05%) · stop 190,00 USD — Aggressivo"
                in text)
        assert all(utf16_len(t) <= MAX_LEN for t, _ in parts)


async def test_no_positions_no_section(maker, qs):
    await _portfolio(maker)
    async with maker() as s:
        parts = await build_summary(s, "daily", NOW, tz=TZ, qs=qs)
    assert "Posizioni aperte" not in "\n".join(t for t, _ in parts)


async def test_unprotected_position_and_unavailable_quote(maker, qs):
    pid = await _portfolio(maker)
    await _market(maker, qs, pid)
    async with maker() as s:
        parts = await build_summary(s, "daily", NOW, tz=TZ, qs=qs)
    assert "stop nessuno — Aggressivo" in "\n".join(t for t, _ in parts)

    empty = FakeQuoteSource()  # no quote: one line, the summary still goes out
    async with maker() as s:
        parts = await build_summary(s, "daily", NOW, tz=TZ, qs=empty)
        text = "\n".join(t for t, _ in parts)
    assert "Aggressivo: valori non disponibili" in text
    assert "Classifica" in text


async def test_without_quote_source_no_section(maker, qs):
    pid = await _portfolio(maker)
    await _market(maker, qs, pid)
    async with maker() as s:
        parts = await build_summary(s, "daily", NOW, tz=TZ)
    assert "Posizioni aperte" not in "\n".join(t for t, _ in parts)
