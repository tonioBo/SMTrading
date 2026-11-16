from decimal import Decimal

import pytest

from smtrading.fees.broker_presets import (
    BROKER_KINDS,
    PRESETS,
    preset_for,
    preset_rule_name,
    presets_dto,
)

D = Decimal


@pytest.mark.parametrize(
    ("kind", "broker", "pct", "fx_pct"),
    [
        ("alpaca_paper", "Alpaca", D(0), D(0)),
        ("alpaca_live", "Alpaca", D(0), D(0)),
        ("t212_demo", "Trading 212", D(0), D("0.15")),
        ("t212_live", "Trading 212", D(0), D("0.15")),
        ("etoro_demo", "eToro", D(0), D("0.5")),
        ("etoro_live", "eToro", D(0), D("0.5")),
    ],
)
def test_every_broker_kind_has_its_preset(kind, broker, pct, fx_pct):
    preset = preset_for(kind)
    assert preset is not None
    assert (preset.broker, preset.pct, preset.fixed, preset.fx_pct) == (broker, pct, D(0), fx_pct)
    assert "da verificare" in preset.source_note


def test_unknown_kind_has_no_preset():
    assert preset_for("ibkr_live") is None
    assert preset_for("") is None
    assert preset_for("alpaca") is None  # a family, not a kind


def test_presets_cover_every_kind_and_carry_the_source_note():
    assert set(BROKER_KINDS) == {
        "alpaca_paper", "alpaca_live", "t212_demo", "t212_live", "etoro_demo", "etoro_live",
    }
    assert {p.broker for p in PRESETS.values()} == {"Alpaca", "Trading 212", "eToro"}
    rows = presets_dto()
    assert [r["broker_kind"] for r in rows] == list(BROKER_KINDS)
    t212 = next(r for r in rows if r["broker_kind"] == "t212_demo")
    assert t212["broker"] == "Trading 212"
    assert (t212["pct"], t212["fixed"], t212["fx_pct"]) == ("0", "0", "0.15")
    assert t212["source_note"] == "listino pubblico, da verificare"
    assert t212["rule_name_suffix"] == "Trading 212"


def test_rule_name_is_portfolio_dash_broker_and_fits_the_column():
    assert preset_rule_name("Swing", preset_for("etoro_live")) == "Swing – eToro"
    long = preset_rule_name("x" * 300, preset_for("t212_demo"))
    assert len(long) <= 128 and long.endswith(" – Trading 212")
