"""How close each approved strategy is to an entry: per symbol the indicator on the last
closed bar and «se chiudesse ora» (the forming bar of `marketdata.forming`), the entry
threshold, the distance to it and whether the backtester's entry rule fires.

Source of truth: the `strategy` decisions (the CEO's), grouped by `details.ref` (or
`details.strategy_id`). Only a decision whose `details.status` (else the `outcome`
column) is approvata / modificata / respinta / sospesa / revocata replaces the previous
one of its ref (by id); others (informativa, no status) are ignored. The strategy counts
when the latest of those is approvata or modificata: a modificata keeps what the
previous approval said and overrides it with its own fields (params...); a later
sospesa / revocata / respinta removes it. Missing fields come from the backtest of
`details.backtest_id`, then from the latest Quant registration (`other` decision) of the
same ref (`details.ref` or `details.strategy_id`):
- type: `details.strategy` | backtest.strategy | quant.strategy
- params: `details.params` | backtest.params | quant.params
- timeframe: `details.timeframe` | backtest.timeframe | quant.timeframe
- symbols: the decision's symbols | backtest.symbols
- portfolios: the decision's portfolio_id plus the portfolio ids in `details.portfolios`
  (codes like "PRU" are not ids and are ignored) | backtest.portfolio_id; archived
  portfolios are left out (a strategy only on archived portfolios disappears).
Nothing is guessed: a missing field lists the strategy as "dati incompleti" (no symbols;
without any portfolio, under portfolio None). Types without a long entry rule here
(buy_and_hold, unknown ones) are "non supportata".

Per symbol, `threshold`, `distance` and `triggered_now` refer to the newest data: the
forming bar when there is one (`value_now` set), else the closed bar (`value_now` None,
`triggered_now` = `triggered_closed`). `distance` is how far the value still has to go
(below: value - threshold; above and cross_up: threshold - value): <= 0 means at or past
the threshold (for cross_up without a fresh cross: already above, entered earlier). A
symbol whose data could not be read has null values and makes the strategy "dati
incompleti". `last` is the quote's price (else the last close); `as_of` the quote's time
with a forming bar, else the last closed bar's.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision, DecisionKind
from smtrading.backtest.models import Backtest
from smtrading.backtest.strategies import min_bars, signals, validate_params
from smtrading.common.errors import SMTError
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.marketdata import indicators
from smtrading.marketdata.forming import forming_bar
from smtrading.marketdata.indicators_service import fetch_bars
from smtrading.marketdata.types import Bar, Timeframe
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.settings import get_stale_threshold

OK = "ok"
INCOMPLETE = "dati incompleti"
UNSUPPORTED = "non supportata"
APPROVED = "approvata"
MODIFIED = "modificata"
APPROVALS = frozenset({APPROVED, MODIFIED})
# the statuses that replace the previous decision of a ref (informativa etc. do not)
STATUS_DECISIONS = APPROVALS | {"respinta", "sospesa", "revocata"}
SIGNAL_TYPES = ("rsi_reversion", "sma_cross", "breakout")
MIN_FETCH_BARS = 250  # RSI is Wilder-smoothed: a long warm-up keeps it near the backtest's
MAX_CONCURRENCY = 4


@dataclass
class SymbolSignal:
    symbol: str
    last: Decimal | None = None
    indicator: str | None = None
    value_closed: Decimal | None = None
    value_now: Decimal | None = None
    threshold: Decimal | None = None
    direction: str | None = None  # below | above | cross_up
    distance: Decimal | None = None
    triggered_closed: bool | None = None
    triggered_now: bool | None = None
    as_of: datetime | None = None


@dataclass
class StrategySignal:
    portfolio_id: uuid.UUID | None
    portfolio_name: str | None
    ref: str
    name: str
    type: str | None
    params: dict
    timeframe: str | None
    status: str
    symbols: list[SymbolSignal] = field(default_factory=list)


@dataclass
class _Chosen:
    """The effective CEO decision of a ref (a modificata merged over its approval)."""
    details: dict
    symbols: list
    portfolio_id: uuid.UUID | None
    subject: str | None
    summary: str


@dataclass
class _Approved:
    ref: str
    name: str
    type: str | None
    params: dict | None
    timeframe: str | None
    symbols: list[str]
    portfolios: list[Portfolio]


def _text(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _ref(d: Decision) -> str | None:
    details = d.details or {}
    return _text(details.get("ref")) or _text(details.get("strategy_id"))


def _status(d: Decision) -> str | None:
    s = _text((d.details or {}).get("status")) or _text(d.outcome)
    return s.lower() if s else None


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError, AttributeError):
        return None


def _first(*values: Any) -> Any:
    return next((v for v in values if v not in (None, "", [], {})), None)


@dataclass
class StrategyState:
    """A ref's latest CEO status decision and its name (the «Strategie» page)."""
    ref: str
    name: str
    status: str  # approvata | modificata | respinta | sospesa | revocata


async def _choose(session: AsyncSession) -> tuple[dict[str, _Chosen], dict[str, StrategyState]]:
    """(the effective approval of each approved ref, the latest status of every ref)."""
    chosen: dict[str, _Chosen] = {}
    states: dict[str, StrategyState] = {}
    for d in await session.scalars(
        select(Decision).where(Decision.kind == DecisionKind.strategy).order_by(Decision.id)
    ):
        ref, status = _ref(d), _status(d)
        if ref is None or status not in STATUS_DECISIONS:
            continue
        prev_state = states.get(ref)
        keep_name = status == MODIFIED and ref in chosen and prev_state is not None
        states[ref] = StrategyState(
            ref, _text(d.subject) or (prev_state.name if keep_name else d.summary), status)
        if status not in APPROVALS:
            chosen.pop(ref, None)
            continue
        prev = chosen.get(ref) if status == MODIFIED else None
        details = d.details or {}
        symbols = [s for s in (d.symbols or []) if isinstance(s, str)]
        if prev is None:
            chosen[ref] = _Chosen(dict(details), symbols, d.portfolio_id, d.subject, d.summary)
        else:
            chosen[ref] = _Chosen(
                {**prev.details, **{k: v for k, v in details.items()
                                    if v not in (None, "", [], {})}},
                symbols or prev.symbols, d.portfolio_id or prev.portfolio_id,
                _text(d.subject) or prev.subject, prev.summary,  # the name stays
            )
    return chosen, states


async def strategy_states(session: AsyncSession) -> dict[str, StrategyState]:
    """Every ref with a status decision: its latest status and name (approvals as
    `strategy_signals` sees them; a modificata keeps the approval's name unless it has a
    subject)."""
    return (await _choose(session))[1]


async def _approved(session: AsyncSession) -> list[_Approved]:
    chosen, _ = await _choose(session)
    if not chosen:
        return []
    quant: dict[str, dict] = {}
    refs = list(chosen)
    for d in await session.scalars(
        select(Decision).where(
            Decision.kind == DecisionKind.other,
            Decision.details["ref"].astext.in_(refs)
            | Decision.details["strategy_id"].astext.in_(refs),
        ).order_by(Decision.id)
    ):
        det = d.details or {}
        if not _text(det.get("strategy")):
            continue
        for key in (_text(det.get("ref")), _text(det.get("strategy_id"))):
            if key in chosen:
                quant[key] = det
    out = []
    for ref, d in chosen.items():
        det = d.details or {}
        bt_id = _uuid(det.get("backtest_id"))
        bt = await session.get(Backtest, bt_id) if bt_id else None
        q = quant.get(ref, {})
        params = _first(det.get("params"), bt.params if bt else None, q.get("params"))
        if params is None and any(isinstance(x, dict) for x in (
                det.get("params"), bt.params if bt else None, q.get("params"))):
            params = {}  # explicitly empty (e.g. buy_and_hold)
        symbols = _first(d.symbols, bt.symbols if bt else None) or []
        ids = [d.portfolio_id] if d.portfolio_id else []
        listed = det.get("portfolios")
        if isinstance(listed, list):
            ids += [u for u in map(_uuid, listed) if u is not None]
        if not ids and bt is not None and bt.portfolio_id is not None:
            ids = [bt.portfolio_id]
        portfolios = []
        for pid in dict.fromkeys(ids):
            p = await session.get(Portfolio, pid)
            if p is not None:
                portfolios.append(p)
        if portfolios and all(p.status == PortfolioStatus.archived for p in portfolios):
            continue  # only on archived portfolios: gone
        out.append(_Approved(
            ref=ref,
            name=_text(d.subject) or d.summary,
            type=_text(_first(det.get("strategy"), bt.strategy if bt else None,
                              q.get("strategy"))),
            params=params if isinstance(params, dict) else None,
            timeframe=_text(_first(det.get("timeframe"), bt.timeframe if bt else None,
                                   q.get("timeframe"))),
            symbols=list(dict.fromkeys(canonical_symbol(s) for s in symbols)),
            portfolios=[p for p in portfolios if p.status != PortfolioStatus.archived],
        ))
    return out


def _label(kind: str, p: dict) -> tuple[str, str]:
    if kind == "rsi_reversion":
        return f"RSI {p['period']}", "below"
    if kind == "sma_cross":
        return f"SMA {p['fast']} / SMA {p['slow']}", "cross_up"
    return f"Chiusura / massimo {p['lookback']}", "above"


def _evaluate(kind: str, p: dict, bars: list[Bar]) -> tuple[Decimal | None, Decimal | None, bool]:
    """(value, threshold, the entry fires on the last bar) — the backtester's rule."""
    closes = [b.close for b in bars]
    if kind == "rsi_reversion":
        value, threshold = indicators.rsi(closes, p["period"])[-1], p["buy_below"]
    elif kind == "sma_cross":
        value = indicators.sma(closes, p["fast"])[-1]
        threshold = indicators.sma(closes, p["slow"])[-1]
    else:
        n = p["lookback"]
        value = bars[-1].close
        threshold = max(b.high for b in bars[-1 - n:-1]) if len(bars) > n else None
    return value, threshold, signals(bars, kind, p)[-1] == "buy"


def _distance(direction: str, value: Decimal | None, threshold: Decimal | None):
    if value is None or threshold is None:
        return None
    return value - threshold if direction == "below" else threshold - value


async def _symbol(router: Any, kind: str, p: dict, tf: Timeframe, sym: str,
                  instr: Instrument | None, stale_s: int | None, now: datetime,
                  ) -> tuple[SymbolSignal, bool]:
    """(the symbol's signal, complete)."""
    indicator, direction = _label(kind, p)
    out = SymbolSignal(sym, indicator=indicator, direction=direction)
    if instr is None or stale_s is None:
        return out, False
    need = min_bars(kind, p)
    try:
        bars = await fetch_bars(router, sym, tf, max(MIN_FETCH_BARS, need * 3), now)
    except SMTError:
        return out, False
    if len(bars) < need:
        return out, False
    forming = await forming_bar(router, instr, tf, bars, stale_s, now)
    value, threshold, fired = _evaluate(kind, p, bars)
    out.value_closed, out.threshold, out.triggered_closed = value, threshold, fired
    out.triggered_now, out.as_of = fired, bars[-1].ts
    out.last = forming.price if forming.price is not None else bars[-1].close
    if forming.bar is not None:
        out.value_now, out.threshold, out.triggered_now = _evaluate(
            kind, p, [*bars, forming.bar])
        out.as_of = forming.quote_ts
    current = out.value_now if forming.bar is not None else value
    out.distance = _distance(direction, current, out.threshold)
    return out, value is not None and out.threshold is not None


async def strategy_signals(
    session: AsyncSession, router: Any, now: datetime | None = None
) -> list[StrategySignal]:
    """One entry per (approved strategy, portfolio), ordered by portfolio name then ref."""
    now = now or datetime.now(UTC)
    approved = await _approved(session)
    syms = {s for a in approved for s in a.symbols}
    instruments = {i.symbol: i for i in await session.scalars(
        select(Instrument).where(Instrument.symbol.in_(syms)))} if syms else {}
    stale: dict[str, int] = {}
    for i in instruments.values():
        if str(i.asset_class) not in stale:
            stale[str(i.asset_class)] = await get_stale_threshold(session, i.asset_class)
    gate = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(kind, p, tf, sym):
        instr = instruments.get(sym)
        async with gate:
            return await _symbol(router, kind, p, tf, sym, instr,
                                 stale.get(str(instr.asset_class)) if instr else None, now)

    out: list[StrategySignal] = []
    for a in approved:
        status, params, rows = OK, a.params or {}, []
        if a.type is None or a.params is None or a.timeframe is None or not a.symbols \
                or not a.portfolios:
            status = INCOMPLETE
        elif a.type not in SIGNAL_TYPES:
            status = UNSUPPORTED
        else:
            try:
                params = validate_params(a.type, a.params)
                tf = Timeframe(a.timeframe)
            except (SMTError, ValueError):
                status = INCOMPLETE
            else:
                done = await asyncio.gather(*(one(a.type, params, tf, s) for s in a.symbols))
                rows = [r for r, _ in done]
                if not all(ok for _, ok in done):
                    status = INCOMPLETE
        targets = a.portfolios or [None]
        for p in targets:
            out.append(StrategySignal(
                portfolio_id=p.id if p else None, portfolio_name=p.name if p else None,
                ref=a.ref, name=a.name, type=a.type, params=params,
                timeframe=a.timeframe, status=status, symbols=rows,
            ))
    out.sort(key=lambda s: (s.portfolio_name is None, s.portfolio_name or "", s.ref))
    return out
