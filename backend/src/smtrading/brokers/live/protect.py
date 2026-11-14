"""Common protections of a real-money portfolio: the automatic reduce-only, the critical
alerts and the Telegram activity of every real order.

Every `live.*` activity is `direct`: the Telegram planner always queues it at once (never
in the night digest, never skipped), see `telegram.planner`.
"""

import logging
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.mapping import ActivityDraft
from smtrading.activity.models import Activity, ActivityLevel
from smtrading.activity.service import record_activity
from smtrading.brokers.base import BrokerOrder
from smtrading.brokers.live.audit import _redact, record_live_audit
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import Order
from smtrading.events import json_safe
from smtrading.instruments.models import Instrument
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import PortfolioStatus
from smtrading.portfolios.service import LIVE_REASON_PREFIX, is_live_reason
from smtrading.telegram.render import Names, clean_text

log = logging.getLogger(__name__)

TRIP_CAUSES = frozenset({
    "reconcile_diff", "order_unknown", "fingerprint_changed", "cash_shortfall", "fuse",
    "health", "margin_account",
})
ALERT_REPEAT_AFTER = timedelta(hours=1)  # one alert per cause per hour on a blocked portfolio
REDUCE_ONLY_KIND = "reduce_only"

CAUSE_TEXT = {
    "reconcile_diff": "posizioni diverse da quelle del broker",
    "order_unknown": "esito sconosciuto di un ordine",
    "fingerprint_changed": "conto cambiato",
    "cash_shortfall": "liquidità al broker inferiore a quella del portafoglio",
    "fuse": "fusibile: troppi ordini reali nell'ultima ora",
    "health": "broker non raggiungibile",
    "margin_account": "il conto Alpaca non è più un conto cash (margine attivo)",
}

Phase = Literal["sent", "filled", "partial", "rejected", "canceled", "dry_run"]
PHASE_HEAD = {
    "sent": "🔴 REALE · inviato",
    "filled": "🔴 REALE · eseguito",
    "partial": "🔴 REALE · parziale",
    "rejected": "🔴 REALE · rifiutato",
    "canceled": "🔴 REALE · annullato",
    "dry_run": "🧪 REALE (prova)",
}
ALERT_TITLES = {
    "reduce_only": "reduce-only automatico",
    "armed": "denaro reale attivato",
    "disarmed": "denaro reale disattivato",
    "account_changed": "conto cambiato",
    "unreachable": "broker non raggiungibile",
    "flatten": "chiusura di tutte le posizioni reali",
    "kill_switch": "kill switch",
    "order_unknown": "esito sconosciuto",
    "reconcile_diff": "riconciliazione con differenze",
    "fuse": "fusibile di frequenza",
    "credentials_rejected": "credenziali rifiutate",
    "monitor_failed": "controllo del conto reale non riuscito",
    "reconcile_unexplained": "riconciliazione non verificabile",
}
TYPE_LABELS = {
    "market": "mercato", "limit": "limite", "stop": "stop", "stop_limit": "stop limite",
    "trailing_stop": "trailing stop",
}
MAX_MESSAGE = 300


# -- rendering (pure) ----------------------------------------------------------------

def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def it_number(value: Any) -> str:
    """0.5 -> '0,5' (Italian decimal comma, no trailing zeros); '' when not a number."""
    d = _dec(value)
    return "" if d is None else decimal_to_str(d).replace(".", ",")


def _money(value: Any, ccy: str | None) -> str:
    text = it_number(value)
    return f"{text} {ccy}".strip() if text and ccy else text


def _order_line(d: dict) -> str:
    side = str(d.get("side") or "?")
    symbol = str(d.get("symbol") or "?")
    ccy = d.get("currency") or None
    if _dec(d.get("qty")) is not None:
        what = f"{side} {it_number(d['qty'])} {symbol}"
    elif _dec(d.get("notional")) is not None:
        what = f"{side} {symbol} per {_money(d['notional'], ccy)}"
    else:
        what = f"{side} {symbol}"
    kind = str(d.get("type") or "")
    label = TYPE_LABELS.get(kind, kind)
    if kind in ("stop", "stop_limit") and _dec(d.get("stop_price")) is not None:
        label = f"stop {it_number(d['stop_price'])}"
        if kind == "stop_limit" and _dec(d.get("limit_price")) is not None:
            label += f" limite {it_number(d['limit_price'])}"
    elif kind == "limit" and _dec(d.get("limit_price")) is not None:
        label = f"limite {it_number(d['limit_price'])}"
    return f"{what} · {label}" if label else what


def _who(a: Activity, names: Names) -> str | None:
    agent = names.agents.get(a.agent_id) if a.agent_id else None
    if agent:
        return f"Agente: {agent}"
    source = (a.data or {}).get("source")
    if source == "admin":
        return "Origine: admin"
    if source == "system":
        return "Origine: SMTrading"
    return None


def render_live_activity(a: Activity, names: Names, tz: str) -> str:
    """The Telegram text of a `live.*` activity. Orders (`live.order.<phase>`): the phase
    line, the portfolio, `buy 0,5 AAPL · mercato`, agent and time; fills add price, value
    and fees; rejections and cancellations the broker's message. Alerts (`live.<kind>`):
    `🔴 REALE · <title>`, the portfolio, the summary and the time."""
    data = a.data if isinstance(a.data, dict) else {}
    portfolio = names.portfolios.get(a.portfolio_id) if a.portfolio_id else None
    when = a.ts.astimezone(ZoneInfo(tz)).strftime("%H:%M") if a.ts is not None else None
    lines: list[str] = []
    if a.type.startswith("live.order."):
        phase = a.type.removeprefix("live.order.")
        lines.append(PHASE_HEAD.get(phase, f"🔴 REALE · {phase}"))
        if portfolio:
            lines.append(portfolio)
        lines.append(_order_line(data))
        ccy = data.get("currency") or None
        if phase in ("filled", "partial"):
            parts = []
            if phase == "partial" and _dec(data.get("filled_qty")) is not None:
                parts.append(f"Eseguito {it_number(data['filled_qty'])}")
            if _dec(data.get("price")) is not None:
                parts.append(f"Prezzo {_money(data['price'], ccy)}")
            if _dec(data.get("value")) is not None:
                parts.append(f"valore {_money(data['value'], ccy)}")
            if _dec(data.get("fees")) is not None:
                parts.append(f"commissioni {_money(data['fees'], data.get('fees_currency'))}")
            if parts:
                lines.append(" · ".join(parts))
        if phase == "dry_run":
            lines.append("Ordine non inviato (prova a secco)")
        message = clean_text(str(data.get("message") or ""))[:MAX_MESSAGE]
        if message and phase in ("rejected", "canceled", "dry_run"):
            lines.append(f"Messaggio: {message}")
    else:
        kind = a.type.removeprefix("live.")
        lines.append(f"🔴 REALE · {ALERT_TITLES.get(kind, kind)}")
        if portfolio:
            lines.append(portfolio)
        if a.summary:
            lines.append(clean_text(a.summary))
    tail = [t for t in (_who(a, names), when) if t]
    if tail:
        lines.append(" · ".join(tail))
    return "\n".join(lines)


# -- activities ----------------------------------------------------------------------

async def live_alert(session: AsyncSession, *, kind: str, summary: str,
                     portfolio_id: uuid.UUID | None, link_id: uuid.UUID | None,
                     data: dict) -> None:
    """A critical `live.<kind>` activity, sent to Telegram at once (day or night)."""
    record_activity(
        session,
        ActivityDraft(f"live.{kind}", ActivityLevel.critical, True, summary,
                      {**data, "link_id": link_id, "portfolio_id": portfolio_id}),
        portfolio_id=portfolio_id, agent_id=None,
    )
    await session.flush()


async def record_live_order_activity(
    session: AsyncSession, order: Order, phase: Phase, *, bo: BrokerOrder | None = None,
    message: str | None = None, payload: dict | None = None,
) -> None:
    """The `live.order.<phase>` activity of a real order (one Telegram message each).
    `payload`: the order request body (a dry run), secret-named keys masked."""
    currency = await session.scalar(
        select(Instrument.currency).where(Instrument.symbol == order.symbol))
    filled = bo.filled_qty if bo is not None else order.filled_qty
    price = (bo.avg_fill_price if bo is not None else None) or order.avg_fill_price
    value = price * filled if price is not None and filled else None
    if message is None:
        message = (bo.message if bo is not None else None) or order.reject_message
    data = {
        "order_id": order.id, "link_id": order.broker_link_id, "phase": phase,
        "symbol": order.symbol, "side": order.side, "qty": order.qty,
        "notional": order.notional, "type": order.type, "limit_price": order.limit_price,
        "stop_price": order.stop_price, "currency": currency, "source": order.source,
        "agent_id": order.agent_id, "filled_qty": filled, "price": price, "value": value,
        "fees": bo.fees if bo is not None else None, "message": message,
        "live_flatten": order.live_flatten,
    }
    if payload is not None:
        data["payload"] = _redact(json_safe(payload))
    if phase == "partial":
        data["fill_id"] = str(filled)  # each partial fill is its own fact
    record_activity(
        session,
        ActivityDraft(f"live.order.{phase}",
                      ActivityLevel.warning if phase == "rejected" else ActivityLevel.info,
                      True, order_summary(data, phase), data),
        portfolio_id=order.portfolio_id, agent_id=order.agent_id,
    )
    await session.flush()


def order_summary(data: dict, phase: str) -> str:
    head = PHASE_HEAD.get(phase, phase).split(" · ")[-1]
    return f"Ordine reale {head}: {_order_line(data)}"


# -- automatic reduce-only -----------------------------------------------------------

async def _alerted_recently(session: AsyncSession, portfolio_id: uuid.UUID,
                            cause: str) -> bool:
    since = await session.scalar(select(func.now() - ALERT_REPEAT_AFTER))
    found = await session.scalar(
        select(Activity.id).where(
            Activity.type == f"live.{REDUCE_ONLY_KIND}",
            Activity.portfolio_id == portfolio_id,
            Activity.data["cause"].astext == cause,
            Activity.ts > since,
        ).limit(1)
    )
    return found is not None


async def trip_reduce_only(session: AsyncSession, portfolio_id: uuid.UUID, *, cause: str,
                           details: dict, actor: str = "system", alert: bool = True) -> bool:
    """Puts a real-money portfolio in reduce-only for `cause` (an anomaly): only
    `resume_live_portfolio` (TOTP) makes it active again.

    - active: reduce_only with reason `live_<cause>` and no end; returns True;
    - reduce_only for another reason (e.g. a daily loss ending at midnight): the reason
      becomes `live_<cause>` and the end is dropped, so no existing path (the midnight
      reset, a PATCH) can clear it; returns True;
    - already reduce_only for a live cause, or locked (the reason becomes live too, the
      status stays locked): returns False.

    The critical alert is sent when the status changes and otherwise once per cause per
    hour; each alert is recorded in live_audit (`reduce_only_auto`). `alert=False`: the
    caller sends its own single alert (an account change); the audit is still written."""
    if cause not in TRIP_CAUSES:
        raise ValueError(f"unknown live reduce-only cause: {cause}")
    portfolio = await lock_portfolio(session, portfolio_id)
    reason = f"{LIVE_REASON_PREFIX}{cause}"
    changed = False
    if portfolio.status == PortfolioStatus.active or (
        portfolio.status == PortfolioStatus.reduce_only
        and not is_live_reason(portfolio.reduce_only_reason)
    ):
        portfolio.status = PortfolioStatus.reduce_only
        portfolio.reduce_only_reason = reason
        portfolio.reduce_only_until = None
        changed = True
    elif (portfolio.status == PortfolioStatus.locked
          and not is_live_reason(portfolio.reduce_only_reason)):
        portfolio.reduce_only_reason = reason  # unlocking it now needs the TOTP resume
        portfolio.reduce_only_until = None
    await session.flush()
    if alert and not changed and await _alerted_recently(session, portfolio_id, cause):
        return False
    text = CAUSE_TEXT[cause]
    summary = (f"Portafoglio in reduce-only: {text}. Verifica e riprendi con TOTP"
               if changed else f"Anomalia sul portafoglio reale: {text}")
    if portfolio.broker_link_id is None:
        log.warning("live reduce-only on %s (%s): portfolio without a link, no live audit",
                    portfolio_id, cause)
    else:
        await record_live_audit(
            session, action="reduce_only_auto", link_id=portfolio.broker_link_id,
            actor=actor, portfolio_id=portfolio_id,
            details={"cause": cause, "changed": changed, "status": portfolio.status,
                     **details},
        )
    if alert:
        await live_alert(
            session, kind=REDUCE_ONLY_KIND, summary=summary, portfolio_id=portfolio_id,
            link_id=portfolio.broker_link_id,
            data={"cause": cause, "changed": changed, "details": details},
        )
    log.warning("live portfolio %s: reduce-only (%s), changed=%s", portfolio_id, cause,
                changed)
    return changed
