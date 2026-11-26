"""Stato del conto del broker, letto solo con GET e solo per i collegamenti con un
portafoglio in `DAY_SCOPE`; al posto del confronto PDT (the rule is gone since 4/6/2026).

The state (`BrokerLink.pdt_state`, a column name kept from migration 0046) holds only
`buying_power`, `non_marginable_buying_power` (the buying power of crypto at Alpaca),
`multiplier`, `equity`, `trading_blocked`, `account_blocked` and `read_at`: never the
account id. A link of campaign portfolios gets no new broker call. No transaction is open
during a network call. A blocked account is reported on Telegram once per NYSE session."""
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import open_adapter
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments.market_hours import local_date
from smtrading.intraday.day_trades import (
    ACCOUNT_KINDS,
    ACCOUNT_READ_INTERVAL_S,
    DAY_TRADE_MARKET,
)
from smtrading.intraday.models import DAY_SCOPE
from smtrading.jobs.runs import run_job_once
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.reconciliation.service import ReadOnlyHttp
from smtrading.telegram.outbox import enqueue

log = logging.getLogger("smtrading.intraday.account_reader")

# the read interval lives in day_trades (risk.checks imports it without brokers.service)
__all__ = ["ACCOUNT_READ_INTERVAL_S", "read_account_state", "refresh_account_states"]


def _text(v) -> str | None:
    return None if v is None else str(v)


def _flag(v) -> bool | None:
    return None if v is None else bool(v)


def _state_from(raw: dict, read_at: datetime) -> dict:
    """Only the fields we need, never the account id."""
    return {
        "buying_power": _text(raw.get("buying_power")),
        "non_marginable_buying_power": _text(raw.get("non_marginable_buying_power")),
        "multiplier": _text(raw.get("multiplier")),
        "equity": _text(raw.get("equity")),
        "trading_blocked": _flag(raw.get("trading_blocked")),
        "account_blocked": _flag(raw.get("account_blocked")),
        "read_at": read_at.isoformat(),
    }


async def read_account_state(
    sessionmaker: async_sessionmaker, http, link_id: uuid.UUID
) -> dict:
    """One read of the broker account (no open transaction during the call); saves the
    account state in `pdt_state`, sets `pdt_state_at` to its `read_at` and returns it.
    A read error: PROVIDER_UNAVAILABLE."""
    try:
        async with sessionmaker() as s, open_adapter(
                s, link_id, http, sessionmaker=sessionmaker) as adapter:
            await s.rollback()
            inner = adapter.http
            adapter.http = ReadOnlyHttp(inner)
            try:
                snap = await adapter.get_account()
            finally:
                adapter.http = inner
    except SMTError:
        raise
    except Exception as e:  # noqa: BLE001  (any broker failure; the text may hold secrets)
        raise SMTError(
            ErrorCode.PROVIDER_UNAVAILABLE, "Lettura del conto broker non riuscita",
            {"broker_link_id": str(link_id), "error": type(e).__name__},
        ) from None
    read_at = datetime.now(UTC)
    state = _state_from(snap.raw, read_at)
    async with sessionmaker() as s:
        await s.execute(update(BrokerLink).where(BrokerLink.id == link_id).values(
            pdt_state=state, pdt_state_at=read_at))
        await s.commit()
    return state


async def _link_ids(sessionmaker: async_sessionmaker) -> list[uuid.UUID]:
    async with sessionmaker() as s:
        return list((await s.scalars(
            select(BrokerLink.id).where(
                BrokerLink.enabled.is_(True), BrokerLink.kind.in_(ACCOUNT_KINDS),
                exists().where(Portfolio.broker_link_id == BrokerLink.id, DAY_SCOPE,
                               Portfolio.status != PortfolioStatus.archived),
            ).order_by(BrokerLink.id))).all())


async def _alert_blocked(sessionmaker: async_sessionmaker, link_id: uuid.UUID, state: dict,
                         now: datetime) -> None:
    what = [label for key, label in (("trading_blocked", "operatività"),
                                     ("account_blocked", "conto")) if state[key] is True]
    if not what:
        return
    async with sessionmaker() as s:
        link = await s.get(BrokerLink, link_id)
        if link is None:
            return
        day = local_date(DAY_TRADE_MARKET, now)
        if await run_job_once(s, "intraday_account_blocked", f"{link_id}:{day}"):
            await enqueue(
                s, source="system", category="system", level="warning",
                text=(f"Conto {link.name} bloccato dal broker ({' e '.join(what)}): nessun "
                      "nuovo ordine passerà finché il broker non lo sblocca"))
        await s.commit()


async def refresh_account_states(sessionmaker: async_sessionmaker, http, now: datetime) -> int:
    """Read every link in scope; the number of links read. A failure of one link is logged
    and retried next round; it never stops the others."""
    read = 0
    for link_id in await _link_ids(sessionmaker):
        try:
            state = await read_account_state(sessionmaker, http, link_id)
        except SMTError as e:
            log.warning("account state of link %s not read: %s", link_id,
                        (e.details or {}).get("error", e.code))
            continue
        except Exception:
            log.exception("account state of link %s failed", link_id)
            continue
        read += 1
        try:
            await _alert_blocked(sessionmaker, link_id, state, now)
        except Exception:
            log.exception("blocked-account alert of link %s failed", link_id)
    return read
