from decimal import Decimal

from smtrading.fees.models import FeeRule, FeeScope, FeeSide
from smtrading.fees.resolver import load_rules, quote_fee
from smtrading.instruments.models import AssetClass


async def test_quote_fee_from_db(db_session, portfolio):
    db_session.add(
        FeeRule(
            name="default",
            scope=FeeScope.global_,
            side=FeeSide.both,
            currency="EUR",
            fixed=Decimal(1),
            pct=Decimal("0.1"),
            min_fee=Decimal(1),
            max_fee=Decimal(20),
        )
    )
    pf_rule = FeeRule(
        name="pf",
        scope=FeeScope.portfolio,
        scope_ref=str(portfolio.id),
        side=FeeSide.both,
        currency="EUR",
        pct=Decimal("0.05"),
    )
    db_session.add(pf_rule)
    await db_session.flush()

    q = await quote_fee(
        db_session,
        portfolio_id=portfolio.id,
        asset_class=AssetClass.us_equity,
        side="buy",
        notional=Decimal(1000),
    )
    assert q.rule_id == pf_rule.id
    assert q.amount == Decimal("0.50")
    assert q.currency == "EUR"


async def test_load_rules_converts_tiers(db_session):
    db_session.add(
        FeeRule(
            name="tiered",
            scope=FeeScope.global_,
            tiers=[{"up_to": "1000", "pct": "0.2"}, {"up_to": None, "pct": "0.1"}],
        )
    )
    db_session.add(FeeRule(name="empty", scope=FeeScope.global_, tiers=[]))
    await db_session.flush()
    rules = {r for r in await load_rules(db_session)}
    tiered = [r for r in rules if r.tiers]
    assert len(tiered) == 1
    assert tiered[0].tiers[0].up_to == Decimal(1000)
    assert tiered[0].tiers[1].up_to is None
    assert tiered[0].tiers[1].pct == Decimal("0.1")
    assert any(r.tiers is None for r in rules)
