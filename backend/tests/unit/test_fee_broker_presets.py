from decimal import Decimal

import pytest

from smtrading.fees.broker_presets import (
    _ALPACA_CRYPTO,
    _ALPACA_STOCKS,
    _ETORO_CRYPTO,
    _ETORO_STOCKS,
    _T212_SOURCE,
    BROKER_KINDS,
    PRESETS,
    preset_for,
    preset_rule_name,
    presets_dto,
)

D = Decimal


def _entries(kind):
    return [(e.asset_class, e.pct, e.fixed, e.fx_pct, e.source_note)
            for e in preset_for(kind).entries]


@pytest.mark.parametrize("kind", ["alpaca_paper", "alpaca_live"])
def test_alpaca_stocks_free_crypto_quarter_percent(kind):
    assert preset_for(kind).broker == "Alpaca"
    assert _entries(kind) == [
        (None, D(0), D(0), D(0), _ALPACA_STOCKS),
        ("crypto", D("0.25"), D(0), D(0), _ALPACA_CRYPTO),
    ]


@pytest.mark.parametrize("kind", ["t212_demo", "t212_live"])
def test_t212_one_rule_fx_015(kind):
    assert preset_for(kind).broker == "Trading 212"
    assert _entries(kind) == [(None, D(0), D(0), D("0.15"), _T212_SOURCE)]


@pytest.mark.parametrize("kind", ["etoro_demo", "etoro_live"])
def test_etoro_stocks_and_crypto_fx_075(kind):
    assert preset_for(kind).broker == "eToro"
    assert _entries(kind) == [
        (None, D(0), D(0), D("0.75"), _ETORO_STOCKS),
        ("crypto", D(1), D(0), D("0.75"), _ETORO_CRYPTO),
    ]


def test_unknown_kind_has_no_preset():
    assert preset_for("ibkr_live") is None
    assert preset_for("") is None
    assert preset_for("alpaca") is None  # a family, not a kind


def test_presets_cover_every_kind_and_carry_the_notes():
    assert set(BROKER_KINDS) == {
        "alpaca_paper", "alpaca_live", "t212_demo", "t212_live", "etoro_demo", "etoro_live",
    }
    assert {p.broker for p in PRESETS.values()} == {"Alpaca", "Trading 212", "eToro"}
    rows = presets_dto()
    assert [r["broker_kind"] for r in rows] == list(BROKER_KINDS)
    etoro = next(r for r in rows if r["broker_kind"] == "etoro_demo")
    assert etoro["broker"] == "eToro"
    assert [(e["asset_class"], e["pct"], e["fixed"], e["fx_pct"]) for e in etoro["entries"]] == [
        (None, "0", "0", "0.75"), ("crypto", "1", "0", "0.75")]
    assert [e["source_note"] for e in etoro["entries"]] == [_ETORO_STOCKS, _ETORO_CRYPTO]
    assert all(e["note"] for e in etoro["entries"])


def test_every_source_note_names_its_check_date_and_official_source():
    """Each figure records where and when it was checked; what could not be confirmed on
    an official page keeps «da verificare» (only the eToro stock commission)."""
    notes = [e.source_note for p in PRESETS.values() for e in p.entries]
    assert all("18/11/2026" in n for n in notes)
    assert all(any(h in n for h in ("alpaca.markets", "trading212.com", "etoro.com"))
               for n in notes)
    assert [n for n in notes if "da verificare" in n] == [_ETORO_STOCKS]


def test_rule_names_with_and_without_class_fit_the_column():
    etoro = preset_for("etoro_live")
    assert preset_rule_name("Swing", etoro) == "Swing – eToro"
    assert preset_rule_name("Swing", etoro, "crypto") == "Swing – eToro · Crypto"
    long = preset_rule_name("x" * 300, preset_for("t212_demo"))
    assert len(long) <= 128 and long.endswith(" – Trading 212")
    long = preset_rule_name("x" * 300, etoro, "crypto")
    assert len(long) <= 128 and long.endswith(" – eToro · Crypto")
