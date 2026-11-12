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


def _invalid(message: str, details: dict | None = None) -> SMTError:
    return SMTError(ErrorCode.LEDGER_INVALID, message, details)


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
    if any(ln.quantity == 0 for ln in lines):
        raise _invalid("journal lines must have a non-zero quantity")

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

    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CHAIN_LOCK_KEY})
    prev_hash = await session.scalar(
        select(Journal.hash).order_by(Journal.seq.desc()).limit(1)
    ) or GENESIS_HASH

    journal_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    payload = canonical_payload(
        journal_id=journal_id, type=str(type), created_at=created_at,
        author_type=author_type, author_id=author_id, reason=reason,
        order_id=order_id, fill_id=fill_id, reversed_journal_id=reversed_journal_id,
        lines=[(ln.account_id, ln.quantity) for ln in lines],
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
        if (
            account.portfolio_id is not None
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
