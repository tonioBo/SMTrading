import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData
from smtrading.fees.models import FeeScope, FeeSide
from smtrading.fees.resolver import resolve_rule
from smtrading.instruments.models import AssetClass

PID = uuid.uuid4()
NOW = datetime.now(UTC)


def rule(**kw) -> FeeRuleData:
    base = {
        "id": uuid.uuid4(),
        "scope": FeeScope.global_,
        "scope_ref": None,
        "side": FeeSide.both,
        "currency": "EUR",
        "fixed": Decimal(0),
        "pct": Decimal(0),
        "min_fee": None,
        "max_fee": None,
        "tiers": None,
        "fx_pct": Decimal(0),
        "priority": 0,
        "active": True,
        "created_at": NOW,
    }
    base.update(kw)
    return FeeRuleData(**base)


def resolve(rules, side="buy", asset_class=AssetClass.us_equity):
    return resolve_rule(rules, portfolio_id=PID, asset_class=asset_class, side=side)


def test_portfolio_beats_asset_class_beats_global():
    g = rule()
    a = rule(scope=FeeScope.asset_class, scope_ref="us_equity")
    p = rule(scope=FeeScope.portfolio, scope_ref=str(PID))
    assert resolve([g, a, p]) is p
    assert resolve([g, a]) is a
    assert resolve([g]) is g


def test_scope_ref_must_match():
    g = rule()
    other_class = rule(scope=FeeScope.asset_class, scope_ref="crypto")
    other_pf = rule(scope=FeeScope.portfolio, scope_ref=str(uuid.uuid4()))
    assert resolve([g, other_class, other_pf]) is g


def test_side_filter():
    g = rule()
    sell_only = rule(scope=FeeScope.portfolio, scope_ref=str(PID), side=FeeSide.sell)
    assert resolve([g, sell_only], side="buy") is g
    assert resolve([g, sell_only], side="sell") is sell_only


def test_inactive_ignored():
    g = rule()
    off = rule(scope=FeeScope.portfolio, scope_ref=str(PID), active=False)
    assert resolve([g, off]) is g


def test_priority_breaks_tie():
    lo = rule(priority=1)
    hi = rule(priority=5)
    assert resolve([lo, hi]) is hi


def test_recency_breaks_priority_tie():
    old = rule(created_at=NOW - timedelta(days=1))
    new = rule(created_at=NOW)
    assert resolve([old, new]) is new


def test_none_found():
    with pytest.raises(SMTError) as ei:
        resolve([rule(active=False)])
    assert ei.value.code == ErrorCode.FEE_RULE_NOT_FOUND


def test_portfolio_asset_class_rule_beats_portfolio_rule_for_its_class_only():
    g = rule()
    a = rule(scope=FeeScope.asset_class, scope_ref="crypto")
    p = rule(scope=FeeScope.portfolio, scope_ref=str(PID))
    pc = rule(scope=FeeScope.portfolio, scope_ref=str(PID), asset_class="crypto")
    crypto, stocks = AssetClass.crypto, AssetClass.us_equity
    assert resolve([g, a, p, pc], asset_class=crypto) is pc
    assert resolve([g, a, p, pc], asset_class=stocks) is p
    # without the plain portfolio rule: the class-filtered one never covers other classes
    assert resolve([g, a, pc], asset_class=stocks) is g
    assert resolve([g, a, pc], asset_class=crypto) is pc
    assert resolve([g, a, p], asset_class=crypto) is p
    assert resolve([g, a], asset_class=crypto) is a


def test_portfolio_asset_class_rule_of_another_portfolio_or_inactive_is_ignored():
    g = rule()
    other = rule(scope=FeeScope.portfolio, scope_ref=str(uuid.uuid4()), asset_class="crypto")
    off = rule(scope=FeeScope.portfolio, scope_ref=str(PID), asset_class="crypto", active=False)
    assert resolve([g, other, off], asset_class=AssetClass.crypto) is g


def test_priority_and_recency_ties_within_portfolio_asset_class():
    lo = rule(scope=FeeScope.portfolio, scope_ref=str(PID), asset_class="crypto", priority=1)
    hi = rule(scope=FeeScope.portfolio, scope_ref=str(PID), asset_class="crypto", priority=5)
    assert resolve([lo, hi], asset_class=AssetClass.crypto) is hi
    old = rule(scope=FeeScope.portfolio, scope_ref=str(PID), asset_class="crypto",
               created_at=NOW - timedelta(days=1))
    new = rule(scope=FeeScope.portfolio, scope_ref=str(PID), asset_class="crypto")
    assert resolve([old, new], asset_class=AssetClass.crypto) is new
