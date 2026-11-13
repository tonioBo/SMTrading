"""Double-entry ledger: posting with balancing, materialized balances and hash chain.

Domain services never commit: the caller owns the transaction and must roll it back
when a function here raises.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.ledger.hashing import GENESIS_HASH, canonical_payload, compute_hash
from smtrading.ledger.models import (
    Account,
    AccountBalance,
    AccountKind,
    Journal,
    JournalLine,
    JournalType,
)

# Serializes every append to the journal hash chain (transaction-scoped).
CHAIN_LOCK_KEY = 7300001

# Precision for Python-side sums: NUMERIC(38,18) exceeds the default 28 digits.
_PREC = 60

# Bounds of NUMERIC(38,18): at most 18 decimal places and 20 integer digits.
_MIN_EXPONENT = -18
_MAX_ABS = Decimal("1E20")

# Portfolio account kinds that must never be negative unless explicitly allowed.
_NON_NEGATIVE_KINDS = frozenset({AccountKind.cash, AccountKind.position, AccountKind.collateral})


@dataclass(frozen=True)
class LineSpec:
    account_id: UUID
    quantity: Decimal
    unit_price: Decimal | None = None
    price_ccy: str | None = None


async def get_or_create_account(
    session: AsyncSession, *, portfolio_id: UUID | None, asset: str, kind: AccountKind
) -> Account:
    await session.execute(
        insert(Account)
        .values(id=uuid.uuid4(), portfolio_id=portfolio_id, asset=asset, kind=kind)
        .on_conflict_do_nothing(constraint="uq_accounts_portfolio_asset_kind")
    )
    portfolio_cond = (
        Account.portfolio_id.is_(None) if portfolio_id is None
        else Account.portfolio_id == portfolio_id
    )
    return (
        await session.execute(
            select(Account).where(portfolio_cond, Account.asset == asset, Account.kind == kind)
        )
    ).scalar_one()


async def get_balance(session: AsyncSession, account_id: UUID) -> Decimal:
    balance = await session.scalar(
        select(AccountBalance.balance).where(AccountBalance.account_id == account_id)
    )
    return Decimal(0) if balance is None else balance


async def _lock_chain(session: AsyncSession) -> None:
    # Transaction-scoped and re-entrant within the same transaction.
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CHAIN_LOCK_KEY})


def _invalid(message: str, details: dict | None = None) -> SMTError:
    return SMTError(ErrorCode.LEDGER_INVALID, message, details)


def _check_bounds(field: str, value: Decimal) -> None:
    """Reject (never quantize) values that NUMERIC(38,18) would round or refuse."""
    # copy_abs and comparisons are exact: no context rounding.
    if (
        not value.is_finite()
        or value.as_tuple().exponent < _MIN_EXPONENT
        or value.copy_abs() >= _MAX_ABS
    ):
        raise _invalid(
            f"{field} out of bounds: finite, at most 18 decimals, abs < 1E20",
            {"field": field, "value": str(value)},
        )


async def post_journal(
    session: AsyncSession,
    *,
    type: JournalType,
    lines: list[LineSpec],
    author_type: str,
    author_id: str | None,
    reason: str | None = None,
    order_id: UUID | None = None,
    fill_id: UUID | None = None,
    reversed_journal_id: UUID | None = None,
    allow_negative: frozenset[AccountKind] = frozenset(),
) -> Journal:
    if len(lines) < 2:
        raise _invalid("a journal needs at least two lines")
    for ln in lines:
        _check_bounds("quantity", ln.quantity)
        if ln.unit_price is not None:
            _check_bounds("unit_price", ln.unit_price)
    if any(ln.quantity == 0 for ln in lines):
        raise _invalid("journal lines must have a non-zero quantity")

    # First statement of the posting. Chain safety relies on READ COMMITTED: each
    # statement below sees journals committed by whoever held the lock before us, so the
    # prev_hash we read is the real tip. Under REPEATABLE READ/SERIALIZABLE the snapshot
    # may predate that commit; UNIQUE(prev_hash) then makes a forking insert fail.
    await _lock_chain(session)

    account_ids = {ln.account_id for ln in lines}
    accounts = {
        a.id: a
        for a in (
            await session.execute(select(Account).where(Account.id.in_(account_ids)))
        ).scalars()
    }
    missing = account_ids - accounts.keys()
    if missing:
        raise _invalid("unknown account", {"account_ids": sorted(str(m) for m in missing)})

    with localcontext() as ctx:
        ctx.prec = _PREC
        per_asset: dict[str, Decimal] = defaultdict(Decimal)
        per_account: dict[UUID, Decimal] = defaultdict(Decimal)
        for ln in lines:
            per_asset[accounts[ln.account_id].asset] += ln.quantity
            per_account[ln.account_id] += ln.quantity
    unbalanced = {asset: decimal_to_str(s) for asset, s in per_asset.items() if s != 0}
    if unbalanced:
        raise SMTError(
            ErrorCode.LEDGER_UNBALANCED, "journal lines do not sum to zero per asset",
            {"unbalanced": unbalanced},
        )

    prev_hash = await session.scalar(
        select(Journal.hash).order_by(Journal.seq.desc()).limit(1)
    ) or GENESIS_HASH

    journal_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    payload = canonical_payload(
        journal_id=journal_id, type=str(type), created_at=created_at,
        author_type=author_type, author_id=author_id, reason=reason,
        order_id=order_id, fill_id=fill_id, reversed_journal_id=reversed_journal_id,
        lines=[(ln.account_id, ln.quantity, ln.unit_price, ln.price_ccy) for ln in lines],
    )
    journal = Journal(
        id=journal_id, type=type, order_id=order_id, fill_id=fill_id,
        reversed_journal_id=reversed_journal_id, author_type=author_type,
        author_id=author_id, reason=reason, created_at=created_at,
        prev_hash=prev_hash, hash=compute_hash(prev_hash, payload),
    )
    session.add(journal)
    await session.flush()
    session.add_all(
        JournalLine(
            journal_id=journal_id, account_id=ln.account_id, quantity=ln.quantity,
            unit_price=ln.unit_price, price_ccy=ln.price_ccy,
        )
        for ln in lines
    )
    await session.flush()

    # Postgres does the balance arithmetic; sorted ids give a stable row-lock order.
    for account_id in sorted(per_account):
        stmt = insert(AccountBalance).values(account_id=account_id, balance=per_account[account_id])
        stmt = stmt.on_conflict_do_update(
            index_elements=[AccountBalance.account_id],
            set_={
                "balance": AccountBalance.balance + stmt.excluded.balance,
                "updated_at": func.now(),
            },
        ).returning(AccountBalance.balance)
        balance = (await session.execute(stmt)).scalar_one()
        account = accounts[account_id]
        # Only a journal that lowers an account can overdraw it: one that moves an
        # already-negative balance towards zero (e.g. covering a short) is accepted.
        if (
            per_account[account_id] < 0
            and account.portfolio_id is not None
            and account.kind in _NON_NEGATIVE_KINDS
            and account.kind not in allow_negative
            and balance < 0
        ):
            raise SMTError(
                ErrorCode.INSUFFICIENT_FUNDS, "insufficient funds",
                {"account_id": str(account_id), "asset": account.asset,
                 "balance": decimal_to_str(balance)},
            )
    return journal


async def reverse_journal(
    session: AsyncSession,
    journal_id: UUID,
    *,
    reason: str,
    author_type: str,
    author_id: str | None,
    allow_negative: frozenset[AccountKind] = frozenset(),
) -> Journal:
    """Post a REVERSAL journal whose lines negate those of `journal_id`.

    `allow_negative` is passed to post_journal (e.g. {position} to reverse a trade that
    covered a short)."""
    if not reason or not reason.strip():
        raise SMTError(ErrorCode.INVALID_REVERSAL, "a reversal needs a reason")

    # Held before the checks so two concurrent reversals cannot both pass them.
    await _lock_chain(session)
    original_type = await session.scalar(select(Journal.type).where(Journal.id == journal_id))
    if original_type is None:
        raise SMTError(ErrorCode.NOT_FOUND, "journal not found", {"journal_id": str(journal_id)})
    if original_type == JournalType.REVERSAL:
        raise SMTError(
            ErrorCode.INVALID_REVERSAL, "a reversal cannot be reversed",
            {"journal_id": str(journal_id)},
        )
    existing = await session.scalar(
        select(Journal.id).where(Journal.reversed_journal_id == journal_id)
    )
    if existing is not None:
        raise SMTError(
            ErrorCode.ALREADY_REVERSED, "journal already reversed",
            {"journal_id": str(journal_id), "reversal_id": str(existing)},
        )

    original_lines = (
        await session.execute(
            select(
                JournalLine.account_id, JournalLine.quantity,
                JournalLine.unit_price, JournalLine.price_ccy,
            )
            .where(JournalLine.journal_id == journal_id)
            .order_by(JournalLine.id)
        )
    ).all()
    return await post_journal(
        session,
        type=JournalType.REVERSAL,
        lines=[
            LineSpec(account_id, quantity.copy_negate(), unit_price, price_ccy)
            for account_id, quantity, unit_price, price_ccy in original_lines
        ],
        author_type=author_type,
        author_id=author_id,
        reason=reason,
        reversed_journal_id=journal_id,
        allow_negative=allow_negative,
    )


@dataclass
class ChainReport:
    ok: bool
    checked: int
    first_bad_journal_id: UUID | None


_VERIFY_BATCH = 500


async def verify_chain(session: AsyncSession) -> ChainReport:
    """Recompute every journal hash from stored values, in `seq` order.

    A journal is bad when its prev_hash is not the previous journal's hash (GENESIS for
    the first) or its hash does not match the recomputed one. `checked` counts the
    journals examined, including the first bad one.
    """
    # Column selects (not entities) so values come from the database, never from
    # objects already in the session's identity map.
    expected_prev = GENESIS_HASH
    checked = 0
    last_seq: int | None = None
    while True:
        query = select(
            Journal.seq, Journal.id, Journal.type, Journal.created_at, Journal.author_type,
            Journal.author_id, Journal.reason, Journal.order_id, Journal.fill_id,
            Journal.reversed_journal_id, Journal.prev_hash, Journal.hash,
        ).order_by(Journal.seq).limit(_VERIFY_BATCH)
        if last_seq is not None:
            query = query.where(Journal.seq > last_seq)
        batch = (await session.execute(query)).all()
        if not batch:
            return ChainReport(ok=True, checked=checked, first_bad_journal_id=None)

        lines_by_journal: dict[UUID, list] = defaultdict(list)
        for row in await session.execute(
            select(
                JournalLine.journal_id, JournalLine.account_id, JournalLine.quantity,
                JournalLine.unit_price, JournalLine.price_ccy,
            ).where(JournalLine.journal_id.in_([j.id for j in batch]))
        ):
            lines_by_journal[row.journal_id].append(
                (row.account_id, row.quantity, row.unit_price, row.price_ccy)
            )

        for j in batch:
            checked += 1
            payload = canonical_payload(
                journal_id=j.id, type=str(j.type), created_at=j.created_at,
                author_type=j.author_type, author_id=j.author_id, reason=j.reason,
                order_id=j.order_id, fill_id=j.fill_id,
                reversed_journal_id=j.reversed_journal_id,
                lines=lines_by_journal[j.id],
            )
            if j.prev_hash != expected_prev or j.hash != compute_hash(j.prev_hash, payload):
                return ChainReport(ok=False, checked=checked, first_bad_journal_id=j.id)
            expected_prev = j.hash
        last_seq = batch[-1].seq


async def verify_balances(session: AsyncSession) -> list[UUID]:
    """Accounts whose materialized balance differs from the sum of their journal lines."""
    line_sums = (
        select(JournalLine.account_id, func.sum(JournalLine.quantity).label("total"))
        .group_by(JournalLine.account_id)
        .subquery()
    )
    rows = await session.execute(
        select(Account.id)
        .outerjoin(AccountBalance, AccountBalance.account_id == Account.id)
        .outerjoin(line_sums, line_sums.c.account_id == Account.id)
        .where(
            func.coalesce(AccountBalance.balance, 0) != func.coalesce(line_sums.c.total, 0)
        )
        .order_by(Account.id)
    )
    return list(rows.scalars())
