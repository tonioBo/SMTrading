"""Backtest queueing, limits and the worker job that runs queued backtests.

A backtest is a pure simulation: it reads bars (MarketDataRouter) and the fee schedule
(read-only) and never touches the ledger, portfolios, orders or brokers.

Limits per agent: at most MAX_ACTIVE backtests queued or running (RATE_LIMITED), at most
MAX_DAILY_SPAN_DAYS for daily-or-slower bars and MAX_INTRADAY_SPAN_DAYS for intraday ones,
at most MAX_SYMBOLS symbols (RISK_LIMIT_EXCEEDED).

BacktestRunner.run: every `poll_s` (2s) it moves `queued` backtests to `running` (FOR
UPDATE SKIP LOCKED, only while the agent has fewer than MAX_ACTIVE running), runs each
engine in a thread under a timeout, and stores `completed` with the result or `failed`
with the error. At start, `running` backtests older than 10 minutes go back to `queued`.
"""

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.agents.models import Agent
from smtrading.backtest import strategies
from smtrading.backtest.engine import run_backtest
from smtrading.backtest.models import Backtest, BacktestStatus
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData, Tier
from smtrading.fees.models import FeeScope, FeeSide
from smtrading.fees.resolver import load_rules, resolve_rule
from smtrading.instruments.service import canonical_symbol, get_instrument
from smtrading.marketdata.types import Timeframe
from smtrading.mcp.dto import to_jsonable
from smtrading.portfolios.models import Portfolio

log = logging.getLogger("smtrading.backtest.runner")

MAX_ACTIVE = 2
MAX_SYMBOLS = 10
MAX_DAILY_SPAN_DAYS = 5 * 366
MAX_INTRADAY_SPAN_DAYS = 90
MAX_TRADES_STORED = 1000
DEFAULT_SLIPPAGE_BPS = 5
STALE_MARGIN_S = 60.0
REQUEUE_EVERY_S = 60.0
TIMEOUT_S = 300.0
_DAILY = (Timeframe.d1, Timeframe.w1)


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def zero_fee_rule() -> FeeRuleData:
    return FeeRuleData(
        id=uuid4(), scope=FeeScope.global_, scope_ref=None, side=FeeSide.both, currency="EUR",
        fixed=Decimal(0), pct=Decimal(0), min_fee=None, max_fee=None, tiers=None,
        fx_pct=Decimal(0), priority=0, active=True, created_at=datetime.now(UTC),
    )


async def enqueue_backtest(
    session: AsyncSession, agent_id: UUID, *, strategy: str, symbols: list[str],
    timeframe: str, start: datetime, end: datetime, initial_cash: Decimal, params: dict,
    portfolio_id: UUID | None,
) -> Backtest:
    """Validate and queue a backtest (the caller has checked portfolio read permission)."""
    params = dict(params or {})
    slip = params.pop("slippage_bps", None)  # explicit slippage override, not a strategy param
    if slip is not None and (
        isinstance(slip, bool) or not isinstance(slip, int) or not 0 <= slip <= 1000
    ):
        raise _invalid("slippage_bps", "slippage_bps must be an integer 0-1000")
    norm = strategies.validate_params(strategy, params)
    try:
        tf = Timeframe(timeframe)
    except ValueError:
        raise _invalid("timeframe", f"timeframe must be one of {[t.value for t in Timeframe]}"
                       ) from None
    if not symbols or not all(isinstance(s, str) and s.strip() for s in symbols):
        raise _invalid("symbols", "symbols must be a non-empty list of symbols")
    names = sorted({canonical_symbol(s) for s in symbols})
    if len(names) > MAX_SYMBOLS:
        raise SMTError(ErrorCode.RISK_LIMIT_EXCEEDED, f"at most {MAX_SYMBOLS} symbols",
                       {"limit": MAX_SYMBOLS})
    if start >= end:
        raise _invalid("from_date", "from_date must be before to_date")
    if initial_cash <= 0:
        raise _invalid("initial_cash", "initial_cash must be positive")
    limit_days = MAX_DAILY_SPAN_DAYS if tf in _DAILY else MAX_INTRADAY_SPAN_DAYS
    if end - start > timedelta(days=limit_days):
        raise SMTError(
            ErrorCode.RISK_LIMIT_EXCEEDED,
            f"range too long for {tf.value} bars: at most {limit_days} days",
            {"limit_days": limit_days, "timeframe": tf.value},
        )
    for name in names:
        await get_instrument(session, name)
    # serialise this agent's enqueues so the active count cannot be overshot
    await session.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())
    active = await session.scalar(
        select(func.count()).select_from(Backtest).where(
            Backtest.agent_id == agent_id,
            Backtest.status.in_([BacktestStatus.queued, BacktestStatus.running]),
        )
    )
    if (active or 0) >= MAX_ACTIVE:
        raise SMTError(ErrorCode.RATE_LIMITED,
                       f"at most {MAX_ACTIVE} backtests queued or running per agent",
                       {"limit": MAX_ACTIVE})
    bt = Backtest(
        agent_id=agent_id, strategy=strategy, symbols=names, timeframe=tf.value,
        start=start, end=end, initial_cash=initial_cash, params={**to_jsonable(norm), **({} if slip is None else {"slippage_bps": slip})},
        portfolio_id=portfolio_id, status=BacktestStatus.queued, warnings=[],
    )
    session.add(bt)
    await session.flush()
    return bt


class BacktestRunner:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], router, *,
                 poll_s: float = 2.0, timeout_s: float = TIMEOUT_S,
                 slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
                 requeue_every_s: float = REQUEUE_EVERY_S) -> None:
        self._sm = sessionmaker
        self._router = router
        self._poll_s = poll_s
        self._timeout_s = timeout_s
        self._slippage_bps = slippage_bps
        self._requeue_every_s = requeue_every_s

    async def run(self) -> None:
        last_requeue = float("-inf")
        while True:
            try:
                if time.monotonic() - last_requeue >= self._requeue_every_s:
                    last_requeue = time.monotonic()
                    await self.requeue_stale()
                if await self.run_once():
                    continue  # one backtest per iteration: look for the next at once
            except Exception:
                log.exception("backtest runner tick failed")
            await asyncio.sleep(self._poll_s)

    async def requeue_stale(self) -> int:
        """`running` rows older than the run timeout (plus a margin) were left by a dead
        worker: back to `queued`."""
        stale = datetime.now(UTC) - timedelta(seconds=self._timeout_s + STALE_MARGIN_S)
        async with self._sm() as s:
            res = await s.execute(
                update(Backtest)
                .where(Backtest.status == BacktestStatus.running,
                       Backtest.started_at < stale)
                .values(status=BacktestStatus.queued, started_at=None)
            )
            await s.commit()
            return res.rowcount or 0

    async def run_once(self) -> int:
        """Claim one runnable queued backtest and run it; returns how many ran (0 or 1)."""
        ids = await self._claim()
        for bid in ids:
            await self._execute(bid)
        return len(ids)

    async def _claim(self) -> list[UUID]:
        claimed: list[UUID] = []
        async with self._sm() as s:
            rows = (await s.execute(
                select(Backtest).where(Backtest.status == BacktestStatus.queued)
                .order_by(Backtest.created_at).limit(20).with_for_update(skip_locked=True)
            )).scalars().all()
            running: dict[UUID | None, int] = {
                a: n for a, n in (await s.execute(
                    select(Backtest.agent_id, func.count()).where(
                        Backtest.status == BacktestStatus.running
                    ).group_by(Backtest.agent_id)
                )).all()
            }
            for bt in rows:
                if running.get(bt.agent_id, 0) >= MAX_ACTIVE:
                    continue
                bt.status = BacktestStatus.running
                bt.started_at = datetime.now(UTC)  # execution starts right after the claim
                claimed.append(bt.id)
                break  # one per iteration: the others stay queued for the next one
            await s.commit()
        return claimed

    async def _execute(self, backtest_id: UUID) -> None:
        try:
            result, warnings = await asyncio.wait_for(self._compute(backtest_id),
                                                      self._timeout_s)
        except TimeoutError:
            await self._finish(backtest_id, error=f"timeout after {self._timeout_s:g}s")
        except SMTError as e:
            await self._finish(backtest_id, error=f"{e.code}: {e.message}")
        except Exception as e:
            log.exception("backtest %s failed", backtest_id)
            await self._finish(backtest_id, error=f"{type(e).__name__}: {e}")
        else:
            await self._finish(backtest_id, result=result, warnings=warnings)

    async def _compute(self, backtest_id: UUID) -> tuple[dict, list[str]]:
        async with self._sm() as s:
            bt = await s.get(Backtest, backtest_id)
            assert bt is not None
            instruments = {n: await get_instrument(s, n) for n in bt.symbols}
            rules = await load_rules(s)
            spec = (bt.strategy, dict(bt.params or {}), bt.initial_cash, bt.portfolio_id,
                    bt.timeframe, bt.start, bt.end, list(bt.symbols))
            portfolio = await s.get(Portfolio, bt.portfolio_id) if bt.portfolio_id else None
            portfolio_slip = portfolio.slippage_bps if portfolio else None
        strategy, params, cash, portfolio_id, timeframe, start, end, symbols = spec
        explicit = params.pop("slippage_bps", None)  # explicit > portfolio > default
        slippage = next(v for v in (explicit, portfolio_slip, self._slippage_bps)
                        if v is not None)
        warnings: list[str] = []
        fee_rules: dict[str, FeeRuleData] = {}
        for sym in symbols:  # fee schedule per symbol's asset class; read-only
            instr = instruments[sym]
            try:
                rule = resolve_rule(rules, portfolio_id=portfolio_id or UUID(int=0),
                                    asset_class=instr.asset_class, side="buy")
            except SMTError:
                warnings.append(f"no_fee_rule:{sym}: simulated without fees")
                fee_rules[sym] = zero_fee_rule()
                continue
            fee_rules[sym] = await self._in_instrument_currency(rule, instr.currency, warnings)
        bars = {sym: await self._router.get_bars(sym, Timeframe(timeframe), start, end)
                for sym in symbols}
        result = await asyncio.to_thread(
            run_backtest, bars, strategy, params, initial_cash=cash, fee_rule=fee_rules,
            slippage_bps=slippage,
            lot_by_symbol={n: i.lot_size for n, i in instruments.items()},
        )
        warnings += result["warnings"]
        if len(result["trades"]) > MAX_TRADES_STORED:
            result["trades"] = result["trades"][:MAX_TRADES_STORED]
            warnings.append(f"trades_truncated:{MAX_TRADES_STORED}")
        result["slippage_bps"] = slippage
        result["fee_rule_ids"] = {s: str(r.id) for s, r in fee_rules.items()}
        return to_jsonable(result), warnings

    async def _in_instrument_currency(
        self, rule: FeeRuleData, currency: str, warnings: list[str]
    ) -> FeeRuleData:
        """Fixed fee, min/max and tier bounds converted at the FX rate of the backtest
        start; the schedule's fx_pct is added to the percentage for cross-currency trades.
        No rate available: the rule is kept as is and a warning is added."""
        if rule.currency == currency:
            return rule
        try:
            rate = await self._router.get_fx_rate(rule.currency, currency)
        except SMTError:
            warnings.append(f"fee_currency_unconverted:{rule.currency}->{currency}")
            return rule
        fx = rule.fx_pct

        def conv(v: Decimal | None) -> Decimal | None:
            return None if v is None else v * rate

        tiers = None if rule.tiers is None else tuple(
            Tier(up_to=conv(t.up_to), pct=t.pct + fx) for t in rule.tiers
        )
        return replace(rule, currency=currency, fixed=rule.fixed * rate, pct=rule.pct + fx,
                       min_fee=conv(rule.min_fee), max_fee=conv(rule.max_fee), tiers=tiers)

    async def _finish(self, backtest_id: UUID, *, result: dict | None = None,
                      warnings: list[str] | None = None, error: str | None = None) -> None:
        async with self._sm() as s:
            bt = await s.get(Backtest, backtest_id)
            if bt is None:
                return
            bt.status = BacktestStatus.failed if error else BacktestStatus.completed
            bt.result, bt.error = result, error
            bt.warnings = warnings or []
            bt.finished_at = datetime.now(UTC)
            await s.commit()
