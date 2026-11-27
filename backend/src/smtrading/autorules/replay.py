"""The replay of an automatic rule on one-minute history (pure, no I/O, Decimal only).

It drives the same `RuleRunner` as the live `scalper`: every bar becomes four prices
(`price_path`) handed to `runner.on_price`, then the bar itself goes to `runner.on_bar`
(the live order: a minute's bar arrives after its prices). Around the runner it plays
the parts of the executor and the broker:

- the spread: `bid = p × (1 - spread_pct/200)`, `ask = p × (1 + spread_pct/200)`;
- an entry intent is sent (`runner.sent`) and filled at the ask of the first price at or
  after `trigger + delay_s`; stop and target are `runner.levels(ask at the trigger)` (the
  executor's expected price); the quantity is `qty`, or `notional / ask at the trigger`
  rounded down to `lot_size` when given (none: nothing to buy, like the live guard);
- from the price after the fill, triggered on the bid like the engine's sell orders
  (`engine.triggers`): the stop (`bid <= stop`) sells at `min(stop, bid)`, the target
  (`bid >= target`) at the target; the runner's own
  exits (time, end of session) sell at the bid of the first price at or after
  `exit + delay_s` (the bracket's stop and target canceled, as `close_position` does);
- fees `compute_trade_fee(fee_rule, notional)` on the entry and
  `compute_trade_fee(exit_fee_rule or fee_rule, notional)` on the exit (the caller
  resolves the broker's rule per side).

Declared limits (in every result's notes): inside a minute only four prices are known,
so a bar that reaches both the target and the stop is taken as a stop (prudent), even
when its path reaches the target first (and the runner's time or session exit is not
taken at a price above the target before that stop); one-minute volume of equities on the free data
plan is IEX only. A trade still open at the end of the history is closed at the last bid
(`end`, noted). Brakes, the guards on stale quotes and on the spread, the risk checks
and the currency conversion of the fees are not replayed (noted)."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from smtrading.autorules.runner import SESSION_EXIT_MARGIN_MIN, Entry, Exit, RuleRunner
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData, compute_trade_fee
from smtrading.marketdata.types import Bar

MINUTE_NOTE = ("al minuto non si vedono i movimenti dentro il minuto; volume solo IEX per "
               "le azioni con il flusso gratuito")
PRUDENT_NOTE = ("stop e obiettivo raggiunti nello stesso minuto: si assume lo stop "
                "(scelta prudente)")
NOT_SIMULATED_NOTE = ("freni, controlli su prezzi vecchi e forbice, controlli di rischio e "
                      "cambio valuta delle commissioni non sono simulati")
END_NOTE = "operazione ancora aperta alla fine dello storico: chiusa all'ultimo prezzo"
_STEP = timedelta(seconds=15)


def price_path(bar: Bar) -> list[tuple[datetime, Decimal]]:
    """Four prices inside the minute, 15 s apart: O, L, H, C for a bar that closes at or
    above its open, O, H, L, C otherwise."""
    middle = (bar.low, bar.high) if bar.close >= bar.open else (bar.high, bar.low)
    prices = (bar.open, *middle, bar.close)
    return [(bar.ts + i * _STEP, p) for i, p in enumerate(prices)]


@dataclass(frozen=True)
class ReplayTrade:
    entry_ts: datetime
    entry_price: Decimal
    exit_ts: datetime
    exit_price: Decimal
    exit_reason: str  # target, stop, time, session, end
    qty: Decimal
    fees: Decimal
    net: Decimal


@dataclass(frozen=True)
class ReplayResult:
    trades: list[ReplayTrade]
    net: Decimal
    fees: Decimal
    wins: int
    losses: int
    max_drawdown: Decimal
    notes: list[str]


@dataclass
class _Trade:
    """The trade in progress: sent (no entry yet), then open."""
    trigger_ts: datetime
    qty: Decimal
    stop: Decimal
    target: Decimal
    entry_ts: datetime | None = None
    entry_price: Decimal | None = None
    exit_due: datetime | None = None  # the runner's exit, waiting for the delay
    exit_reason: str | None = None


def _invalid(message: str, field: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _check(qty, notional, spread_pct, delay_s, day_close_minutes, lot_size) -> None:
    if (qty is None) == (notional is None):
        raise _invalid("indicare la quantità oppure il controvalore", "qty")
    if (qty is not None and not qty > 0) or (notional is not None and not notional > 0):
        raise _invalid("quantità e controvalore devono essere maggiori di 0", "qty")
    if lot_size is not None and not lot_size > 0:
        raise _invalid("il lotto deve essere maggiore di 0", "lot_size")
    if spread_pct < 0:
        raise _invalid("la forbice non può essere negativa", "spread_pct")
    if delay_s < 0:
        raise _invalid("il ritardo non può essere negativo", "delay_s")
    if day_close_minutes < 0:
        raise _invalid("day_close_minutes non può essere negativo", "day_close_minutes")


def _size(qty, notional, lot_size, expected: Decimal) -> Decimal:
    if qty is not None:
        return qty
    raw = notional / expected
    return raw if lot_size is None else (raw / lot_size).to_integral_value(ROUND_FLOOR) * lot_size


def max_drawdown(nets: list[Decimal]) -> Decimal:
    """The largest peak-to-trough drop of the cumulative results, from 0."""
    peak = cum = worst = Decimal(0)
    for n in nets:
        cum += n
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def replay(bars: list[Bar], *, model: str, params: dict, market: str, tick_size: Decimal,
           qty: Decimal | None, notional: Decimal | None, fee_rule: FeeRuleData,
           spread_pct: Decimal, delay_s: int, day_close_minutes: int,
           lot_size: Decimal | None = None,
           exit_fee_rule: FeeRuleData | None = None) -> ReplayResult:
    _check(qty, notional, spread_pct, delay_s, day_close_minutes, lot_size)
    runner = RuleRunner(model, params, market=market, tick_size=tick_size,
                        session_exit_before=timedelta(
                            minutes=day_close_minutes + SESSION_EXIT_MARGIN_MIN))
    half = spread_pct / 200
    delay = timedelta(seconds=delay_s)
    trades: list[ReplayTrade] = []
    notes = [MINUTE_NOTE, PRUDENT_NOTE, NOT_SIMULATED_NOTE]
    trade: _Trade | None = None
    last: tuple[datetime, Decimal] | None = None  # the last price's ts and bid

    def close(ts: datetime, price: Decimal, reason: str) -> None:
        nonlocal trade
        assert trade is not None and trade.entry_price is not None
        fees = (compute_trade_fee(fee_rule, trade.qty * trade.entry_price)
                + compute_trade_fee(exit_fee_rule or fee_rule, trade.qty * price))
        net = (price - trade.entry_price) * trade.qty - fees
        trades.append(ReplayTrade(trade.entry_ts, trade.entry_price, ts, price, reason,
                                  trade.qty, fees, net))
        runner.exited(ts)
        trade = None

    for bar in sorted(bars, key=lambda b: b.ts):
        path = price_path(bar)
        target_blocked = False  # the bar reaches the stop after the target: the stop
        for i, (ts, p) in enumerate(path):
            bid, ask = p * (1 - half), p * (1 + half)
            last = (ts, bid)
            if trade is not None and trade.entry_price is None:  # sent
                if ts >= trade.trigger_ts + delay:
                    trade.entry_ts, trade.entry_price = ts, ask
                    runner.entered(ts, ask)
                continue
            if trade is not None and trade.exit_due is not None:  # the runner's exit
                if ts >= trade.exit_due + delay:
                    close(ts, bid, trade.exit_reason)
                continue
            if trade is not None:  # open, its stop and target resting
                # the engine's triggers of a sell: stop on bid <= stop, limit on bid >= limit
                if bid <= trade.stop:
                    close(ts, min(trade.stop, bid), "stop")
                    continue
                if bid >= trade.target:
                    if not target_blocked and not any(
                            q * (1 - half) <= trade.stop for _, q in path[i + 1:]):
                        close(ts, trade.target, "target")
                        continue
                    # the stop comes later in this minute (prudent): no exit of the
                    # runner above the target at this price either
                    target_blocked = True
                    continue
            intent = runner.on_price(ts, p, bid, ask)
            if isinstance(intent, Entry):
                size = _size(qty, notional, lot_size, ask)
                if size <= 0:
                    continue
                stop, target = runner.levels(ask)
                runner.sent(ts)
                trade = _Trade(ts, size, stop, target)
                if delay_s == 0:
                    trade.entry_ts, trade.entry_price = ts, ask
                    runner.entered(ts, ask)
            elif isinstance(intent, Exit) and trade is not None:
                trade.exit_due, trade.exit_reason = intent.ts, intent.reason
                if delay_s == 0:
                    close(ts, bid, intent.reason)
        runner.on_bar(bar)

    if trade is not None and trade.entry_price is not None and last is not None:
        close(last[0], last[1], "end")
        notes.append(END_NOTE)
    nets = [t.net for t in trades]
    return ReplayResult(
        trades=trades, net=sum(nets, Decimal(0)),
        fees=sum((t.fees for t in trades), Decimal(0)),
        wins=sum(1 for n in nets if n > 0), losses=sum(1 for n in nets if n < 0),
        max_drawdown=max_drawdown(nets), notes=notes)
