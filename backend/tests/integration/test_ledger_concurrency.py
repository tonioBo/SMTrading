import asyncio
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.ledger.hashing import GENESIS_HASH
from smtrading.ledger.models import AccountKind, Journal, JournalType
from smtrading.ledger.service import LineSpec, get_balance, get_or_create_account, post_journal
from smtrading.portfolios.models import Portfolio

DEPOSITS = 20


async def _worker(url: str, name: str) -> UUID:
    engine = make_engine(url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s, s.begin():
            p = Portfolio(name=name)
            s.add(p)
            await s.flush()
            cash = await get_or_create_account(
                s, portfolio_id=p.id, asset="EUR", kind=AccountKind.cash
            )
            eq = await get_or_create_account(
                s, portfolio_id=None, asset="EUR", kind=AccountKind.system_equity
            )
        for _ in range(DEPOSITS):
            async with sm() as s, s.begin():
                await post_journal(
                    s, type=JournalType.DEPOSIT, author_type="admin", author_id=name,
                    lines=[LineSpec(cash.id, Decimal(1)), LineSpec(eq.id, Decimal(-1))],
                )
            await asyncio.sleep(0)
        return cash.id
    finally:
        await engine.dispose()


async def test_concurrent_posts_form_a_single_chain(fresh_db):
    cash_a, cash_b = await asyncio.gather(_worker(fresh_db, "A"), _worker(fresh_db, "B"))

    engine = make_engine(fresh_db)
    try:
        async with async_sessionmaker(engine)() as s:
            total, distinct_prev = (
                await s.execute(
                    select(func.count(), func.count(func.distinct(Journal.prev_hash)))
                )
            ).one()
            assert total == 2 * DEPOSITS
            assert distinct_prev == total

            chain = (
                await s.execute(select(Journal.prev_hash, Journal.hash).order_by(Journal.seq))
            ).all()
            expected_prev = GENESIS_HASH
            for prev_hash, hash_ in chain:
                assert prev_hash == expected_prev
                expected_prev = hash_

            assert await get_balance(s, cash_a) == Decimal(DEPOSITS)
            assert await get_balance(s, cash_b) == Decimal(DEPOSITS)
    finally:
        await engine.dispose()
