"""Admin API: portfolios, valuation, positions, performance, orders, ledger movements,
cash moves, reversals, reset and archive. Every route requires an admin session (router
level).

Archived portfolios (Task 13) are left out of the list unless `include_archived=1`; they
stay readable by id, and every change (settings, cash moves, reversals touching them,
reset) is refused with PORTFOLIO_ARCHIVED until they are restored.

Handlers commit; on an error the session is rolled back and the queued post-commit
effects are dropped. Decimals go out as plain strings (`mcp.dto.to_jsonable`).
"""

import csv
import io
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_access
from smtrading.admin.common import Quotes, _invalid, _transaction
from smtrading.admin.idempotency import RESET_IN_FLIGHT_TTL_S, Idempotency, idempotent
from smtrading.admin.portfolio_ops import (
    archive_portfolio,
    reset_portfolio,
    unarchive_portfolio,
)
from smtrading.admin.schemas import (
    CashMoveIn,
    PortfolioCreateIn,
    PortfolioPatchIn,
    ResetIn,
    ReverseIn,
)
from smtrading.agents.service import write_audit
from smtrading.brokers.live.deposit import check_live_deposit, read_live_cash
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.fingerprints import name_warning
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import OPEN_STATUSES, OrderStatus
from smtrading.engine.quotesource import MemoQuoteSource
from smtrading.engine.service import cancel_order, list_orders
from smtrading.ledger.models import Account, Journal, JournalLine, JournalType
from smtrading.ledger.service import reverse_journal
from smtrading.mcp.dto import (
    journal_dto,
    order_dto,
    parse_decimal,
    parse_enum,
    parse_int,
    parse_ts,
    position_valuation_dto,
    to_jsonable,
    valuation_dto,
)
from smtrading.performance.service import get_performance
from smtrading.portfolios.locks import lock_live_portfolio
from smtrading.portfolios.mode import admin_mode_fields
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.queries import day_pnl, list_portfolio_journals
from smtrading.portfolios.service import (
    create_portfolio,
    deposit,
    get_cash_balances,
    set_portfolio_status,
    withdraw,
)
from smtrading.risk.models import RiskProfile
from smtrading.valuation.service import value_portfolio

log = logging.getLogger("smtrading.admin.portfolios")
router = APIRouter(prefix="/api", tags=["portfolios"], dependencies=[Depends(require_access)])

MIN_LEVERAGE, MAX_LEVERAGE = Decimal(1), Decimal(10)
MAX_SLIPPAGE_BPS = 10_000
MAX_NAME_LEN = 128
DEFAULT_TX_LIMIT, MAX_TX_LIMIT = 50, 500
DEFAULT_ORDER_LIMIT, MAX_ORDER_LIMIT = 100, 500
CSV_BATCH = 500
CSV_HEADER = (
    "seq", "data", "tipo", "asset", "conto", "quantità", "prezzo", "valuta", "motivazione",
    "id_ordine", "id_journal",
)
# the patchable statuses: reduce_only is only ever set by the risk engine
_PATCH_STATUSES = (PortfolioStatus.active.value, PortfolioStatus.locked.value)
# Ruling R5: executions are never reversed; an opposite order corrects them
_EXECUTION_JOURNALS = frozenset(
    {JournalType.TRADE, JournalType.LIQUIDATION, JournalType.CORPORATE_ACTION}
)
EXECUTION_REVERSAL_MESSAGE = "Non si può stornare un'esecuzione: usa un ordine di segno opposto"
RESET_REVERSAL_MESSAGE = "Il reset non si può stornare"  # Ruling R13
# spreadsheet formula prefixes (CSV injection): such text cells get a leading '
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


async def _portfolio(session: AsyncSession, portfolio_id: UUID) -> Portfolio:
    portfolio = await session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise SMTError(
            ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio_id": str(portfolio_id)}
        )
    return portfolio


# --- validation ----------------------------------------------------------------------


def _clean_name(name: str | None) -> str:
    cleaned = (name or "").strip()
    if not cleaned or len(cleaned) > MAX_NAME_LEN:
        raise _invalid("name", f"il nome deve avere da 1 a {MAX_NAME_LEN} caratteri")
    return cleaned


def _duplicate_name(name: str) -> SMTError:
    return SMTError(
        ErrorCode.INVALID_ORDER, "esiste già un portafoglio con questo nome",
        {"field": "name", "name": name},
    )


def _leverage(value: Any) -> Decimal:
    lev = parse_decimal(value, "leverage")
    if lev is None or not MIN_LEVERAGE <= lev <= MAX_LEVERAGE:
        raise _invalid("leverage", f"la leva deve essere tra {MIN_LEVERAGE} e {MAX_LEVERAGE}")
    return lev


def _slippage(value: Any) -> int:
    bps = parse_int(value, "slippage_bps", 0, MAX_SLIPPAGE_BPS)
    if bps is None:
        raise _invalid("slippage_bps", "slippage_bps obbligatorio")
    return bps


def _timezone(value: str | None) -> str:
    try:
        ZoneInfo(value or "")
    except (ZoneInfoNotFoundError, ValueError):
        raise _invalid("timezone", "fuso orario non valido") from None
    return value  # type: ignore[return-value]


def _pct(value: Any, field: str) -> Decimal:
    pct = parse_decimal(value, field)
    if pct is None:
        raise _invalid(field, f"{field} obbligatorio")
    return pct


async def _check_risk_profile(session: AsyncSession, profile_id: UUID | None) -> None:
    if profile_id is not None and await session.get(RiskProfile, profile_id) is None:
        raise SMTError(
            ErrorCode.NOT_FOUND, "profilo di rischio non trovato",
            {"field": "risk_profile_id", "risk_profile_id": str(profile_id)},
        )


async def _name_taken(session: AsyncSession, name: str, exclude: UUID | None = None) -> bool:
    query = select(Portfolio.id).where(Portfolio.name == name)
    if exclude is not None:
        query = query.where(Portfolio.id != exclude)
    return await session.scalar(query) is not None


# --- portfolio views -----------------------------------------------------------------

PORTFOLIO_FIELDS = (
    "id", "name", "base_currency", "leverage", "maintenance_margin_pct",
    "warning_margin_pct", "slippage_bps", "status", "timezone", "risk_profile_id",
    "reduce_only_reason", "reduce_only_until", "created_at", "broker_link_id",
    "archived_at", "archived_by",
)


def _portfolio_dto(p: Portfolio, link: BrokerLink | None = None) -> dict[str, Any]:
    """PORTFOLIO_FIELDS plus `mode`, `live_state`, `broker_kind` and `mode_label` (from
    `link`, the portfolio's broker link: `reale` only for an armed real-money link)."""
    out = {f: to_jsonable(getattr(p, f)) for f in PORTFOLIO_FIELDS}
    out.update(to_jsonable(admin_mode_fields(p, link)))
    return out


async def portfolio_dto(session: AsyncSession, p: Portfolio) -> dict[str, Any]:
    """_portfolio_dto with the link read from the database (identity map first)."""
    link = await session.get(BrokerLink, p.broker_link_id) if p.broker_link_id else None
    return _portfolio_dto(p, link)


async def _summary(
    session: AsyncSession, qs: MemoQuoteSource, redis, portfolio: Portfolio
) -> dict[str, Any]:
    """The portfolio settings plus equity_eur and the day P&L. A portfolio that cannot be
    valued (no quote, no cache) has them null and `valuation_error` set to the code."""
    out = await portfolio_dto(session, portfolio)
    out.update(equity_eur=None, day_pnl_eur=None, day_pnl_pct=None, valuation_error=None)
    try:
        v = await value_portfolio(session, qs, portfolio.id, redis=redis)
        pnl, pct = await day_pnl(session, qs, portfolio, v)
    except SMTError as e:
        out["valuation_error"] = str(e.code)
        return out
    out.update(equity_eur=to_jsonable(v.equity_eur), day_pnl_eur=to_jsonable(pnl),
               day_pnl_pct=to_jsonable(pct))
    return out


def _with_name_warning(out: dict[str, Any]) -> dict[str, Any]:
    """A save response: `name_warning` set when agents will see a simulation word."""
    out["name_warning"] = name_warning(out["name"])
    return out


@router.get("/portfolios")
async def list_portfolios(
    session: DbSession, redis: RedisClient, qs: Quotes, include_archived: bool = False
) -> list[dict]:
    query = select(Portfolio).order_by(Portfolio.name)
    if not include_archived:
        query = query.where(Portfolio.status != PortfolioStatus.archived)
    portfolios = list(await session.scalars(query))
    return [await _summary(session, qs, redis, p) for p in portfolios]


@router.post("/portfolios")
async def create(
    body: PortfolioCreateIn, admin: CurrentAdmin, session: DbSession, redis: RedisClient,
    qs: Quotes,
) -> dict:
    name = _clean_name(body.name)
    initial_cash = parse_decimal(body.initial_cash, "initial_cash")
    leverage = _leverage(body.leverage)
    slippage = _slippage(body.slippage_bps)
    tz = _timezone(body.timezone)
    try:
        async with _transaction(session, redis):
            await _check_risk_profile(session, body.risk_profile_id)
            portfolio = await create_portfolio(
                session, name=name, initial_cash=initial_cash, base_currency=body.base_currency,
                leverage=leverage, slippage_bps=slippage, timezone=tz,
                author_id=admin.email, risk_profile_id=body.risk_profile_id,
            )
    except IntegrityError:  # a concurrent creation with the same name won the race
        if await _name_taken(session, name):
            raise _duplicate_name(name) from None
        raise
    return _with_name_warning(await _summary(session, qs, redis, portfolio))


@router.get("/portfolios/{portfolio_id}")
async def get_portfolio(
    portfolio_id: UUID, session: DbSession, redis: RedisClient, qs: Quotes
) -> dict:
    return await _summary(session, qs, redis, await _portfolio(session, portfolio_id))


@router.patch("/portfolios/{portfolio_id}")
async def patch_portfolio(
    portfolio_id: UUID, body: PortfolioPatchIn, session: DbSession, redis: RedisClient,
    qs: Quotes,
) -> dict:
    """Only the fields sent change. status: "active" (also clears a reduce_only and its
    reason/until) or "locked"."""
    fields = body.model_fields_set
    try:
        async with _transaction(session, redis):
            p = await lock_live_portfolio(session, portfolio_id)
            if "name" in fields:
                name = _clean_name(body.name)
                if await _name_taken(session, name, exclude=p.id):
                    raise _duplicate_name(name)
                p.name = name
            if "leverage" in fields:
                p.leverage = _leverage(body.leverage)
            if "slippage_bps" in fields:
                p.slippage_bps = _slippage(body.slippage_bps)
            if "timezone" in fields:
                p.timezone = _timezone(body.timezone)
            if "risk_profile_id" in fields:
                await _check_risk_profile(session, body.risk_profile_id)
                p.risk_profile_id = body.risk_profile_id
            if "maintenance_margin_pct" in fields:
                p.maintenance_margin_pct = _pct(
                    body.maintenance_margin_pct, "maintenance_margin_pct"
                )
            if "warning_margin_pct" in fields:
                p.warning_margin_pct = _pct(body.warning_margin_pct, "warning_margin_pct")
            if not 0 < p.maintenance_margin_pct < p.warning_margin_pct <= 100:
                raise SMTError(
                    ErrorCode.INVALID_ORDER,
                    "serve 0 < maintenance_margin_pct < warning_margin_pct <= 100",
                    {"field": "maintenance_margin_pct"
                     if "maintenance_margin_pct" in fields else "warning_margin_pct"},
                )
            if "status" in fields:
                status = parse_enum(PortfolioStatus, body.status, "status")
                if status is None or status.value not in _PATCH_STATUSES:
                    raise SMTError(
                        ErrorCode.INVALID_ORDER, "status deve essere active o locked",
                        {"field": "status", "allowed": list(_PATCH_STATUSES)},
                    )
                # a live anomaly's reduce-only is cleared only with the TOTP resume
                set_portfolio_status(session, p, status)
            await session.flush()
    except IntegrityError:
        if "name" in fields and await _name_taken(session, _clean_name(body.name), portfolio_id):
            raise _duplicate_name(_clean_name(body.name)) from None
        raise
    return _with_name_warning(await _summary(session, qs, redis, p))


@router.get("/portfolios/{portfolio_id}/valuation")
async def get_valuation(
    portfolio_id: UUID, session: DbSession, redis: RedisClient, qs: Quotes
) -> dict:
    await _portfolio(session, portfolio_id)
    return valuation_dto(await value_portfolio(session, qs, portfolio_id, redis=redis))


@router.get("/portfolios/{portfolio_id}/positions")
async def get_positions(
    portfolio_id: UUID, session: DbSession, redis: RedisClient, qs: Quotes
) -> list[dict]:
    await _portfolio(session, portfolio_id)
    v = await value_portfolio(session, qs, portfolio_id, redis=redis)
    return [
        {**position_valuation_dto(p), "side": "long" if p.qty > 0 else "short"}
        for p in v.positions
    ]


@router.get("/portfolios/{portfolio_id}/performance")
async def performance(portfolio_id: UUID, session: DbSession, period: str = "1m") -> dict:
    await _portfolio(session, portfolio_id)
    return to_jsonable(await get_performance(session, portfolio_id, period))  # type: ignore[arg-type]


# --- orders --------------------------------------------------------------------------

ORDER_BROKER_FIELDS = (
    "broker_link_id", "broker_replicated", "broker_status", "broker_order_id",
)


def _order_dto(o) -> dict[str, Any]:
    """order_dto plus the broker replication state (admin only; the MCP DTO is unchanged)."""
    out = order_dto(o)
    out.update({f: to_jsonable(getattr(o, f)) for f in ORDER_BROKER_FIELDS})
    return out


def _parse_statuses(value: str | None) -> list[OrderStatus] | None:
    """Comma-separated order statuses; "open" stands for every non-final status."""
    if not value:
        return None
    out: list[OrderStatus] = []
    for part in (s.strip() for s in value.split(",") if s.strip()):
        if part == "open":
            out.extend(sorted(OPEN_STATUSES))
        else:
            out.append(parse_enum(OrderStatus, part, "status"))  # type: ignore[arg-type]
    return out or None


@router.get("/portfolios/{portfolio_id}/orders")
async def get_orders(
    portfolio_id: UUID, session: DbSession, status: str | None = None,
    limit: int = DEFAULT_ORDER_LIMIT,
) -> list[dict]:
    statuses = _parse_statuses(status)
    n = parse_int(limit, "limit", 1, MAX_ORDER_LIMIT)
    await _portfolio(session, portfolio_id)
    orders = await list_orders(session, portfolio_id, status=statuses, limit=n)  # type: ignore[arg-type]
    return [_order_dto(o) for o in orders]


@router.post("/orders/{order_id}/cancel")
async def cancel(
    order_id: UUID, admin: CurrentAdmin, session: DbSession, redis: RedisClient
) -> dict:
    async with _transaction(session, redis):
        order = await cancel_order(session, order_id, by="admin")
        await write_audit(
            session, agent_id=None, method="admin:order.cancel",
            params={"order_id": order.id, "portfolio_id": order.portfolio_id,
                    "symbol": order.symbol, "status": order.status, "by": admin.email},
            outcome="ok", latency_ms=0,
        )
        out = _order_dto(order)
    return out


# --- ledger movements ----------------------------------------------------------------


def _parse_journal_types(value: str | None) -> list[JournalType] | None:
    if not value:
        return None
    types = [parse_enum(JournalType, t.strip(), "type") for t in value.split(",") if t.strip()]
    return types or None  # type: ignore[return-value]


def _tx_filters(type_: str | None, from_: str | None, to: str | None) -> dict:
    return {
        "types": _parse_journal_types(type_),
        "start": parse_ts(from_, "from"),
        "end": parse_ts(to, "to"),
    }


@router.get("/portfolios/{portfolio_id}/transactions")
async def transactions(
    portfolio_id: UUID, session: DbSession,
    type_: Annotated[str | None, Query(alias="type")] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None, before_seq: int | None = None, limit: int = DEFAULT_TX_LIMIT,
) -> dict:
    """{items (newest first), next_before_seq}: pass next_before_seq as before_seq for the
    next page; null on the last one. type: comma-separated journal types."""
    filters = _tx_filters(type_, from_, to)
    n = parse_int(limit, "limit", 1, MAX_TX_LIMIT)
    before = parse_int(before_seq, "before_seq", 1)
    await _portfolio(session, portfolio_id)
    items = await list_portfolio_journals(
        session, portfolio_id, before_seq=before, limit=n + 1, with_audit=True, **filters  # type: ignore[operator]
    )
    more = len(items) > n  # type: ignore[operator]
    items = items[:n]
    return {
        "items": to_jsonable(items),
        "next_before_seq": items[-1]["seq"] if more else None,
    }


def _csv_decimal(d: Decimal | None) -> str:
    return "" if d is None else decimal_to_str(d).replace(".", ",")


def _csv_text(value: str | None) -> str:
    """Free text for a spreadsheet: a cell that would start a formula gets a leading '."""
    text = value or ""
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


@router.get("/portfolios/{portfolio_id}/transactions.csv")
async def transactions_csv(
    portfolio_id: UUID, request: Request, session: DbSession,
    type_: Annotated[str | None, Query(alias="type")] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
) -> StreamingResponse:
    """Every movement matching the filters, one row per journal line of the portfolio,
    newest first: `;` separated, decimal comma, dates in the portfolio timezone, UTF-8
    with BOM (Excel)."""
    filters = _tx_filters(type_, from_, to)
    portfolio = await _portfolio(session, portfolio_id)
    tz = ZoneInfo(portfolio.timezone)
    sessionmaker = request.app.state.sessionmaker

    def encode(rows: list[tuple]) -> str:
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="\r\n").writerows(rows)
        return buf.getvalue()

    async def generate() -> AsyncIterator[bytes]:
        yield ("\ufeff" + encode([CSV_HEADER])).encode()
        try:
            async for chunk in rows():
                yield chunk
        except Exception:
            # the 200 status and the header are already sent: the client gets a cut file
            log.exception("CSV export of portfolio %s failed mid-stream", portfolio_id)
            raise

    async def rows() -> AsyncIterator[bytes]:
        before: int | None = None
        async with sessionmaker() as s:  # the request session may be closed by now
            while True:
                batch = await list_portfolio_journals(
                    s, portfolio_id, before_seq=before, limit=CSV_BATCH, **filters
                )
                if not batch:
                    return
                lines = [
                    (
                        j["seq"], j["created_at"].astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
                        j["type"].value, _csv_text(line["asset"]), line["kind"].value,
                        _csv_decimal(line["quantity"]), _csv_decimal(line["unit_price"]),
                        _csv_text(line["price_ccy"]), _csv_text(j["reason"]),
                        j["order_id"] or "", j["id"],
                    )
                    for j in batch for line in j["lines"]
                ]
                yield encode(lines).encode()
                before = batch[-1]["seq"]

    filename = f"movimenti-{portfolio_id}.csv"
    return StreamingResponse(
        generate(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- cash moves, reversal, reset -----------------------------------------------------


async def _cash_move(
    mover: Callable[..., Awaitable[Journal]], portfolio_id: UUID, body: CashMoveIn,
    admin, session: AsyncSession, redis, idem: Idempotency, *,
    live_guard: tuple[Request, MemoQuoteSource] | None = None,
):
    """A deposit or withdrawal; with `live_guard` (deposits) a portfolio linked to a
    real-money link needs the cash at the broker first (brokers.live.deposit)."""
    async def run() -> dict:
        broker = None
        if live_guard is not None:  # read before any lock, no transaction open (R9)
            from smtrading.admin.routes_live import live_http_factory  # (circular import)

            request = live_guard[0]
            broker = await read_live_cash(request.app.state.sessionmaker, portfolio_id,
                                          live_http_factory(request), live_guard[1],
                                          currency=body.currency)
        async with _transaction(session, redis):
            portfolio = await lock_live_portfolio(session, portfolio_id)
            amount = parse_decimal(body.amount, "amount")
            currency = body.currency or portfolio.base_currency
            if live_guard is not None and amount > 0:
                await check_live_deposit(session, portfolio, broker, amount, currency)
            journal = await mover(
                session, portfolio_id, amount, currency,
                author_id=admin.email, reason=(body.reason or "").strip() or None,
            )
            out = {
                "journal": journal_dto(journal),
                "cash": to_jsonable(await get_cash_balances(session, portfolio_id)),
            }
        return out

    return await idem.run(run)


@router.post("/portfolios/{portfolio_id}/deposit")
async def deposit_cash(
    portfolio_id: UUID, body: CashMoveIn, request: Request, admin: CurrentAdmin,
    session: DbSession, redis: RedisClient, qs: Quotes,
    idem: Annotated[Idempotency, Depends(idempotent("deposit"))],
):
    return await _cash_move(deposit, portfolio_id, body, admin, session, redis, idem,
                            live_guard=(request, qs))


@router.post("/portfolios/{portfolio_id}/withdraw")
async def withdraw_cash(
    portfolio_id: UUID, body: CashMoveIn, admin: CurrentAdmin, session: DbSession,
    redis: RedisClient, idem: Annotated[Idempotency, Depends(idempotent("withdraw"))],
):
    return await _cash_move(withdraw, portfolio_id, body, admin, session, redis, idem)


@router.post("/journals/{journal_id}/reverse")
async def reverse(
    journal_id: UUID, body: ReverseIn, admin: CurrentAdmin, session: DbSession,
    redis: RedisClient, idem: Annotated[Idempotency, Depends(idempotent("reverse"))],
):
    """A REVERSAL of the journal. Executions (TRADE, LIQUIDATION, CORPORATE_ACTION) are
    refused (Ruling R5): an order of opposite sign corrects them. RESET too (Ruling R13)."""
    reason = body.reason.strip()

    async def run() -> dict:
        if not reason:
            raise SMTError(ErrorCode.INVALID_REVERSAL, "lo storno richiede una motivazione")
        async with _transaction(session, redis):
            original_type = await session.scalar(
                select(Journal.type).where(Journal.id == journal_id)
            )
            if original_type is None:
                raise SMTError(
                    ErrorCode.NOT_FOUND, "journal not found", {"journal_id": str(journal_id)}
                )
            if original_type in _EXECUTION_JOURNALS:  # before any lock
                raise SMTError(
                    ErrorCode.INVALID_REVERSAL, EXECUTION_REVERSAL_MESSAGE,
                    {"journal_id": str(journal_id), "type": str(original_type)},
                )
            if original_type == JournalType.RESET:
                raise SMTError(
                    ErrorCode.INVALID_REVERSAL, RESET_REVERSAL_MESSAGE,
                    {"journal_id": str(journal_id), "type": str(original_type)},
                )
            portfolio_ids = sorted(set(await session.scalars(
                select(Account.portfolio_id)
                .join(JournalLine, JournalLine.account_id == Account.id)
                .where(JournalLine.journal_id == journal_id, Account.portfolio_id.is_not(None))
            )))
            for pid in portfolio_ids:  # portfolio rows before the chain lock (Ruling R11)
                await lock_live_portfolio(session, pid)
            journal = await reverse_journal(
                session, journal_id, reason=reason, author_type="admin", author_id=admin.email,
            )
            lines = list(await session.scalars(
                select(JournalLine).where(JournalLine.journal_id == journal.id)
                .order_by(JournalLine.id)
            ))
            out = journal_dto(journal, lines)
        return out

    return await idem.run(run)


@router.post("/portfolios/{portfolio_id}/reset")
async def reset(
    portfolio_id: UUID, body: ResetIn, request: Request, admin: CurrentAdmin,
    session: DbSession, redis: RedisClient, qs: Quotes,
    idem: Annotated[
        Idempotency, Depends(idempotent("reset", in_flight_ttl_s=RESET_IN_FLIGHT_TTL_S))
    ],
):
    """With new cash, a portfolio linked to a real-money link needs that cash at the
    broker first (brokers.live.deposit, the deposit guard)."""
    async def run() -> dict:
        new_cash = parse_decimal(body.new_cash, "new_cash")
        broker = None
        if new_cash > 0:  # read before any lock, no transaction open (R9)
            from smtrading.admin.routes_live import live_http_factory  # (circular import)

            broker = await read_live_cash(request.app.state.sessionmaker, portfolio_id,
                                          live_http_factory(request), qs,
                                          currency=body.currency)
        async with _transaction(session, redis):
            if new_cash > 0:
                portfolio = await lock_live_portfolio(session, portfolio_id)
                await check_live_deposit(session, portfolio, broker, new_cash,
                                         body.currency, replace=True, field="new_cash")
            result = await reset_portfolio(
                session, qs, portfolio_id, new_cash, body.currency,  # type: ignore[arg-type]
                reason=body.reason, author_id=admin.email, redis=redis,
            )
            out = to_jsonable(result)
        return out

    return await idem.run(run)


@router.post("/portfolios/{portfolio_id}/archive")
async def archive(
    portfolio_id: UUID, admin: CurrentAdmin, session: DbSession, redis: RedisClient,
    qs: Quotes,
) -> dict:
    """Archive (idempotent): refused with open orders, a broker link, shorts or a margin
    loan."""
    async with _transaction(session, redis):
        p = await archive_portfolio(session, portfolio_id, by=admin.email)
    return await _summary(session, qs, redis, p)


@router.post("/portfolios/{portfolio_id}/unarchive")
async def unarchive(
    portfolio_id: UUID, admin: CurrentAdmin, session: DbSession, redis: RedisClient,
    qs: Quotes,
) -> dict:
    """Restore (idempotent) with the status the portfolio had when archived, after the
    dividends/splits missed while archived are applied (PROVIDER_UNAVAILABLE when the
    calendar cannot be read: retry)."""
    async with _transaction(session, redis):
        p = await unarchive_portfolio(session, portfolio_id, qs=qs, by=admin.email)
    return await _summary(session, qs, redis, p)
