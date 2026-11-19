"""«Strategie: previsto contro reale»: per strategy, what its backtest promised when the CEO
approved it against what it did live since then. Read-only, for the portal.

Approval: the CEO's `strategy` decisions by ref (`details.ref` / `details.strategy_id`, the
statuses of `signals.service`): an `approvata` after no approval (or after a sospesa /
revocata / respinta) starts a new approval; a `modificata` merges its non-empty details
over it. The latest approval of every ref ever approved is compared (a strategy sospesa
since keeps its comparison); `approved_at` is when it started.

Expected (each field from the first source that has it):
1. the approval's `details.metrics` (the CEO copies the Quant's: total_return_pct,
   max_drawdown_pct, sharpe, win_rate_pct, trades);
2. the latest Quant registration of the ref (`other` decision with `details.strategy`,
   its `details.metrics`, period `from_date` / `to_date`);
3. the backtest of `details.backtest_id` (else the Quant's `backtest_ids.dev` /
   `backtest_id`): `result.metrics`, its period (start → end) and the average return per
   trade (mean of pnl / (entry price × qty) of its trades, %).
`max_drawdown_pct` is a positive number (the decisions write it negative);
`trades_per_month` = trades over the backtest period (30.44-day months).

Live (the «Diario operazioni» trades of the ref, every portfolio, opened since
`approved_at`): `trades` (opened), `closed_trades`, `win_rate_pct` and `avg_trade_pct`
(mean `pnl_pct`, net of costs) over the closed ones, `pnl_eur`, `max_drawdown_pct` of the
strategy's realised equity (100 compounded by each closed trade's pnl_pct, in closing
order; peak-to-trough %), `max_drawdown_eur` (peak-to-trough of the cumulated net P/L),
`trades_per_month` (opened over the months since approval, at least one day).

Verdict: `dati insufficienti` with fewer than MIN_TRADES closed trades or no expected
metric to compare; else `sotto le attese` when any of: win rate more than
WIN_RATE_GAP_PTS points under the expected, average per trade under the expected by more
than max(0.5, half its size) points, drawdown more than DD_FACTOR × the expected; else
`in linea`. `reasons` says why, in Italian.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision, DecisionKind
from smtrading.backtest.models import Backtest
from smtrading.engine.quotesource import QuoteSource
from smtrading.performance.journal import TradeRow, trade_rows
from smtrading.signals.service import (
    APPROVALS,
    MODIFIED,
    STATUS_DECISIONS,
    _ref,
    _status,
    _text,
    _uuid,
    strategy_states,
)

MIN_TRADES = 5
WIN_RATE_GAP_PTS = Decimal(15)
DD_FACTOR = Decimal("1.5")
IN_LINE, BELOW, INSUFFICIENT = "in linea", "sotto le attese", "dati insufficienti"
_MONTH_DAYS = Decimal("30.4375")
_Q = Decimal("0.01")
_PCT = Decimal("0.1")


def _dec(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        d = Decimal(str(v).replace(",", ".").replace("%", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _day(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return datetime.fromisoformat(v.strip()[:10]).date()
        except ValueError:
            return None
    return None


def _it(v: Decimal, places: int = 1) -> str:
    return f"{v:.{places}f}".replace(".", ",")


@dataclass
class Expected:
    source: str | None = None  # approvazione | quant | backtest (the first used)
    backtest_id: uuid.UUID | None = None
    from_date: date | None = None
    to_date: date | None = None
    trades: int | None = None
    trades_per_month: Decimal | None = None
    win_rate_pct: Decimal | None = None
    avg_trade_pct: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    sharpe: Decimal | None = None
    total_return_pct: Decimal | None = None


@dataclass
class Live:
    since: datetime
    trades: int = 0
    open_trades: int = 0
    closed_trades: int = 0
    wins: int = 0
    win_rate_pct: Decimal | None = None
    avg_trade_pct: Decimal | None = None
    pnl_eur: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    max_drawdown_eur: Decimal | None = None
    trades_per_month: Decimal | None = None
    days: int = 0


@dataclass
class StrategyComparison:
    ref: str
    name: str
    status: str
    approved_at: datetime
    expected: Expected
    live: Live
    verdict: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class _Approval:
    ref: str
    at: datetime
    details: dict


async def _approvals(session: AsyncSession) -> dict[str, _Approval]:
    out: dict[str, _Approval] = {}
    current: dict[str, str] = {}
    for d in await session.scalars(
            select(Decision).where(Decision.kind == DecisionKind.strategy)
            .order_by(Decision.id)):
        ref, status = _ref(d), _status(d)
        if ref is None or status not in STATUS_DECISIONS:
            continue
        details = d.details or {}
        if status in APPROVALS:
            if status == MODIFIED and current.get(ref) in APPROVALS and ref in out:
                out[ref].details = {**out[ref].details, **{
                    k: v for k, v in details.items() if v not in (None, "", [], {})}}
            else:
                out[ref] = _Approval(ref, d.created_at, dict(details))
        current[ref] = status
    return out


async def _quant(session: AsyncSession, refs: list[str]) -> dict[str, dict]:
    if not refs:
        return {}
    out: dict[str, dict] = {}
    for d in await session.scalars(
            select(Decision).where(
                Decision.kind == DecisionKind.other,
                Decision.details["ref"].astext.in_(refs)
                | Decision.details["strategy_id"].astext.in_(refs),
            ).order_by(Decision.id)):
        det = d.details or {}
        if not _text(det.get("strategy")):
            continue
        for key in (_text(det.get("ref")), _text(det.get("strategy_id"))):
            if key in refs:
                out[key] = det
    return out


def _backtest_id(*details: dict) -> uuid.UUID | None:
    for det in details:
        for v in (det.get("backtest_id"),
                  (det.get("backtest_ids") or {}).get("dev")
                  if isinstance(det.get("backtest_ids"), dict) else None):
            if (u := _uuid(v)) is not None:
                return u
    return None


def _avg_trade_pct(result: dict) -> Decimal | None:
    pcts = []
    for t in result.get("trades") or []:
        if not isinstance(t, dict):
            continue
        pnl, price, qty = _dec(t.get("pnl")), _dec(t.get("entry_price")), _dec(t.get("qty"))
        if pnl is None or not price or not qty:
            continue
        pcts.append(pnl / (price * qty) * 100)
    return (sum(pcts) / len(pcts)).quantize(_Q, ROUND_HALF_UP) if pcts else None


async def _expected(session: AsyncSession, approval: _Approval, quant: dict | None
                    ) -> Expected:
    det = approval.details
    q = quant or {}
    e = Expected()
    sources: list[tuple[str, dict]] = []
    if isinstance(det.get("metrics"), dict):
        sources.append(("approvazione", det["metrics"]))
    if isinstance(q.get("metrics"), dict):
        sources.append(("quant", q["metrics"]))
    e.backtest_id = _backtest_id(det, q)
    bt = await session.get(Backtest, e.backtest_id) if e.backtest_id else None
    result = bt.result if bt is not None and isinstance(bt.result, dict) else {}
    if isinstance(result.get("metrics"), dict):
        sources.append(("backtest", result["metrics"]))
    used: list[str] = []

    def pick(name: str) -> Decimal | None:
        for src, metrics in sources:
            v = _dec(metrics.get(name))
            if v is not None:
                used.append(src)
                return v
        return None

    e.win_rate_pct = pick("win_rate_pct")
    e.max_drawdown_pct = abs(v) if (v := pick("max_drawdown_pct")) is not None else None
    e.sharpe = pick("sharpe")
    e.total_return_pct = pick("total_return_pct")
    trades = pick("trades")
    e.trades = int(trades) if trades is not None else None
    e.avg_trade_pct = pick("avg_trade_pct")
    if e.avg_trade_pct is None and result:
        e.avg_trade_pct = _avg_trade_pct(result)
        if e.avg_trade_pct is not None:
            used.append("backtest")
    if bt is not None:
        e.from_date, e.to_date = bt.start.date(), bt.end.date()
    else:
        e.from_date = _day(q.get("from_date")) or _day(det.get("from_date"))
        e.to_date = _day(q.get("to_date")) or _day(det.get("to_date"))
    if e.trades is not None and e.from_date and e.to_date and e.to_date > e.from_date:
        months = Decimal((e.to_date - e.from_date).days) / _MONTH_DAYS
        e.trades_per_month = (Decimal(e.trades) / months).quantize(_Q, ROUND_HALF_UP)
    e.source = used[0] if used else None
    return e


def _live(rows: list[TradeRow], since: datetime, now: datetime) -> Live:
    live = Live(since=since)
    live.trades = len(rows)
    closed = sorted((r for r in rows if r.outcome != "open"), key=lambda r: r.closed_at)
    live.closed_trades, live.open_trades = len(closed), len(rows) - len(closed)
    live.wins = sum(1 for r in closed if r.outcome == "win")
    days = max(Decimal((now - since).total_seconds()) / 86400, Decimal(1))
    live.days = int(days)
    live.trades_per_month = (Decimal(live.trades) / (days / _MONTH_DAYS)).quantize(
        _Q, ROUND_HALF_UP)
    if not closed:
        return live
    live.win_rate_pct = (Decimal(live.wins) * 100 / len(closed)).quantize(_PCT, ROUND_HALF_UP)
    pcts = [r.pnl_pct for r in closed]
    pnls = [r.pnl_eur for r in closed]
    if None not in pcts:
        live.avg_trade_pct = (sum(pcts) / len(pcts)).quantize(_Q, ROUND_HALF_UP)
        equity, peak, worst = Decimal(100), Decimal(100), Decimal(0)
        for p in pcts:
            equity *= 1 + p / 100
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak * 100 if peak > 0 else Decimal(0))
        live.max_drawdown_pct = worst.quantize(_Q, ROUND_HALF_UP)
    if None not in pnls:
        live.pnl_eur = sum(pnls, Decimal(0)).quantize(_Q, ROUND_HALF_UP)
        cum, peak, worst = Decimal(0), Decimal(0), Decimal(0)
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            worst = max(worst, peak - cum)
        live.max_drawdown_eur = worst.quantize(_Q, ROUND_HALF_UP)
    return live


def verdict(e: Expected, live: Live) -> tuple[str, list[str]]:
    comparable = [v for v in (e.win_rate_pct, e.avg_trade_pct, e.max_drawdown_pct)
                  if v is not None]
    if not comparable:
        return INSUFFICIENT, ["metriche del backtest non registrate nell'approvazione"]
    if live.closed_trades < MIN_TRADES:
        why = f"{live.closed_trades} operazioni chiuse: ne servono almeno {MIN_TRADES}"
        return INSUFFICIENT, [why]
    reasons = []
    if (e.win_rate_pct is not None and live.win_rate_pct is not None
            and live.win_rate_pct < e.win_rate_pct - WIN_RATE_GAP_PTS):
        reasons.append(f"vincenti {_it(live.win_rate_pct)}% contro {_it(e.win_rate_pct)}% "
                       "attese")
    if e.avg_trade_pct is not None and live.avg_trade_pct is not None:
        gap = max(Decimal("0.5"), abs(e.avg_trade_pct) / 2)
        if live.avg_trade_pct < e.avg_trade_pct - gap:
            reasons.append(f"media per operazione {_it(live.avg_trade_pct, 2)}% contro "
                           f"{_it(e.avg_trade_pct, 2)}% attesa")
    if (e.max_drawdown_pct and live.max_drawdown_pct is not None
            and live.max_drawdown_pct > e.max_drawdown_pct * DD_FACTOR):
        reasons.append(f"drawdown {_it(live.max_drawdown_pct)}% contro "
                       f"{_it(e.max_drawdown_pct)}% del backtest")
    return (BELOW, reasons) if reasons else (IN_LINE, [])


async def strategy_expectations(
    session: AsyncSession, qs: QuoteSource, *, redis=None, now: datetime | None = None,
    rows: list[TradeRow] | None = None,
) -> list[StrategyComparison]:
    """One comparison per ref ever approved, by ref number."""
    now = now or datetime.now(UTC)
    approvals = await _approvals(session)
    if not approvals:
        return []
    quant = await _quant(session, list(approvals))
    states = await strategy_states(session)
    if rows is None:
        rows = await trade_rows(session, qs, redis=redis, now=now)
    out = []
    for ref, a in approvals.items():
        expected = await _expected(session, a, quant.get(ref))
        mine = [r for r in rows if r.strategy_ref == ref and r.opened_at >= a.at]
        live = _live(mine, a.at, now)
        v, reasons = verdict(expected, live)
        state = states.get(ref)
        out.append(StrategyComparison(
            ref=ref, name=state.name if state else ref,
            status=state.status if state else "approvata", approved_at=a.at,
            expected=expected, live=live, verdict=v, reasons=reasons,
        ))
    out.sort(key=lambda c: int(c.ref[4:]) if c.ref[4:].isdigit() else 0)
    return out
