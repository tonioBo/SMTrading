import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.ledger.models import Journal, JournalType
from smtrading.portfolios.service import (
    create_portfolio,
    deposit,
    get_cash_balances,
    withdraw,
)


async def _journal_count(session):
    return await session.scalar(select(func.count()).select_from(Journal))


@pytest.fixture
async def funded(db_session):
    return await create_portfolio(db_session, name="Funded", initial_cash=Decimal(1000))


async def test_create_portfolio_with_initial_cash(db_session):
    p = await create_portfolio(db_session, name="Demo", initial_cash=Decimal(100000))
    assert await get_cash_balances(db_session, p.id) == {"EUR": Decimal(100000)}


async def test_create_portfolio_zero_cash_has_no_journal(db_session):
    before = await _journal_count(db_session)
    p = await create_portfolio(db_session, name="Empty", initial_cash=Decimal(0))
    assert await get_cash_balances(db_session, p.id) == {}
    assert await _journal_count(db_session) == before


async def test_create_portfolio_negative_cash(db_session):
    with pytest.raises(SMTError) as e:
        await create_portfolio(db_session, name="Neg", initial_cash=Decimal(-1))
    assert e.value.code == ErrorCode.INVALID_AMOUNT


async def test_create_portfolio_duplicate_name(db_session, funded):
    with pytest.raises(SMTError) as e:
        await create_portfolio(db_session, name="Funded", initial_cash=Decimal(1))
    assert e.value.code == ErrorCode.INVALID_ORDER


@pytest.mark.parametrize("amt", ["0", "-5", "0.001"])
async def test_deposit_invalid_amount(db_session, funded, amt):
    before = await _journal_count(db_session)
    with pytest.raises(SMTError) as e:
        await deposit(db_session, funded.id, Decimal(amt), "EUR", author_id="a")
    assert e.value.code == ErrorCode.INVALID_AMOUNT
    assert await _journal_count(db_session) == before


async def test_deposit_other_currency_creates_usd_account(db_session, funded):
    j = await deposit(db_session, funded.id, Decimal(500), " usd ", author_id="a", reason="fx")
    assert j.type == JournalType.DEPOSIT and j.author_type == "admin"
    assert await get_cash_balances(db_session, funded.id) == {
        "EUR": Decimal(1000), "USD": Decimal(500),
    }


async def test_withdraw(db_session, funded):
    j = await withdraw(db_session, funded.id, Decimal(400), "EUR", author_id="a")
    assert j.type == JournalType.WITHDRAWAL
    assert await get_cash_balances(db_session, funded.id) == {"EUR": Decimal(600)}


async def test_withdraw_more_than_balance(db_session, funded):
    with pytest.raises(SMTError) as e:
        await withdraw(db_session, funded.id, Decimal("1000.01"), "EUR", author_id="a")
    assert e.value.code == ErrorCode.INSUFFICIENT_FUNDS


async def test_deposit_rounds_to_minor_units(db_session, funded):
    await deposit(db_session, funded.id, Decimal("10.005"), "EUR", author_id=None)
    assert (await get_cash_balances(db_session, funded.id))["EUR"] == Decimal("1010.01")


async def test_deposit_jpy_has_no_decimals(db_session, funded):
    await deposit(db_session, funded.id, Decimal("100.5"), "JPY", author_id=None)
    assert (await get_cash_balances(db_session, funded.id))["JPY"] == Decimal(101)


async def test_unknown_portfolio(db_session):
    for op in (deposit, withdraw):
        with pytest.raises(SMTError) as e:
            await op(db_session, uuid.uuid4(), Decimal(1), "EUR", author_id=None)
        assert e.value.code == ErrorCode.NOT_FOUND
