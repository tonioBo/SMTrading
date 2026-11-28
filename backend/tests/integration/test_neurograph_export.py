"""The nightly export to Neurograph's ingest API (smtrading.neurograph), against a fake
Neurograph: rows of the three files, the exact request, the nightly rules (once per night,
catch-up of missed days in date order, 400 / 401 / 5xx), the campaign-only filter, the key
that never leaves the request, and the «Fiume dei flussi» export events.

Each test works in its own rolled-back transaction (the jobs' sessions are that same
session), on days far from any committed row."""

import hashlib
import json
import logging
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from smtrading.activity.models import Decision
from smtrading.agents.models import Agent, AgentPortfolio
from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.flow.events import flow_events
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.ledger.models import Journal, JournalType
from smtrading.neurograph import export, sender
from smtrading.neurograph.models import NeurographExport
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.models import JobRun
from smtrading.telegram.models import TelegramOutbox

ROME = ZoneInfo("Europe/Rome")
KEY = "ng-export-secret-key-0123456789"
DAY = date(2034, 6, 10)
# 22:30 UTC of June 9 is 00:30 of June 10 in Rome (summer time)
IN_DAY = datetime(2034, 6, 9, 22, 30, tzinfo=UTC)


def night(d: date, hour: int = 1) -> datetime:
    """`hour` o'clock (Rome) of the night that starts day `d`."""
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=ROME)


class _SM:
    """A sessionmaker whose sessions are the test's session (rolled back at the end)."""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def ng():
    """A fake Neurograph ingest API: `answer(kind, day)` -> (status, json body) decides each
    response (default 201); every request is recorded."""
    state = SimpleNamespace(requests=[], answer=lambda kind, day: (201, {"esito": "ricevuto"}))

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            parts = self.path.rstrip("/").split("/")
            kind, day = parts[-2], parts[-1]
            state.requests.append({"path": self.path, "headers": dict(self.headers),
                                   "body": body, "kind": kind, "day": day})
            status, out = state.answer(kind, day)
            b = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{srv.server_address[1]}/api/dati/smtrading"
    yield state
    srv.shutdown()


def _settings(url, key=KEY):
    return SimpleNamespace(neurograph_export_url=url, tz="Europe/Rome",
                           neurograph_export_key=SecretStr(key) if key is not None else None)


@pytest.fixture(autouse=True)
def _short_window(monkeypatch):
    monkeypatch.setattr(sender, "BACKFILL_DAYS", 2)
    monkeypatch.setattr(sender, "FIRST_DAY", date(2034, 1, 1))


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


async def _nightly(db_session, http, now, url, key=KEY):
    return await sender.run_nightly(now, sessionmaker=_SM(db_session), http=http,
                                    settings=_settings(url, key))


def _code() -> str:
    return "N" + uuid.uuid4().hex[:5].upper()


@pytest.fixture
async def world(db_session):
    """A campaign portfolio (code, Alpaca link), a hidden test portfolio and an archived
    one; an agent; an instrument."""
    sym = f"NG{uuid.uuid4().hex[:6].upper()}"
    db_session.add(Instrument(symbol=sym, name=sym, asset_class=AssetClass.us_equity,
                              market="US", currency="USD"))
    link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind="alpaca_paper")
    agent = Agent(name=f"PM {uuid.uuid4().hex[:8]}", token_prefix=uuid.uuid4().hex[:8],
                  token_hash="x")
    db_session.add_all([link, agent])
    await db_session.flush()
    camp = Portfolio(name=f"C-{uuid.uuid4().hex[:8]}", code=_code(), broker_link_id=link.id)
    hidden = Portfolio(name=f"H-{uuid.uuid4().hex[:8]}", in_totals=False)
    archived = Portfolio(name=f"A-{uuid.uuid4().hex[:8]}", status=PortfolioStatus.archived)
    flagged = Portfolio(name=f"I-{uuid.uuid4().hex[:8]}", in_totals=False,
                        neurograph_export=True)
    flagged_archived = Portfolio(name=f"IA-{uuid.uuid4().hex[:8]}", in_totals=False,
                                 neurograph_export=True, status=PortfolioStatus.archived)
    db_session.add_all([camp, hidden, archived, flagged, flagged_archived])
    await db_session.flush()
    db_session.add(AgentPortfolio(agent_id=agent.id, portfolio_id=camp.id, permission="trade"))
    await db_session.flush()
    return SimpleNamespace(sym=sym, link=link, agent=agent, camp=camp, hidden=hidden,
                           archived=archived, flagged=flagged,
                           flagged_archived=flagged_archived)


async def _agent(db_session, *portfolios):
    a = Agent(name=f"AG {uuid.uuid4().hex[:8]}", token_prefix=uuid.uuid4().hex[:8],
              token_hash="x")
    db_session.add(a)
    await db_session.flush()
    for p in portfolios:
        db_session.add(AgentPortfolio(agent_id=a.id, portfolio_id=p.id, permission="trade"))
    await db_session.flush()
    return a


async def _decision(db_session, w, portfolio, *, at=IN_DAY, agent=None, **kw):
    d = Decision(agent_id=(agent or w.agent).id, portfolio_id=portfolio.id if portfolio else None,
                 kind="strategy", summary="Compro su rottura", details={"livello": 101},
                 symbols=[w.sym], created_at=at, **kw)
    db_session.add(d)
    await db_session.flush()
    return d


async def _order(db_session, w, portfolio, *, at=IN_DAY, **kw):
    fields = {"source": "agent", "agent_id": str(w.agent.id), **kw}
    o = Order(portfolio_id=portfolio.id, symbol=w.sym, side=OrderSide.buy,
              type=OrderType.limit, qty=D("3.50000000"), limit_price=D("101.2500"),
              status=OrderStatus.filled, created_at=at, updated_at=at + timedelta(minutes=5),
              **fields)
    db_session.add(o)
    await db_session.flush()
    return o


async def _fill(db_session, o, *, at=IN_DAY + timedelta(minutes=1)):
    h = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    j = Journal(type=JournalType.TRADE, author_type="system", prev_hash=h,
                hash=hashlib.sha256(h.encode()).hexdigest())
    db_session.add(j)
    await db_session.flush()
    f = Fill(order_id=o.id, portfolio_id=o.portfolio_id, symbol=o.symbol, side=o.side,
             qty=D("3.5"), price=D("101.20000000"), quote_bid=D("101.1"), quote_ask=D("101.3"),
             quote_last=D("101.2"), quote_ts=at - timedelta(seconds=1), fee_amount=D("0.35"),
             fee_currency="USD", journal_id=j.id, created_at=at, source="broker")
    db_session.add(f)
    await db_session.flush()
    return f


# --- rows ---------------------------------------------------------------------------


async def test_decision_rows_campaign_and_general_only(db_session, world):
    w = world
    mine = await _decision(db_session, w, w.camp, confidence=70,
                           expected={"return_pct": 2.5, "horizon_days": 5},
                           outcome="approvata", reason="trend")
    general = await _decision(db_session, w, None, at=IN_DAY + timedelta(hours=1))
    await _decision(db_session, w, w.hidden)
    await _decision(db_session, w, w.archived)
    await _decision(db_session, w, w.camp, at=IN_DAY - timedelta(hours=1))  # June 9 in Rome
    rows = await export.build_rows(db_session, "decisioni", DAY)
    assert [r["id"] for r in rows] == [mine.id, general.id]
    assert rows[0] == {
        "schema": 1, "ambito": "campagna", "id": mine.id, "ts": "2034-06-09T22:30:00+00:00",
        "agente": w.agent.name, "portafoglio": w.camp.code, "tipo": "strategy",
        "sintesi": "Compro su rottura", "dettagli": {"livello": 101}, "simboli": [w.sym],
        "esito": "approvata", "motivo": "trend", "fiducia": 70,
        "risultato_atteso": {"rendimento_pct": 2.5, "orizzonte_giorni": 5},
    }
    assert rows[1]["portafoglio"] is None and rows[1]["fiducia"] is None
    assert rows[1]["risultato_atteso"] is None and rows[1]["ambito"] == "campagna"


async def test_general_decisions_only_of_exported_agents(db_session, world):
    w = world
    drill = await _agent(db_session, w.hidden)  # a drill-only agent
    tester = await _agent(db_session, w.flagged)  # only on the intraday test portfolio
    await _decision(db_session, w, None, agent=drill)
    mine = await _decision(db_session, w, None, agent=tester)
    rows = await export.build_rows(db_session, "decisioni", DAY)
    assert [(r["id"], r["ambito"]) for r in rows] == [(mine.id, "prova_intraday")]


async def test_the_flagged_test_portfolio_is_exported_tagged(db_session, world):
    w = world
    d = await _decision(db_session, w, w.flagged)
    o = await _order(db_session, w, w.flagged)
    f = await _fill(db_session, o)
    await _decision(db_session, w, w.flagged_archived)
    await _fill(db_session, await _order(db_session, w, w.flagged_archived))
    for kind, want in (("decisioni", d.id), ("ordini", str(o.id)), ("esecuzioni", str(f.id))):
        rows = await export.build_rows(db_session, kind, DAY)
        assert [(r["id"], r["ambito"]) for r in rows] == [(want, "prova_intraday")], kind
    events = await flow_events(db_session, after=None, limit=500,
                               now=IN_DAY + timedelta(minutes=2))
    assert f"fill:{f.id}" in {e["id"] for e in events}


async def test_order_rows_carry_the_decision(db_session, world):
    w = world
    d = await _decision(db_session, w, w.camp)
    o = await _order(db_session, w, w.camp, decision_id=d.id, broker_link_id=w.link.id,
                     broker_sent_at=IN_DAY + timedelta(seconds=2), day_trade=True)
    plain = await _order(db_session, w, w.camp, at=IN_DAY + timedelta(minutes=1),
                         source="system", agent_id=None)
    await _order(db_session, w, w.hidden)
    await _order(db_session, w, w.archived)
    rows = await export.build_rows(db_session, "ordini", DAY)
    assert [r["id"] for r in rows] == [str(o.id), str(plain.id)]
    assert rows[0] == {
        "schema": 1, "ambito": "campagna", "id": str(o.id), "ts": "2034-06-09T22:30:00+00:00",
        "aggiornato": "2034-06-09T22:35:00+00:00", "portafoglio": w.camp.code,
        "broker": "alpaca_paper", "origine": w.agent.name, "simbolo": w.sym, "lato": "buy",
        "tipo": "limit", "durata": "day", "quantita": "3.5", "controvalore": None,
        "prezzo_limite": "101.25", "prezzo_stop": None, "ruolo": None, "padre": None,
        "gruppo_oco": None, "stato": "filled", "codice_rifiuto": None,
        "inviato_al_broker": "2034-06-09T22:30:02+00:00", "giornata": True,
        "decisione": d.id,
    }
    assert rows[1]["decisione"] is None and rows[1]["origine"] == "sistema"
    assert rows[1]["broker"] == "simulato"


async def test_fill_rows_campaign_only(db_session, world):
    w = world
    o = await _order(db_session, w, w.camp)
    f = await _fill(db_session, o)
    await _fill(db_session, await _order(db_session, w, w.hidden))
    await _fill(db_session, await _order(db_session, w, w.archived))
    rows = await export.build_rows(db_session, "esecuzioni", DAY)
    assert rows == [{
        "schema": 1, "ambito": "campagna", "id": str(f.id), "ordine": str(o.id), "ts": "2034-06-09T22:31:00+00:00",
        "portafoglio": w.camp.code, "simbolo": w.sym, "lato": "buy", "quantita": "3.5",
        "prezzo": "101.2", "bid": "101.1", "ask": "101.3", "ultimo": "101.2",
        "ts_quotazione": "2034-06-09T22:30:59+00:00", "commissione": "0.35",
        "valuta_commissione": "USD", "fonte": "broker",
    }]


# --- one send ----------------------------------------------------------------------


async def test_send_day_request_is_exact(db_session, world, ng, http):
    w = world
    await _decision(db_session, w, w.camp)
    await _decision(db_session, w, None)
    result = await sender.send_day(db_session, http, "decisioni", DAY,
                                   settings=_settings(ng.url))
    assert result.status == "confirmed" and result.http_status == 201
    [req] = ng.requests
    assert req["path"] == "/api/dati/smtrading/decisioni/2034-06-10"
    h = {k.lower(): v for k, v in req["headers"].items()}
    assert h["authorization"] == f"Bearer {KEY}"
    assert h["content-type"] == "application/x-ndjson"
    assert h["x-smt-export-id"] == "smtrading:decisioni:2034-06-10:1"
    assert h["x-smt-rows"] == "2"
    lines = req["body"].decode("utf-8").split("\n")
    assert len(lines) == 2 and all(isinstance(json.loads(x), dict) for x in lines)
    row = await db_session.get(NeurographExport, ("decisioni", DAY, 1))
    assert (row.status, row.rows, row.http_status, row.attempts) == ("confirmed", 2, 201, 1)


async def test_send_day_with_a_higher_version(db_session, world, ng, http):
    ng.answer = lambda kind, day: (200, {"esito": "sostituito"})
    result = await sender.send_day(db_session, http, "ordini", DAY, version=3,
                                   settings=_settings(ng.url))
    assert result.status == "confirmed" and "sostituito" in result.message
    h = {k.lower(): v for k, v in ng.requests[0]["headers"].items()}
    assert h["x-smt-export-id"] == "smtrading:ordini:2034-06-10:3" and h["x-smt-rows"] == "0"
    assert ng.requests[0]["body"] == b""


# --- nightly -------------------------------------------------------------------------


def _sent(ng):
    return [(r["day"], r["kind"]) for r in ng.requests]


def _all(*days):
    return [(d, k) for d in days for k in ("decisioni", "ordini", "esecuzioni")]


async def _alerts(db_session) -> list[str]:
    return list(await db_session.scalars(
        select(TelegramOutbox.text).where(TelegramOutbox.text.like("%Neurograph%"))
        .order_by(TelegramOutbox.id)))


async def test_nightly_sends_once_then_nothing(db_session, ng, http):
    await _nightly(db_session, http, night(DAY), ng.url)
    assert _sent(ng) == _all("2034-06-08", "2034-06-09")
    await _nightly(db_session, http, night(DAY, 3), ng.url)  # same night: nothing
    assert len(ng.requests) == 6
    await _nightly(db_session, http, night(DAY + timedelta(days=1)), ng.url)
    assert _sent(ng)[6:] == _all("2034-06-10")  # only the new day


async def test_nightly_waits_for_half_past_midnight(db_session, ng, http):
    await _nightly(db_session, http, datetime(2034, 6, 10, 0, 10, tzinfo=ROME), ng.url)
    assert ng.requests == []


async def test_already_present_counts_as_confirmed(db_session, ng, http):
    ng.answer = lambda kind, day: (200, {"esito": "già presente"})
    await _nightly(db_session, http, night(DAY), ng.url)
    await _nightly(db_session, http, night(DAY + timedelta(days=1)), ng.url)
    assert _sent(ng) == _all("2034-06-08", "2034-06-09", "2034-06-10")
    statuses = set(await db_session.scalars(
        select(NeurographExport.status).where(NeurographExport.day >= date(2034, 6, 8))))
    assert statuses == {"confirmed"}


async def test_a_400_blocks_only_its_file(db_session, ng, http):
    ng.answer = lambda kind, day: (
        (400, {"errore": "riga 3: non è un oggetto JSON"}) if (kind, day) == ("ordini", "2034-06-08")
        else (201, {"esito": "ricevuto"}))
    await _nightly(db_session, http, night(DAY), ng.url)
    assert _sent(ng) == _all("2034-06-08", "2034-06-09")  # the other files went on
    [alert] = await _alerts(db_session)
    assert "ordini" in alert and "08/06/2034" in alert and "riga 3" in alert
    assert "Da fare:" in alert
    row = await db_session.get(NeurographExport, ("ordini", date(2034, 6, 8), 1))
    assert row.status == "rejected" and row.http_status == 400
    await _nightly(db_session, http, night(DAY + timedelta(days=1)), ng.url)
    assert _sent(ng)[6:] == _all("2034-06-10")  # the rejected file is not retried
    assert len(await _alerts(db_session)) == 1


async def test_a_401_stops_the_run_and_alerts_once(db_session, ng, http):
    ng.answer = lambda kind, day: (401, {"errore": "chiave errata"})
    await _nightly(db_session, http, night(DAY), ng.url)
    assert len(ng.requests) == 1
    [alert] = await _alerts(db_session)
    assert "chiave" in alert and "Da fare:" in alert
    ng.answer = lambda kind, day: (201, {"esito": "ricevuto"})
    await _nightly(db_session, http, night(DAY + timedelta(days=1)), ng.url)
    assert _sent(ng)[1:] == _all("2034-06-09", "2034-06-10")


async def test_a_5xx_is_retried_the_next_night(db_session, ng, http):
    ng.answer = lambda kind, day: (500, {"errore": "interno"})
    await _nightly(db_session, http, night(DAY), ng.url)
    assert _sent(ng) == _all("2034-06-08", "2034-06-09")
    assert await _alerts(db_session) == []
    ng.answer = lambda kind, day: (201, {"esito": "ricevuto"})
    await _nightly(db_session, http, night(DAY + timedelta(days=1)), ng.url)
    assert _sent(ng)[6:] == _all("2034-06-09", "2034-06-10")  # 06-08 left the window
    row = await db_session.get(NeurographExport, ("ordini", date(2034, 6, 9), 1))
    assert row.status == "confirmed" and row.attempts == 2


async def test_three_nights_down_then_every_missing_day_in_order(db_session, ng, http,
                                                                monkeypatch):
    monkeypatch.setattr(sender, "BACKFILL_DAYS", 5)
    down = "http://127.0.0.1:9/api/dati/smtrading"  # nothing listens there
    for n in range(3):
        await _nightly(db_session, http, night(DAY + timedelta(days=n)), down)
    await _nightly(db_session, http, night(DAY + timedelta(days=3)), ng.url)
    want = _all(*[(date(2034, 6, 8) + timedelta(days=i)).isoformat() for i in range(5)])
    assert _sent(ng) == want and len(set(want)) == len(want)


async def test_no_key_does_nothing(db_session, ng, http):
    for key in (None, ""):
        assert await _nightly(db_session, http, night(DAY), ng.url, key=key) is None
    assert ng.requests == []
    claims = await db_session.scalars(select(JobRun.key).where(JobRun.job == sender.JOB))
    assert list(claims) == []


async def test_the_key_never_leaves_the_request(db_session, ng, http, caplog):
    caplog.set_level(logging.DEBUG)
    ng.answer = lambda kind, day: (
        (400, {"errore": f"intestazione Bearer {KEY} non valida per {kind}"}) if kind == "decisioni"
        else (503, {"errore": "ricezione non configurata"}))
    await _nightly(db_session, http, night(DAY), ng.url)
    ng.answer = lambda kind, day: (401, {"errore": f"chiave {KEY} errata"})
    await _nightly(db_session, http, night(DAY + timedelta(days=1)), ng.url)
    assert ng.requests  # it did send
    assert KEY not in caplog.text
    for m in await db_session.scalars(select(NeurographExport.message)):
        assert KEY not in (m or "")
    for t in await _alerts(db_session):
        assert KEY not in t
    for k in await db_session.scalars(select(JobRun.key).where(JobRun.job == sender.JOB)):
        assert KEY not in k


async def test_a_changed_day_in_the_window_goes_at_the_next_version(db_session, world, ng,
                                                                    http, monkeypatch):
    monkeypatch.setattr(sender, "BACKFILL_DAYS", 5)
    w = world
    oid = (await _order(db_session, w, w.camp, at=datetime(2034, 6, 8, 12, tzinfo=UTC))).id
    await _nightly(db_session, http, night(DAY), ng.url)
    sent_at = await db_session.scalar(select(NeurographExport.sent_at).where(
        NeurographExport.kind == "ordini", NeurographExport.day == date(2034, 6, 8)))
    await db_session.execute(update(Order).where(Order.id == oid).values(
        status=OrderStatus.filled, updated_at=sent_at + timedelta(microseconds=1)))
    ng.requests.clear()
    ng.answer = lambda kind, day: (200, {"esito": "sostituito"})
    await _nightly(db_session, http, night(DAY + timedelta(days=2)), ng.url)
    ids = [{k.lower(): v for k, v in r["headers"].items()}["x-smt-export-id"]
           for r in ng.requests]
    assert ids == ["smtrading:ordini:2034-06-08:2", *(
        f"smtrading:{k}:{d}:1" for d in ("2034-06-10", "2034-06-11")
        for k in ("decisioni", "ordini", "esecuzioni"))]
    body = json.loads(ng.requests[0]["body"])
    assert body["stato"] == "filled"
    row = await db_session.get(NeurographExport, ("ordini", date(2034, 6, 8), 2))
    assert row.status == "confirmed"
    ng.requests.clear()
    await _nightly(db_session, http, night(DAY + timedelta(days=3)), ng.url)
    assert _sent(ng) == _all("2034-06-12")  # not sent a third time


async def test_a_failed_higher_version_is_retried(db_session, ng, http):
    await _nightly(db_session, http, night(DAY), ng.url)
    ng.answer = lambda kind, day: (500, {"errore": "interno"})
    await sender.send_day(db_session, http, "ordini", date(2034, 6, 9), version=2,
                          settings=_settings(ng.url))
    ng.requests.clear()
    ng.answer = lambda kind, day: (201, {"esito": "ricevuto"})
    await _nightly(db_session, http, night(DAY + timedelta(days=1)), ng.url)
    ids = [{k.lower(): v for k, v in r["headers"].items()}["x-smt-export-id"]
           for r in ng.requests]
    assert ids[0] == "smtrading:ordini:2034-06-09:2"


async def test_an_error_on_one_file_does_not_stop_the_others(db_session, ng, http,
                                                             monkeypatch, caplog):
    real = sender.build_rows

    async def broken(session, kind, day, **kw):
        if kind == "ordini":
            raise RuntimeError("boom")
        return await real(session, kind, day, **kw)

    monkeypatch.setattr(sender, "build_rows", broken)
    counts = await _nightly(db_session, http, night(DAY), ng.url)
    assert _sent(ng) == [(d, k) for d in ("2034-06-08", "2034-06-09")
                         for k in ("decisioni", "esecuzioni")]
    assert counts["failed"] == 2 and "neurograph export of smtrading:ordini" in caplog.text


async def test_outage_alert_on_the_third_night_once_per_streak(db_session, ng, http,
                                                              monkeypatch):
    monkeypatch.setattr(sender, "BACKFILL_DAYS", 5)
    down = "http://127.0.0.1:9/api/dati/smtrading"
    nights = [DAY + timedelta(days=n) for n in range(10)]
    for n in nights[:2]:
        await _nightly(db_session, http, night(n), down)
    assert await _alerts(db_session) == []
    await _nightly(db_session, http, night(nights[2]), down)
    [alert] = await _alerts(db_session)
    assert "da 3 notti" in alert and "Da fare: verificare la ricezione di Neurograph" in alert
    # night 06-12 covers 06-07..06-11: 15 files, the oldest leaves on 06-13
    assert "15 file in attesa" in alert and "esce dal recupero il 13/06" in alert
    await _nightly(db_session, http, night(nights[3]), down)
    assert len(await _alerts(db_session)) == 1  # once per streak
    await _nightly(db_session, http, night(nights[4]), ng.url)  # back: streak reset
    ng.answer = lambda kind, day: (503, {"errore": "ricezione non configurata"})
    for n in nights[5:8]:
        await _nightly(db_session, http, night(n), ng.url)
    alerts = await _alerts(db_session)
    assert len(alerts) == 2 and "ricezione non configurata" in alerts[1]


async def test_a_refused_key_alerts_once_per_streak(db_session, ng, http):
    ng.answer = lambda kind, day: (401, {"errore": "chiave errata"})
    for n in range(3):
        await _nightly(db_session, http, night(DAY + timedelta(days=n)), ng.url)
    assert len(await _alerts(db_session)) == 1
    ng.answer = lambda kind, day: (201, {"esito": "ricevuto"})
    await _nightly(db_session, http, night(DAY + timedelta(days=3)), ng.url)
    ng.answer = lambda kind, day: (401, {"errore": "chiave errata"})
    await _nightly(db_session, http, night(DAY + timedelta(days=4)), ng.url)
    assert len(await _alerts(db_session)) == 2


# --- flow ----------------------------------------------------------------------------


async def test_exports_appear_in_the_flow(db_session, ng, http):
    ng.answer = lambda kind, day: ((400, {"errore": "no"}) if kind == "ordini"
                                   else (201, {"esito": "ricevuto"}))
    await sender.send_day(db_session, http, "decisioni", DAY, settings=_settings(ng.url))
    await sender.send_day(db_session, http, "ordini", DAY, settings=_settings(ng.url))
    sent_at = await db_session.scalar(select(NeurographExport.sent_at).where(
        NeurographExport.kind == "ordini", NeurographExport.day == DAY))
    events = await flow_events(db_session, after=sent_at - timedelta(minutes=1), limit=500,
                               now=sent_at + timedelta(seconds=1))
    mine = {e["id"]: e for e in events if e["kind"] == "export"}
    ok, ko = mine["export:decisioni:2034-06-10:1"], mine["export:ordini:2034-06-10:1"]
    assert ok["path"] == ["db", "ng"] and ok["hops"] == ["esportazione decisioni"]
    assert ok["outcome"] == "ok" and ok["who"] == "db"
    assert ko["hops"] == ["esportazione ordini"] and ko["outcome"] == "error"


# --- one-day command and worker job ------------------------------------------------------


async def test_cli_sends_one_day_with_a_version(db_session, ng, http, capsys):
    from smtrading import cli

    await _nightly(db_session, http, night(DAY + timedelta(days=1)), ng.url)  # v1 confirmed
    ng.requests.clear()
    args = cli.build_parser().parse_args(
        ["neurograph-export", "--day", "2034-06-10", "--type", "ordini", "--version", "2"])
    code, out = await cli.run_neurograph_export(args, _SM(db_session), http,
                                                settings=_settings(ng.url))
    assert code == 0 and KEY not in out
    assert json.loads(out) == {"tipo": "ordini", "giorno": "2034-06-10", "versione": 2,
                               "esito": "confirmed", "http": 201, "righe": 0,
                               "messaggio": "ricevuto"}
    h = {k.lower(): v for k, v in ng.requests[0]["headers"].items()}
    assert h["x-smt-export-id"] == "smtrading:ordini:2034-06-10:2"
    args = cli.build_parser().parse_args(["neurograph-export", "--day", "2034-06-10"])
    code, out = await cli.run_neurograph_export(args, _SM(db_session), http,
                                                settings=_settings(ng.url))
    assert code == 0 and [json.loads(x)["tipo"] for x in out.split("\n")] == [
        "decisioni", "ordini", "esecuzioni"]
    code, out = await cli.run_neurograph_export(args, _SM(db_session), http,
                                                settings=_settings(ng.url, key=None))
    assert code == 1 and "SMT_NEUROGRAPH_EXPORT_KEY" in out


def test_cli_refuses_a_bad_day_or_version():
    from smtrading import cli

    for argv in (["--day", "10/06/2034"], ["--day", "2034-06-10", "--version", "0"],
                 ["--day", "2034-06-10", "--type", "posizioni"]):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["neurograph-export", *argv])


async def test_worker_job_runs_the_night_and_survives_errors(db_session, ng, http, caplog):
    from smtrading.neurograph.job import NeurographExportJob

    job = NeurographExportJob(_SM(db_session), http, clock=lambda: night(DAY),
                              settings=_settings(ng.url))
    assert (await job.tick())["confirmed"] == 6
    assert await job.tick() is None  # tonight already ran

    class Broken:
        def __call__(self):
            raise RuntimeError("db down")

    broken = NeurographExportJob(Broken(), http, clock=lambda: night(DAY + timedelta(days=1)),
                                 settings=_settings(ng.url))
    assert await broken.tick() is None
    assert "neurograph export failed" in caplog.text
