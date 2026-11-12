from decimal import Decimal

from sqlalchemy import select

from smtrading.fees.models import FeeScope
from smtrading.fees.resolver import load_rules
from smtrading.instruments.models import AssetClass
from smtrading.instruments.service import get_instrument
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import get_cash_balances
from smtrading.seed import run_seed


async def test_seed_idempotent(db_session):
    await run_seed(db_session)
    await run_seed(db_session)
    demo = (
        (await db_session.execute(select(Portfolio).where(Portfolio.name == "Demo")))
        .scalars()
        .all()
    )
    assert len(demo) == 1
    assert await get_cash_balances(db_session, demo[0].id) == {"EUR": Decimal(100000)}
    btc = await get_instrument(db_session, "btc-usd")
    assert btc.asset_class == AssetClass.crypto
    assert btc.shortable is False
    assert (await get_instrument(db_session, "AAPL")).shortable is True
    rules = await load_rules(db_session)
    assert len([r for r in rules if r.scope == FeeScope.global_]) == 1
