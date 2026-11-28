"""The rows of the nightly export to Neurograph (schema v1, Italian field names as agreed
with Neurograph, plus `decisione` on the orders).

One file per type and Rome day: `decisioni` (decisions registered that day), `ordini`
(orders created that day, with their state at export time), `esecuzioni` (fills of that
day). Only the campaign's portfolios (`total_portfolio_ids`: hidden test and archived
portfolios left out); a decision without a portfolio (a general one) is kept. No secret
and no Telegram text; amounts as decimal strings, times ISO 8601 UTC.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision
from smtrading.agents.models import Agent
from smtrading.brokers.models import BrokerLink
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import Fill, Order
from smtrading.portfolios.models import Portfolio, total_portfolio_ids

Kind = Literal["decisioni", "ordini", "esecuzioni"]
KINDS: tuple[Kind, ...] = ("decisioni", "ordini", "esecuzioni")
SCHEMA = 1
ROME = "Europe/Rome"  # the day of a file is the Rome day (contract with Neurograph)
AUTO_PREFIX = "smt-auto"  # client_order_id of an automatic rule's orders
SIMULATED = "simulato"
ORIGIN_AUTO = "regola automatica"
ORIGIN_SYSTEM = "sistema"
ORIGIN_ADMIN = "amministratore"
ORIGIN_AGENT = "agente"  # an agent's order whose agent is not found


def day_bounds(day: date, tz: str | None = None) -> tuple[datetime, datetime]:
    """[start, end) of the Rome day `day` (or of `tz`), in UTC."""
    zone = ZoneInfo(tz or ROME)
    start = datetime.combine(day, time(0), zone)
    end = datetime.combine(day + timedelta(days=1), time(0), zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def _ts(v: datetime | None) -> str | None:
    return v.astimezone(UTC).isoformat() if v is not None else None


def _num(v: Decimal | None) -> str | None:
    return decimal_to_str(v) if v is not None else None


def _str(v: Any) -> str | None:
    return str(v) if v is not None else None


def _val(v: Any) -> Any:
    return getattr(v, "value", v)


def _expected(e: dict | None) -> dict | None:
    if not isinstance(e, dict):
        return None
    return {"rendimento_pct": e.get("return_pct"), "orizzonte_giorni": e.get("horizon_days")}


def _portfolio(code: str | None, name: str | None) -> str | None:
    return code or name


async def _decisions(session: AsyncSession, start: datetime, end: datetime) -> list[dict]:
    q = (select(Decision, Agent.name, Portfolio.code, Portfolio.name.label("pname"))
         .join(Agent, Agent.id == Decision.agent_id)
         .outerjoin(Portfolio, Portfolio.id == Decision.portfolio_id)
         .where(Decision.created_at >= start, Decision.created_at < end,
                or_(Decision.portfolio_id.is_(None),
                    Decision.portfolio_id.in_(total_portfolio_ids())))
         .order_by(Decision.created_at, Decision.id))
    out = []
    for d, agent, code, pname in (await session.execute(q)).all():
        out.append({
            "schema": SCHEMA, "id": d.id, "ts": _ts(d.created_at), "agente": agent,
            "portafoglio": _portfolio(code, pname) if d.portfolio_id else None,
            "tipo": d.kind, "sintesi": d.summary, "dettagli": d.details,
            "simboli": d.symbols, "esito": d.outcome, "motivo": d.reason,
            "fiducia": d.confidence, "risultato_atteso": _expected(d.expected),
        })
    return out


def _origin(o: Order, agent: str | None) -> str:
    if o.auto_rule_id is not None or (o.client_order_id or "").startswith(AUTO_PREFIX):
        return ORIGIN_AUTO
    if o.source == "agent":
        return agent or ORIGIN_AGENT
    return ORIGIN_ADMIN if o.source == "admin" else ORIGIN_SYSTEM


async def _orders(session: AsyncSession, start: datetime, end: datetime) -> list[dict]:
    q = (select(Order, Portfolio.code, Portfolio.name, BrokerLink.kind, Agent.name)
         .join(Portfolio, Portfolio.id == Order.portfolio_id)
         .outerjoin(BrokerLink, BrokerLink.id == Order.broker_link_id)
         .outerjoin(Agent, cast(Agent.id, String) == Order.agent_id)
         .where(Order.created_at >= start, Order.created_at < end,
                Order.portfolio_id.in_(total_portfolio_ids()))
         .order_by(Order.created_at, Order.id))
    out = []
    for o, code, pname, link_kind, agent in (await session.execute(q)).all():
        out.append({
            "schema": SCHEMA, "id": str(o.id), "ts": _ts(o.created_at),
            "aggiornato": _ts(o.updated_at), "portafoglio": _portfolio(code, pname),
            "broker": link_kind or SIMULATED, "origine": _origin(o, agent),
            "simbolo": o.symbol, "lato": _val(o.side), "tipo": _val(o.type),
            "durata": _val(o.tif), "quantita": _num(o.qty), "controvalore": _num(o.notional),
            "prezzo_limite": _num(o.limit_price), "prezzo_stop": _num(o.stop_price),
            "ruolo": o.role, "padre": _str(o.parent_id), "gruppo_oco": _str(o.oco_group_id),
            "stato": _val(o.status), "codice_rifiuto": o.reject_code,
            "inviato_al_broker": _ts(o.broker_sent_at), "giornata": bool(o.day_trade),
            "decisione": o.decision_id,
        })
    return out


async def _fills(session: AsyncSession, start: datetime, end: datetime) -> list[dict]:
    q = (select(Fill, Portfolio.code, Portfolio.name)
         .join(Portfolio, Portfolio.id == Fill.portfolio_id)
         .where(Fill.created_at >= start, Fill.created_at < end,
                Fill.portfolio_id.in_(total_portfolio_ids()))
         .order_by(Fill.created_at, Fill.id))
    out = []
    for f, code, pname in (await session.execute(q)).all():
        out.append({
            "schema": SCHEMA, "id": str(f.id), "ordine": str(f.order_id), "ts": _ts(f.created_at),
            "portafoglio": _portfolio(code, pname), "simbolo": f.symbol, "lato": _val(f.side),
            "quantita": _num(f.qty), "prezzo": _num(f.price), "bid": _num(f.quote_bid),
            "ask": _num(f.quote_ask), "ultimo": _num(f.quote_last),
            "ts_quotazione": _ts(f.quote_ts), "commissione": _num(f.fee_amount),
            "valuta_commissione": f.fee_currency,
            "fonte": "broker" if f.source == "broker" else "simulazione",
        })
    return out


_BUILDERS = {"decisioni": _decisions, "ordini": _orders, "esecuzioni": _fills}


async def build_rows(session: AsyncSession, kind: Kind, day: date, *,
                     tz: str | None = None) -> list[dict]:
    """The rows of file `kind` for the Rome day `day`, oldest first."""
    if kind not in _BUILDERS:
        raise ValueError(f"unknown export type {kind!r}")
    start, end = day_bounds(day, tz)
    return await _BUILDERS[kind](session, start, end)
