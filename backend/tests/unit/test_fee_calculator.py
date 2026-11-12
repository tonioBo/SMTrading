import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData, Tier, compute_fx_fee, compute_trade_fee
from smtrading.fees.models import FeeScope, FeeSide

D = Decimal


def rule(**kw) -> FeeRuleData:
    for k in ("fixed", "pct", "fx_pct", "min_fee", "max_fee"):
        if isinstance(kw.get(k), str):
            kw[k] = D(kw[k])
    if "tiers" in kw and kw["tiers"] is not None:
        kw["tiers"] = tuple(Tier(up_to=u, pct=p) for u, p in kw["tiers"])
    base = {
        "id": uuid.uuid4(),
        "scope": FeeScope.global_,
        "scope_ref": None,
        "side": FeeSide.both,
        "currency": "EUR",
        "fixed": D(0),
        "pct": D(0),
        "min_fee": None,
        "max_fee": None,
        "tiers": None,
        "fx_pct": D(0),
        "priority": 0,
        "active": True,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return FeeRuleData(**base)


DEFAULT = rule(fixed="1", pct="0.1", min_fee="1", max_fee="20", fx_pct="0.25", currency="EUR")


@pytest.mark.parametrize(
    "notional,exp", [("500", "1.50"), ("10", "1.01"), ("0", "1.00"), ("50000", "20.00")]
)
def test_default_rule(notional, exp):
    assert compute_trade_fee(DEFAULT, D(notional)) == D(exp)


TIERED = rule(
    fixed="0",
    pct="0",
    tiers=((D("1000"), D("0.2")), (D("10000"), D("0.1")), (None, D("0.05"))),
)


@pytest.mark.parametrize("notional,exp", [("1000", "2.00"), ("5000", "5.00"), ("20000", "10.00")])
def test_tiers(notional, exp):
    assert compute_trade_fee(TIERED, D(notional)) == D(exp)


def test_tiers_ignore_rule_pct_and_add_fixed():
    r = rule(fixed="1", pct="9", tiers=((None, D("0.1")),))
    assert compute_trade_fee(r, D("1000")) == D("2.00")


def test_fx_fee():
    assert compute_fx_fee(DEFAULT, D("-1900"), "USD") == D("4.75")


def test_jpy_rounding():
    r = rule(fixed="0", pct="0.1", currency="JPY")
    assert compute_trade_fee(r, D("15050")) == D("15")


def test_negative_notional_rejected():
    with pytest.raises(SMTError) as ei:
        compute_trade_fee(DEFAULT, D("-1"))
    assert ei.value.code == ErrorCode.INVALID_AMOUNT
