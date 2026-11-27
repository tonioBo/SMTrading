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

Entry rules (the backtester's, `backtest.strategies`): rsi_reversion RSI below buy_below
(«below»); sma_cross the fast average crossing above the slow one («cross_up»);
breakout / donchian the close above the highest high of the previous lookback / entry
bars («above»); momentum the ROC (close `skip` bars ago over the one `lookback` before
it, in percent) above enter_above («above»).

Per symbol, `threshold`, `distance` and `triggered_now` refer to the newest data: the
forming bar when there is one (`value_now` set), else the closed bar (`value_now` None,
`triggered_now` = `triggered_closed`). `distance` is how far the value still has to go
(below: value - threshold; above and cross_up: threshold - value): <= 0 means at or past
the threshold (for cross_up without a fresh cross: already above, entered earlier). A
symbol whose data could not be read has null values and makes the strategy "dati
incompleti". `last` is the quote's price (else the last close); `as_of` the quote's time
with a forming bar, else the last closed bar's.

Per (portfolio, symbol) `position` is the open position of the strategy's portfolio
({qty, avg_price} from the `positions` projection, null when flat or without a
portfolio) and `open_entry` whether a buy entry order of that symbol is still resting
there (FILLABLE_STATUSES, not reduce-only, not an OCO leg, role entry or none): a
strategy already in the market, or waiting for its fill, is not a new signal.

With a position, `exit` is the strategy's exit rule on the same newest data ({rule,
value, threshold, distance, triggered}; null when flat or without data), the
backtester's sell rules: rsi_reversion RSI above sell_above; sma_cross the fast average
below the slow one (it was above when it entered); breakout / donchian the close below
the lowest low of the previous exit_lookback / exit bars; momentum the ROC below
exit_below. donchian with atr_stop: the higher of its channel low and its ATR stop
(highest high of the last `entry` bars minus atr_stop × ATR) is the exit shown, the one
that fires first. `distance` is how far the value still has to go (> 0: not yet; <= 0:
the exit fires).

`filters` are the entry filters of the strategy on the same newest data ({rule, value,
threshold, passed}; empty without filters): rsi_reversion's sma_filter (the close above
SMA n) and max_atr_pct (ATR as a share of the close at most max_atr_pct). A filter that
does not pass keeps `triggered_*` false even when the entry rule's value is past its
threshold (`distance` <= 0): the backtester would not enter either.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision, DecisionKind
from smtrading.backtest.models import Backtest
from smtrading.backtest.strategies import (
    atr_pct,
    atr_stop_line,
    min_bars,
    roc,
    signals,
    validate_params,
    volatility_ok,
)
from smtrading.common.errors import SMTError
from smtrading.engine.models import FILLABLE_STATUSES, Order, OrderSide
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.marketdata import indicators
from smtrading.marketdata.forming import forming_bar
from smtrading.marketdata.indicators_service import fetch_bars
from smtrading.marketdata.types import Bar, Timeframe
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.positions.models import Position
from smtrading.risk.settings import get_stale_threshold

OK = "ok"
INCOMPLETE = "dati incompleti"
# backtest execution settings sometimes registered with the parameters: not entry rules
EXECUTION_KEYS = frozenset({"slippage_bps"})
UNSUPPORTED = "non supportata"
APPROVED = "approvata"
MODIFIED = "modificata"
APPROVALS = frozenset({APPROVED, MODIFIED})
# the statuses that replace the previous decision of a ref (informativa etc. do not)
STATUS_DECISIONS = APPROVALS | {"respinta", "sospesa", "revocata"}
SIGNAL_TYPES = ("rsi_reversion", "sma_cross", "breakout", "momentum", "donchian")
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
    position: dict | None = None  # {qty, avg_price} in the strategy's portfolio
    open_entry: bool = False  # a resting buy entry of the symbol in that portfolio
    exit: dict | None = None  # the exit rule's state while in position
    filters: list[dict] = field(default_factory=list)  # entry filters on the newest data
    # the entry and exit rules one bar earlier (None: not enough bars): a watch with no
    # stored state yet tells a fresh false -> true from a rule that was already true
    triggered_prev: bool | None = None
    exit_prev: bool | None = None


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


Approved = _Approved  # public alias (the intraday selector's type)
_select = select  # `strategy_signals` has a `select` parameter


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


async def approved_strategies(session: AsyncSession) -> list[_Approved]:
    """The approved strategies as `strategy_signals` sees them (ref, name, type, params,
    timeframe, symbols, non-archived portfolios), without reading any market data."""
    return await _approved(session)


def _fast_name(p: dict) -> str:
    return "Chiusura" if p["fast"] == 1 else f"SMA {p['fast']}"  # SMA 1 is the close


def _roc_name(p: dict) -> str:
    return f"ROC {p['lookback']}" + (f" (salta {p['skip']})" if p["skip"] else "")


def _entry_window(kind: str, p: dict) -> int:
    return p["entry"] if kind == "donchian" else p["lookback"]


def _label(kind: str, p: dict) -> tuple[str, str]:
    if kind == "rsi_reversion":
        return f"RSI {p['period']}", "below"
    if kind == "sma_cross":
        return f"{_fast_name(p)} / SMA {p['slow']}", "cross_up"
    if kind == "momentum":
        return _roc_name(p), "above"
    return f"Chiusura / massimo {_entry_window(kind, p)}", "above"


def _it(d: Decimal) -> str:
    """70 → "70", 70.5 → "70,5" (a rule's label)."""
    return format(d.normalize(), "f").replace(".", ",")


def _exit(kind: str, p: dict, bars: list[Bar]) -> dict | None:
    """The exit rule on the last bar (see the module docstring); None without data."""
    closes = [b.close for b in bars]
    if kind == "rsi_reversion":
        rule = f"RSI {p['period']} sopra {_it(p['sell_above'])}"
        value, threshold = indicators.rsi(closes, p["period"])[-1], p["sell_above"]
        distance = None if value is None else threshold - value
    else:
        if kind == "sma_cross":
            rule = f"{_fast_name(p)} sotto SMA {p['slow']}"
            value = indicators.sma(closes, p["fast"])[-1]
            threshold = indicators.sma(closes, p["slow"])[-1]
        elif kind == "momentum":
            rule = f"{_roc_name(p)} sotto {_it(p['exit_below'])}"
            value = roc(closes, p["lookback"], p["skip"])[-1]
            threshold = p["exit_below"]
        else:
            n = p["exit"] if kind == "donchian" else p["exit_lookback"]
            rule = f"Chiusura sotto minimo {n}"
            value = bars[-1].close
            threshold = min(b.low for b in bars[-1 - n:-1]) if len(bars) > n else None
            k = p.get("atr_stop") if kind == "donchian" else None
            stop = atr_stop_line(bars, p["entry"], k, p["atr_period"])[-1] if k else None
            if stop is not None and (threshold is None or stop > threshold):
                rule = f"Chiusura sotto stop {_it(k)} × ATR {p['atr_period']}"
                threshold = stop
        distance = None if value is None or threshold is None else value - threshold
    if distance is None:
        return None
    triggered = distance < 0 if kind != "rsi_reversion" else value > threshold
    return {"rule": rule, "value": value, "threshold": threshold, "distance": distance,
            "triggered": triggered}


def _evaluate(kind: str, p: dict, bars: list[Bar]) -> tuple[Decimal | None, Decimal | None, bool]:
    """(value, threshold, the entry fires on the last bar) — the backtester's rule."""
    closes = [b.close for b in bars]
    if kind == "rsi_reversion":
        value, threshold = indicators.rsi(closes, p["period"])[-1], p["buy_below"]
    elif kind == "sma_cross":
        value = indicators.sma(closes, p["fast"])[-1]
        threshold = indicators.sma(closes, p["slow"])[-1]
    elif kind == "momentum":
        value, threshold = roc(closes, p["lookback"], p["skip"])[-1], p["enter_above"]
    else:
        n = _entry_window(kind, p)
        value = bars[-1].close
        threshold = max(b.high for b in bars[-1 - n:-1]) if len(bars) > n else None
    return value, threshold, signals(bars, kind, p)[-1] == "buy"


def _filters(kind: str, p: dict, bars: list[Bar]) -> list[dict]:
    """The strategy's entry filters on the last bar (see the module docstring)."""
    out = []
    n = p.get("sma_filter") if kind == "rsi_reversion" else None
    if n:
        close, sma = bars[-1].close, indicators.sma([b.close for b in bars], n)[-1]
        out.append({"rule": f"Chiusura sopra SMA {n}", "value": close, "threshold": sma,
                    "passed": sma is not None and close > sma})
    limit = p.get("max_atr_pct")
    if limit:
        value = atr_pct(bars, p["atr_period"])[-1]
        out.append({"rule": f"ATR {p['atr_period']} al massimo {_it(limit)}% del prezzo",
                    "value": value, "threshold": limit, "passed": volatility_ok(value, p)})
    return out


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
    newest = [*bars, forming.bar] if forming.bar is not None else bars
    out.exit = _exit(kind, p, newest)
    if len(bars) > need:  # one bar earlier, the same rules
        out.triggered_prev = _evaluate(kind, p, bars[:-1])[2]
        before = _exit(kind, p, newest[:-1])
        out.exit_prev = None if before is None else bool(before["triggered"])
    out.filters = _filters(kind, p, newest)
    out.distance = _distance(direction, current, out.threshold)
    return out, value is not None and out.threshold is not None


async def strategy_signals(
    session: AsyncSession, router: Any, now: datetime | None = None, *,
    select: Callable[[Approved], Approved | None] | None = None,
) -> list[StrategySignal]:
    """One entry per (approved strategy, portfolio), ordered by portfolio name then ref.

    With `select` every approved strategy goes through it before any data is read (None:
    left out; it may narrow the portfolios and symbols). Without it the output is the
    plain one. On the short timeframes there is no forming bar (`forming.FORMING_TIMEFRAMES`):
    entry and exit are already on the closed bars."""
    now = now or datetime.now(UTC)
    approved = await _approved(session)
    if select is not None:
        approved = [kept for a in approved if (kept := select(a)) is not None]
    syms = {s for a in approved for s in a.symbols}
    instruments = {i.symbol: i for i in await session.scalars(
        _select(Instrument).where(Instrument.symbol.in_(syms)))} if syms else {}
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
                params = validate_params(a.type, {k: v for k, v in a.params.items()
                                                  if k not in EXECUTION_KEYS})
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
    await _mark_holdings(session, out)
    out.sort(key=lambda s: (s.portfolio_name is None, s.portfolio_name or "", s.ref))
    return out


async def _mark_holdings(session: AsyncSession, out: list[StrategySignal]) -> None:
    """Per portfolio copies of the symbol rows with `position`, `open_entry` and `exit`
    (kept only with a position)."""
    pids = list({s.portfolio_id for s in out if s.portfolio_id is not None})
    syms = list({r.symbol for s in out for r in s.symbols})
    for s in out:
        if s.portfolio_id is None or not pids or not syms:
            s.symbols = [replace(r, exit=None) for r in s.symbols]
    if not pids or not syms:
        return
    held = {(p.portfolio_id, p.symbol): p for p in await session.scalars(
        select(Position).where(Position.portfolio_id.in_(pids), Position.symbol.in_(syms),
                               Position.qty != 0))}
    entries = set((await session.execute(
        select(Order.portfolio_id, Order.symbol).where(
            Order.portfolio_id.in_(pids), Order.symbol.in_(syms),
            Order.side == OrderSide.buy, Order.status.in_(FILLABLE_STATUSES),
            Order.reduce_only.is_(False), Order.oco_group_id.is_(None),
            Order.role.is_(None) | (Order.role == "entry"),
        ))).all())
    for s in out:
        if s.portfolio_id is None:
            continue
        rows = []
        for r in s.symbols:
            pos = held.get((s.portfolio_id, r.symbol))
            rows.append(replace(
                r, position={"qty": pos.qty, "avg_price": pos.avg_price} if pos else None,
                open_entry=(s.portfolio_id, r.symbol) in entries,
                exit=r.exit if pos else None))
        s.symbols = rows
