"""«Resoconto del venerdì»: the week's PDF sent to the group as a document (respx only:
the real Telegram API is never called)."""

import io
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from pypdf import PdfReader
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.events import discard_post_commit
from smtrading.goals.service import set_weekly_goal
from smtrading.jobs.runs import job_done
from smtrading.paperclip.models import AgentCostDay
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import AppSetting, EquitySnapshot, SnapshotKind
from smtrading.seed import run_seed
from smtrading.telegram import config as tg_config
from smtrading.telegram import weekly_report as wr
from smtrading.telegram.client import API_URL
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.models import OutboxStatus, TelegramOutbox
from smtrading.telegram.outbox import MAX_DOCUMENT_BYTES, enqueue_document, resume_paused
from smtrading.telegram.sender import OutboxSender
from smtrading.telegram.weekly_report import JOB, WeeklyReport

D = Decimal
TZ = "Europe/Rome"
ROME = ZoneInfo(TZ)
TOKEN = "123456789:AAHsecretSECRETsecret_SECRET-1234567"
DOC_URL = f"{API_URL}/bot{TOKEN}/sendDocument"
MONDAY = date(2027, 3, 8)  # the 12th is a Friday


def rome(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2027, 3, day, h, m, tzinfo=ROME).astimezone(UTC)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


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
        await s.commit()
    yield sm
    await engine.dispose()


async def _configure(maker) -> None:
    async with maker() as s:
        await save_config(s, TelegramConfig(bot_token=TOKEN, chat_id="-100123", enabled=True))
        await s.commit()


async def _setting(maker, value: dict) -> None:
    async with maker() as s:
        await s.execute(insert(AppSetting).values(key="weekly_report_pdf", value=value)
                        .on_conflict_do_update(index_elements=["key"], set_={"value": value}))
        await s.commit()


async def _portfolio(maker, name: str, points: list[tuple[datetime, str]], *,
                     in_totals: bool = True) -> uuid.UUID:
    async with maker() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(0))
        p.in_totals = in_totals
        discard_post_commit(s)
        for ts, equity in points:
            s.add(EquitySnapshot(portfolio_id=p.id, ts=ts, kind=SnapshotKind.periodic,
                                 equity_eur=D(equity), cash_eur=D(equity),
                                 exposure_eur=D(0), margin_used_eur=D(0)))
        await s.commit()
        return p.id


async def _week(maker) -> None:
    """A (in the totals) +3% over the week, B out of the totals, the weekly goal +2%,
    agents' costs of the week."""
    await _portfolio(maker, "Squadra A", [(rome(8, 10), "1000"), (rome(12, 20), "1030")])
    await _portfolio(maker, "Esperimento B", [(rome(8, 10), "500"), (rome(12, 20), "490")],
                     in_totals=False)
    async with maker() as s:
        await set_weekly_goal(s, on=True, pct=D(2), today=MONDAY)
        s.add_all([
            AgentCostDay(day=MONDAY, paperclip_agent_id="pc-1", paperclip_agent_name="CEO",
                         api_usd=D("1.5"), subscription_usd=D("2"), other_usd=D(0), runs=3),
            AgentCostDay(day=MONDAY - timedelta(days=1), paperclip_agent_id="pc-1",
                         paperclip_agent_name="CEO", api_usd=D(9), subscription_usd=D(0),
                         other_usd=D(0), runs=9),  # the week before: left out
        ])
        await s.commit()


async def _rows(maker) -> list[TelegramOutbox]:
    async with maker() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


async def _document(maker, row_id: int) -> bytes | None:
    async with maker() as s:
        return await s.scalar(select(TelegramOutbox.document)
                              .where(TelegramOutbox.id == row_id))


def _job(maker, clock) -> WeeklyReport:
    return WeeklyReport(maker, FakeQuoteSource(), clock=clock, tz=TZ)


def _pdf_pages(body: bytes) -> list[str]:
    return [p.extract_text() for p in PdfReader(io.BytesIO(body)).pages]


@respx.mock
async def test_friday_report_is_queued_once_and_sent_as_a_document(maker):
    await _configure(maker)
    await _week(maker)
    clock = Clock(rome(12, 22, 29))
    await _job(maker, clock).tick()
    assert await _rows(maker) == []  # before 22:30

    clock.t = rome(12, 22, 30)
    await _job(maker, clock).tick()
    [row] = await _rows(maker)
    assert (row.category, row.status, row.source, row.document_name) == (
        "weekly", "pending", "system", "resoconto-settimanale-2027-03-08-2027-03-12.pdf")
    assert row.text.split("\n") == [
        "📄 Resoconto settimanale — 08/03–12/03/2027",
        "P&L totale: +30,00 € (+3,00%)",
        "Obiettivo settimanale +2,0%: raggiunto ✅ (ora +3,00%)",
        "Costo agenti: 3,50 USD",
        "Fuori dai totali: 1 portafogli (pagina a parte)",
    ]
    body = await _document(maker, row.id)
    assert body is not None and body.startswith(b"%PDF")
    pages = _pdf_pages(body)
    assert "Resoconto settimanale" in pages[0]
    assert "Squadra A" in pages[0]
    assert "Portafogli fuori dai totali" in pages[-1] and "Esperimento B" in pages[-1]
    assert "Totale fuori dai totali" in pages[-1]
    assert not any("Portafogli fuori dai totali" in p for p in pages[:-1])

    # idempotent per week: a restarted worker (new instance) sends nothing more
    clock.t = rome(12, 23, 30)
    await _job(maker, clock).tick()
    assert len(await _rows(maker)) == 1
    async with maker() as s:
        assert await job_done(s, JOB, "2027-03-08")

    route = respx.post(DOC_URL).mock(return_value=httpx.Response(
        200, json={"ok": True, "result": {"message_id": 42}}))
    async with httpx.AsyncClient() as http:
        sender = OutboxSender(maker, http, clock=clock, tz=TZ,
                              sleep=lambda s: _noop(clock, s))
        assert await sender.tick() == 1
    [call] = route.calls
    content = call.request.content
    assert call.request.headers["content-type"].startswith("multipart/form-data")
    assert b'filename="resoconto-settimanale-2027-03-08-2027-03-12.pdf"' in content
    assert body in content
    assert "P&L totale: +30,00 €".encode() in content
    assert b'name="disable_notification"\r\n\r\ntrue' in content  # 23:30: quiet hours
    [row] = await _rows(maker)
    assert (row.status, row.telegram_message_id) == ("sent", 42)
    assert row.document_name is not None
    assert await _document(maker, row.id) is None  # the file is not kept once sent


async def _noop(clock: Clock, seconds: float) -> None:
    clock.advance(seconds)


async def test_off_not_ready_late_and_custom_day(maker):
    await _week(maker)
    # off: nothing, nothing claimed
    await _setting(maker, {"on": False, "weekday": 4, "at": "22:30"})
    await _configure(maker)
    await _job(maker, Clock(rome(12, 22, 30))).tick()
    assert await _rows(maker) == []
    # on, Telegram not ready: nothing claimed, sent once it is (within the window)
    await _setting(maker, {"on": True, "weekday": 4, "at": "22:30"})
    async with maker() as s:
        await save_config(s, TelegramConfig())
        await s.commit()
    job = _job(maker, Clock(rome(12, 22, 31)))
    await job.tick()
    assert await _rows(maker) == []
    async with maker() as s:
        assert not await job_done(s, JOB, "2027-03-08")
    await _configure(maker)
    job._clock = Clock(rome(13, 9))  # Saturday morning, still within 24 h
    await job.tick()
    assert len(await _rows(maker)) == 1

    # the next week, worker down past the window: claimed and skipped
    await _job(maker, Clock(rome(20, 22, 31))).tick()  # Saturday 22:31, > 24 h late
    assert len(await _rows(maker)) == 1
    async with maker() as s:
        assert await job_done(s, JOB, "2027-03-15")

    # another day and time: Monday 09:00, the period is Monday only
    await _setting(maker, {"on": True, "weekday": 0, "at": "09:00"})
    await _job(maker, Clock(rome(22, 8, 59))).tick()
    assert len(await _rows(maker)) == 1
    await _job(maker, Clock(rome(22, 9))).tick()
    rows = await _rows(maker)
    assert len(rows) == 2
    assert rows[-1].document_name == "resoconto-settimanale-2027-03-22-2027-03-22.pdf"
    assert rows[-1].text.startswith("📄 Resoconto settimanale — 22/03–22/03/2027")


async def test_without_goal_or_outside_portfolios(maker):
    await _configure(maker)
    await _portfolio(maker, "Solo A", [(rome(8, 10), "1000"), (rome(12, 20), "990")])
    async with maker() as s:  # the migrated weekly goal turned off
        await set_weekly_goal(s, on=False, pct=D(2), today=MONDAY)
        await s.commit()
    await _job(maker, Clock(rome(12, 22, 30))).tick()
    [row] = await _rows(maker)
    lines = row.text.split("\n")
    assert lines[1] == "P&L totale: −10,00 € (−1,00%)"
    assert lines[2] == "Obiettivo settimanale: nessuno attivo"
    assert not any("Fuori dai totali" in line for line in lines)
    pages = _pdf_pages(await _document(maker, row.id))
    assert not any("Portafogli fuori dai totali" in p for p in pages)


async def test_pdf_over_the_limit_sends_the_caption_with_a_note(maker, monkeypatch):
    await _configure(maker)
    await _week(maker)
    monkeypatch.setattr(wr, "MAX_DOCUMENT_BYTES", 10)
    await _job(maker, Clock(rome(12, 22, 30))).tick()
    [row] = await _rows(maker)
    assert row.document_name is None
    assert row.text.startswith("📄 Resoconto settimanale")
    assert "supera il limite di 20 MB" in row.text


async def test_enqueue_document_limits_and_stale_skip_clears_the_file(maker):
    async with maker() as s:
        for kw in ({"content": b""}, {"content": b"x" * (MAX_DOCUMENT_BYTES + 1)},
                   {"caption": "x" * 1025}, {"filename": "a/b.pdf"}, {"filename": ""}):
            args = {"source": "system", "category": "weekly", "level": "info",
                    "caption": "c", "filename": "r.pdf", "content": b"%PDF", **kw}
            with pytest.raises(ValueError):
                await enqueue_document(s, **args)
        await save_config(s, TelegramConfig(bot_token=TOKEN, chat_id="-1", enabled=True,
                                            paused=True))
        row = await enqueue_document(s, source="system", category="weekly", level="info",
                                     caption="c", filename="r.pdf", content=b"%PDF")
        assert row.status == OutboxStatus.paused
        row.created_at = datetime.now(UTC) - timedelta(days=2)
        await s.flush()
        await resume_paused(s)
        await s.commit()
    [row] = await _rows(maker)
    assert row.status == OutboxStatus.skipped
    assert await _document(maker, row.id) is None


@respx.mock
async def test_document_send_error_keeps_the_file_for_the_retry(maker):
    await _configure(maker)
    async with maker() as s:
        await enqueue_document(s, source="system", category="weekly", level="info",
                               caption="c", filename="r.pdf", content=b"%PDF-1")
        await s.commit()
    respx.post(DOC_URL).mock(return_value=httpx.Response(400, json={
        "ok": False, "error_code": 400, "description": "Bad Request: file too big"}))
    clock = Clock(rome(10, 10))
    async with httpx.AsyncClient() as http:
        sender = OutboxSender(maker, http, clock=clock, tz=TZ,
                              sleep=lambda s: _noop(clock, s))
        assert await sender.tick() == 1
    [row] = await _rows(maker)
    assert (row.status, row.attempts) == ("pending", 1)
    assert "file too big" in row.last_error
    assert await _document(maker, row.id) == b"%PDF-1"
