import uuid
from decimal import Decimal, localcontext

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.ledger.hashing import GENESIS_HASH, canonical_payload, compute_hash
from smtrading.ledger.models import AccountKind, Journal, JournalLine, JournalType
from smtrading.ledger.service import (
    LineSpec,
    get_balance,
    get_or_create_account,
    post_journal,
)


async def _post(session, lines, **kw):
    kw.setdefault("type", JournalType.DEPOSIT)
    return await post_journal(session, author_type="admin", author_id="1", lines=lines, **kw)


async def _deposit(session, cash, eq, amount):
    return await _post(session, [LineSpec(cash.id, amount), LineSpec(eq.id, -amount)])


async def test_balanced_deposit_updates_balances(db_session, portfolio):
    cash = await get_or_create_account(
        db_session, portfolio_id=portfolio.id, asset="EUR", kind=AccountKind.cash
    )
    eq = await get_or_create_account(
        db_session, portfolio_id=None, asset="EUR", kind=AccountKind.system_equity
    )
    j = await post_journal(
        db_session, type=JournalType.DEPOSIT, author_type="admin", author_id="1",
        lines=[LineSpec(cash.id, Decimal(1000)), LineSpec(eq.id, Decimal(-1000))],
    )
    assert await get_balance(db_session, cash.id) == Decimal(1000)
    assert await get_balance(db_session, eq.id) == Decimal(-1000)
    assert j.prev_hash == GENESIS_HASH and len(j.hash) == 64


async def test_unbalanced_rejected(db_session, cash_and_equity):
    cash, eq = cash_and_equity
    with pytest.raises(SMTError) as e:
        await _post(db_session, [LineSpec(cash.id, Decimal(10)), LineSpec(eq.id, Decimal(-9))])
    assert e.value.code == ErrorCode.LEDGER_UNBALANCED


async def test_unbalanced_per_asset_rejected(db_session, portfolio, cash_and_equity):
    cash, _ = cash_and_equity
    usd = await get_or_create_account(
        db_session, portfolio_id=portfolio.id, asset="USD", kind=AccountKind.cash
    )
    # Sums to zero overall, but not per asset.
    with pytest.raises(SMTError) as e:
        await _post(db_session, [LineSpec(cash.id, Decimal(10)), LineSpec(usd.id, Decimal(-10))])
    assert e.value.code == ErrorCode.LEDGER_UNBALANCED


@pytest.mark.parametrize("case", ["single", "zero", "empty"])
async def test_single_or_zero_line_rejected(db_session, cash_and_equity, case):
    cash, eq = cash_and_equity
    lines = {
        "single": [LineSpec(cash.id, Decimal(1))],
        "zero": [
            LineSpec(cash.id, Decimal(5)), LineSpec(eq.id, Decimal(-5)),
            LineSpec(eq.id, Decimal(0)),
        ],
        "empty": [],
    }[case]
    with pytest.raises(SMTError) as e:
        await _post(db_session, lines)
    assert e.value.code == ErrorCode.LEDGER_INVALID


async def test_unknown_account_rejected(db_session, cash_and_equity):
    cash, _ = cash_and_equity
    with pytest.raises(SMTError) as e:
        await _post(
            db_session, [LineSpec(cash.id, Decimal(1)), LineSpec(uuid.uuid4(), Decimal(-1))]
        )
    assert e.value.code == ErrorCode.LEDGER_INVALID


async def test_negative_cash_rejected(db_session, cash_and_equity):
    cash, eq = cash_and_equity
    await _deposit(db_session, cash, eq, Decimal(100))
    with pytest.raises(SMTError) as e:
        await _post(
            db_session,
            [LineSpec(cash.id, Decimal("-100.01")), LineSpec(eq.id, Decimal("100.01"))],
            type=JournalType.WITHDRAWAL,
        )
    assert e.value.code == ErrorCode.INSUFFICIENT_FUNDS
    assert e.value.details["account_id"] == str(cash.id)
    assert e.value.details["asset"] == "EUR"
    assert Decimal(e.value.details["balance"]) == Decimal("-0.01")


async def test_system_account_may_go_negative(db_session, cash_and_equity):
    cash, eq = cash_and_equity
    await _deposit(db_session, cash, eq, Decimal(1000))
    assert await get_balance(db_session, eq.id) == Decimal(-1000)


async def test_allow_negative_position(db_session, portfolio):
    pos = await get_or_create_account(
        db_session, portfolio_id=portfolio.id, asset="AAPL", kind=AccountKind.position
    )
    other = await get_or_create_account(
        db_session, portfolio_id=None, asset="AAPL", kind=AccountKind.system_equity
    )
    lines = [LineSpec(pos.id, Decimal(-5)), LineSpec(other.id, Decimal(5))]
    async with db_session.begin_nested() as sp:
        with pytest.raises(SMTError) as e:
            await _post(db_session, lines, type=JournalType.TRADE)
        assert e.value.code == ErrorCode.INSUFFICIENT_FUNDS
        await sp.rollback()
    await _post(
        db_session, lines, type=JournalType.TRADE, allow_negative=frozenset({AccountKind.position})
    )
    assert await get_balance(db_session, pos.id) == Decimal(-5)


async def test_margin_loan_may_go_negative(db_session, portfolio, cash_and_equity):
    cash, _ = cash_and_equity
    loan = await get_or_create_account(
        db_session, portfolio_id=portfolio.id, asset="EUR", kind=AccountKind.margin_loan
    )
    await _post(db_session, [LineSpec(cash.id, Decimal(50)), LineSpec(loan.id, Decimal(-50))])
    assert await get_balance(db_session, loan.id) == Decimal(-50)


async def test_same_account_twice_in_one_journal(db_session, cash_and_equity):
    cash, eq = cash_and_equity
    await _post(
        db_session,
        [LineSpec(cash.id, Decimal(30)), LineSpec(cash.id, Decimal(20)),
         LineSpec(eq.id, Decimal(-50))],
    )
    assert await get_balance(db_session, cash.id) == Decimal(50)


async def test_high_precision_quantities(db_session, cash_and_equity):
    cash, eq = cash_and_equity
    # 38 significant digits: beyond the default 28-digit context (hence copy_negate, not -big).
    big = Decimal("12345678901234567890.123456789012345678")
    await _post(db_session, [LineSpec(cash.id, big), LineSpec(eq.id, big.copy_negate())])
    await _post(
        db_session,
        [LineSpec(cash.id, Decimal("-0.000000000000000001")),
         LineSpec(eq.id, Decimal("0.000000000000000001"))],
        type=JournalType.WITHDRAWAL,
    )
    with localcontext() as ctx:
        ctx.prec = 60
        expected = big - Decimal("0.000000000000000001")
    assert await get_balance(db_session, cash.id) == expected


async def test_get_balance_zero_when_missing(db_session, cash_and_equity):
    cash, _ = cash_and_equity
    assert await get_balance(db_session, cash.id) == Decimal(0)


async def test_chain_links(db_session, cash_and_equity):
    cash, eq = cash_and_equity
    j1 = await _deposit(db_session, cash, eq, Decimal(1))
    j2 = await _deposit(db_session, cash, eq, Decimal(2))
    assert j1.prev_hash == GENESIS_HASH
    assert j2.prev_hash == j1.hash
    assert j2.seq > j1.seq


async def test_hash_round_trip_from_db(db_session, cash_and_equity):
    cash, eq = cash_and_equity
    j = await _post(
        db_session,
        [
            LineSpec(cash.id, Decimal("12.5"), Decimal("1.1"), "USD"),
            LineSpec(eq.id, Decimal("-12.5")),
        ],
        reason="round trip",
    )
    jid, stored_hash, prev = j.id, j.hash, j.prev_hash
    db_session.expire_all()
    row = (await db_session.execute(select(Journal).where(Journal.id == jid))).scalar_one()
    db_lines = (
        await db_session.execute(select(JournalLine).where(JournalLine.journal_id == jid))
    ).scalars().all()
    payload = canonical_payload(
        journal_id=row.id, type=str(row.type), created_at=row.created_at,
        author_type=row.author_type, author_id=row.author_id, reason=row.reason,
        order_id=row.order_id, fill_id=row.fill_id, reversed_journal_id=row.reversed_journal_id,
        lines=[(ln.account_id, ln.quantity) for ln in db_lines],
    )
    assert row.hash == stored_hash
    assert compute_hash(prev, payload) == stored_hash
    assert {(ln.unit_price, ln.price_ccy) for ln in db_lines} == {
        (Decimal("1.1"), "USD"), (None, None)
    }


async def test_get_or_create_account_idempotent(db_session, portfolio):
    a = await get_or_create_account(
        db_session, portfolio_id=portfolio.id, asset="EUR", kind=AccountKind.cash
    )
    b = await get_or_create_account(
        db_session, portfolio_id=portfolio.id, asset="EUR", kind=AccountKind.cash
    )
    s1 = await get_or_create_account(
        db_session, portfolio_id=None, asset="EUR", kind=AccountKind.system_equity
    )
    s2 = await get_or_create_account(
        db_session, portfolio_id=None, asset="EUR", kind=AccountKind.system_equity
    )
    assert a.id == b.id and s1.id == s2.id and a.id != s1.id


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.lists(
        st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("1e9"), places=6),
        min_size=1, max_size=10,
    )
)
async def test_random_deposits_balance_equals_sum(db_session, cash_and_equity, amounts):
    cash, eq = cash_and_equity
    sp = await db_session.begin_nested()
    try:
        for amount in amounts:
            await _deposit(db_session, cash, eq, amount)
        with localcontext() as ctx:
            ctx.prec = 60
            total = sum(amounts, Decimal(0))
        assert await get_balance(db_session, cash.id) == total
        assert await get_balance(db_session, eq.id) == -total
    finally:
        await sp.rollback()
