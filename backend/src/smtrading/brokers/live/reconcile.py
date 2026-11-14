"""Live reconciliation: the broker's positions and cash against a real-money portfolio.

Run by the LiveMonitor every LIVE_RECONCILE_INTERVAL_S and after each fill; it never
corrects anything. Reads happen in a short session, the broker calls (GET only) with no
transaction open (R9), the outcome is written in a last session:

- a symbol with an order in flight at the broker (sent, still open) is left out of that
  pass (`{"symbol", "skipped": "ordine in corso"}`), and with any such order the cash is
  not compared either: a fill not yet synced is not a difference. The state is read
  before and after the broker calls; a symbol (or the cash) that changed meanwhile, or
  in flight in either read, is skipped too;
- a position that differs, or broker cash below ours by more than `cash_tolerance`
  (a surplus is fine: the account is dedicated, money may be added there) -> a
  `Reconciliation(ok=False)` and `trip_reduce_only` (`reconcile_diff` / `cash_shortfall`);
- another account behind the same keys (fingerprint) -> the link goes back to
  `configured`, the portfolio to reduce-only (`fingerprint_changed`) and a critical
  `live.account_changed`; the stored fingerprint changes only with a new test (Task 5).
"""

import hashlib
import logging
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.admin.common import mask_value
from smtrading.brokers.base import AccountSnapshot
from smtrading.brokers.live.audit import record_live_audit
from smtrading.brokers.live.models import LiveState
from smtrading.brokers.live.protect import live_alert, trip_reduce_only
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.brokers.registry import adapter_class
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json
from smtrading.common.logsetup import register_secret
from smtrading.engine.models import OPEN_STATUSES, Order
from smtrading.engine.quotesource import QuoteSource
from smtrading.events import json_safe
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.models import Portfolio

log = logging.getLogger(__name__)

LIVE_RECONCILE_INTERVAL_S = 60
LIVE_CONFIG_AAD = b"broker_live"
CASH_TOLERANCE_EUR = Decimal("0.50")
CASH_TOLERANCE_PCT = Decimal("0.001")
INFLIGHT = "ordine in corso"
ACCOUNT_CHANGED_REASON = "conto cambiato"


class LiveBrokerUnreachable(Exception):
    """The broker calls of a reconciliation failed (network, 5xx, rate limit...)."""


def cash_tolerance(ours: Decimal, eur_in_ccy: Decimal) -> Decimal:
    """max(0,50 € in the account currency, 0,1% of our cash)."""
    return max(CASH_TOLERANCE_EUR * eur_in_ccy, ours * CASH_TOLERANCE_PCT)


def account_fingerprint(kind: str, account_id: str) -> str:
    return hashlib.sha256(f"{kind}:{account_id}".encode()).hexdigest()


def snapshot_json(snap: AccountSnapshot) -> dict:
    """The stored `last_account_snapshot`: never the broker's `raw` answer nor the full
    account ID (same shape as brokers.live.service's test snapshot)."""
    return json_safe({
        "account_masked": mask_value(snap.account_id) if snap.account_id else None,
        "account_type": snap.account_type,
        "currency": snap.currency, "cash_available": snap.cash_available,
        "positions_count": snap.positions_count, "open_orders_count": snap.open_orders_count,
        "crypto_enabled": snap.crypto_enabled,
        "order_types": sorted(str(t) for t in snap.order_types)
        if snap.order_types is not None else None,
    })


def _num(v: Decimal) -> str:
    return format(v.normalize(), "f") if v != 0 else "0"


@dataclass
class _State:
    portfolio_ids: list[UUID]
    positions: dict[str, Decimal] = field(default_factory=dict)
    cash: dict[str, Decimal] = field(default_factory=dict)
    inflight: set[str] = field(default_factory=set)


async def _read_state(session: AsyncSession, link_id: UUID) -> _State:
    portfolio_ids = list(await session.scalars(
        select(Portfolio.id).where(Portfolio.broker_link_id == link_id).order_by(Portfolio.id)
    ))
    state = _State(portfolio_ids)
    if not portfolio_ids:
        return state
    rows = await session.execute(
        select(Account.kind, Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(Account.portfolio_id.in_(portfolio_ids),
               Account.kind.in_((AccountKind.position, AccountKind.cash)))
    )
    for kind, asset, balance in rows:
        target = state.positions if kind == AccountKind.position else state.cash
        target[asset] = target.get(asset, Decimal(0)) + balance
    state.positions = {s: q for s, q in state.positions.items() if q != 0}
    state.inflight = set(await session.scalars(
        select(Order.symbol).where(
            or_(Order.portfolio_id.in_(portfolio_ids), Order.broker_link_id == link_id),
            Order.status.in_(OPEN_STATUSES),
            or_(Order.broker_order_id.is_not(None), Order.broker_sent_at.is_not(None)),
        )
    ))
    return state


def _live_config(link: BrokerLink) -> dict:
    if link.config_encrypted is None:
        return {}
    config = decrypt_json(link.config_encrypted, get_settings().master_key,
                          aad=LIVE_CONFIG_AAD)
    for value in config.values():
        if isinstance(value, str):
            register_secret(value)
    return config


async def _our_cash(qs: QuoteSource | None, cash: dict[str, Decimal],
                    ccy: str) -> tuple[Decimal, Decimal] | None:
    """(our cash, 1 EUR) in the account currency `ccy`; None if a rate is missing."""
    rates: dict[str, Decimal] = {ccy: Decimal(1)}
    try:
        for c in {*cash, "EUR"} - {ccy}:
            if qs is None:
                return None
            rates[c] = await qs.get_fx_rate(c, ccy)
    except Exception as e:  # noqa: BLE001  (no rate: the cash is not compared)
        log.warning("live reconciliation: fx rate to %s unavailable: %s", ccy, e)
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        total = sum((amount * rates[c] for c, amount in cash.items()), Decimal(0))
    return total, rates["EUR"]


async def reconcile_live(
    sessionmaker: async_sessionmaker[AsyncSession], link_id: UUID, http_factory, *,
    qs: QuoteSource | None = None,
) -> Reconciliation | None:
    """Reconcile a live link (live_state not null) with its linked portfolio; None (no
    broker call) for any other link or without a linked portfolio. Broker failures raise
    LiveBrokerUnreachable. The returned row is the first linked portfolio's."""
    async with sessionmaker() as session:
        link = await session.get(BrokerLink, link_id)
        if link is None or link.live_state is None:
            return None
        before = await _read_state(session, link_id)
        if not before.portfolio_ids:
            return None
        kind, fingerprint = link.kind, link.account_fingerprint
        config = _live_config(link)
        instruments = list(await session.scalars(
            select(Instrument).where(Instrument.symbol.in_(sorted(before.positions)))
        )) if before.positions else []

    http = http_factory(link_id)
    try:
        adapter = adapter_class(kind)(config, http)
        await adapter.prepare(instruments)
        broker = await adapter.get_positions()
        snap = await adapter.get_account()
    except Exception as e:
        raise LiveBrokerUnreachable(str(e)[:500]) from e
    finally:
        await http.aclose()
    money = await _our_cash(qs, before.cash, snap.currency)

    async with sessionmaker() as session:
        link = await session.get(BrokerLink, link_id, with_for_update=True)
        after = await _read_state(session, link_id)
        pids = after.portfolio_ids or before.portfolio_ids
        if fingerprint is not None and account_fingerprint(kind, snap.account_id) != fingerprint:
            rows = await _account_changed(session, link, pids)
        else:
            link.last_account_snapshot = snapshot_json(snap)
            link.last_snapshot_at = await session.scalar(select(func.now()))
            diffs, position_diff, cash_short = _compare(before, after, broker, snap, money)
            rows = [Reconciliation(broker_link_id=link_id, portfolio_id=pid, diffs=diffs,
                                   ok=not (position_diff or cash_short)) for pid in pids]
            session.add_all(rows)
            await session.flush()
            for pid in pids:
                if position_diff:
                    await trip_reduce_only(session, pid, cause="reconcile_diff",
                                           details={"link_id": link_id, "diffs": diffs})
                if cash_short:
                    await trip_reduce_only(session, pid, cause="cash_shortfall",
                                           details={"link_id": link_id, "diffs": diffs})
            if position_diff or cash_short:
                log.warning("live link %s: reconciliation differs: %s", link_id, diffs)
        await session.commit()
    return rows[0] if rows else None


def _compare(before: _State, after: _State, broker: dict[str, Decimal],
             snap: AccountSnapshot, money: tuple[Decimal, Decimal] | None
             ) -> tuple[list[dict], bool, bool]:
    diffs: list[dict] = []
    skipped = before.inflight | after.inflight | {
        s for s in set(before.positions) | set(after.positions)
        if before.positions.get(s) != after.positions.get(s)
    }
    position_diff = False
    for symbol in sorted(set(after.positions) | set(broker) | skipped):
        if symbol in skipped:
            diffs.append({"symbol": symbol, "skipped": INFLIGHT})
            continue
        ours, theirs = after.positions.get(symbol, Decimal(0)), broker.get(symbol, Decimal(0))
        if ours != theirs:
            diffs.append({"symbol": symbol, "ours": _num(ours), "broker": _num(theirs)})
            position_diff = True
    cash_short = False
    if before.inflight or after.inflight or before.cash != after.cash:
        diffs.append({"cash": {"skipped": INFLIGHT}})
    elif money is None:
        diffs.append({"cash": {"skipped": "cambio non disponibile"}})
    else:
        ours_cash, eur = money
        tolerance = cash_tolerance(ours_cash, eur)
        if snap.cash_available < ours_cash - tolerance:
            cash_short = True
            diffs.append({"cash": {"ours": _num(ours_cash), "broker": _num(snap.cash_available),
                                   "currency": snap.currency, "tolerance": _num(tolerance)}})
    return diffs, position_diff, cash_short


async def _account_changed(session: AsyncSession, link: BrokerLink,
                           pids: list[UUID]) -> list[Reconciliation]:
    """Another account behind the keys: back to `configured` (a new test and TOTP are
    needed), portfolio in reduce-only, critical alert (once per change of state)."""
    was = link.live_state
    if was != LiveState.configured:
        link.live_state = LiveState.configured
        link.disarmed_at = await session.scalar(select(func.now()))
        link.disarmed_reason = ACCOUNT_CHANGED_REASON
        await record_live_audit(session, action="disarmed", link_id=link.id, actor="system",
                                portfolio_id=pids[0] if pids else None,
                                details={"reason": ACCOUNT_CHANGED_REASON, "from": was})
        await live_alert(
            session, kind="account_changed",
            summary="Conto diverso dietro le stesse credenziali: denaro reale disattivato",
            portfolio_id=pids[0] if pids else None, link_id=link.id, data={"from": was},
        )
    rows = [Reconciliation(broker_link_id=link.id, portfolio_id=pid,
                           diffs=[{"account": "cambiato"}], ok=False) for pid in pids]
    session.add_all(rows)
    await session.flush()
    for pid in pids:
        await trip_reduce_only(session, pid, cause="fingerprint_changed",
                               details={"link_id": link.id})
    log.warning("live link %s: account changed, back to configured", link.id)
    return rows
