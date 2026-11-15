import uuid

import pytest

from smtrading.brokers.models import BrokerLink
from smtrading.portfolios import mode as mode_mod
from smtrading.portfolios.mode import (
    SIMULATED_LABEL,
    PortfolioMode,
    admin_mode_fields,
    mode_fields,
    mode_label,
)
from smtrading.portfolios.models import Portfolio


def _portfolio(linked: bool) -> Portfolio:
    return Portfolio(name="P", broker_link_id=uuid.uuid4() if linked else None)


def _link(kind: str, live_state: str | None = None) -> BrokerLink:
    return BrokerLink(name="L", kind=kind, live_state=live_state)


@pytest.fixture(autouse=True)
def _etoro_live_is_live(monkeypatch):
    """etoro_live joins LIVE_KINDS with Tasks 13-15: emulate it so its label is checked now."""
    monkeypatch.setattr(mode_mod, "LIVE_KINDS", mode_mod.LIVE_KINDS | {"etoro_live"})


@pytest.mark.parametrize(("kind", "live_state", "expected"), [
    (None, None, "SMTrading (simulato)"),
    ("alpaca_paper", None, "Alpaca · prova"),
    ("t212_demo", None, "Trading 212 · demo"),
    ("etoro_demo", None, "eToro · demo"),
    ("alpaca_live", "armed", "🔴 Alpaca · reale"),
    ("alpaca_live", "dry_run", "🔴 Alpaca · reale · prova a secco"),
    ("alpaca_live", "configured", "🔴 Alpaca · reale · sospeso"),
    ("t212_live", "armed", "🔴 Trading 212 · reale"),
    ("etoro_live", "dry_run", "🔴 eToro · reale · prova a secco"),
    ("ibkr_paper", None, "ibkr_paper"),
])
def test_mode_label_table(kind, live_state, expected):
    link = _link(kind, live_state) if kind else None
    assert mode_label(_portfolio(link is not None), link) == expected


def test_simulated_label_constant():
    assert mode_label(_portfolio(False), None) == SIMULATED_LABEL == "SMTrading (simulato)"


@pytest.mark.parametrize(("kind", "live_state"), [
    (None, None), ("alpaca_paper", None), ("t212_live", "dry_run"), ("alpaca_live", "armed"),
])
def test_admin_mode_fields_extend_mode_fields(kind, live_state):
    link = _link(kind, live_state) if kind else None
    p = _portfolio(link is not None)
    out = admin_mode_fields(p, link)
    assert out == {**mode_fields(p, link), "broker_kind": kind, "mode_label": mode_label(p, link)}
    assert isinstance(out["mode"], PortfolioMode)


def test_mode_counts_grouped_and_ordered():
    """Simulated first, then practice/demo alphabetically, then real-money (alphabetically)."""
    sim = (_portfolio(False), None)
    rows = [
        (_portfolio(True), _link("t212_live", "armed")),
        (_portfolio(True), _link("t212_demo")),
        sim,
        (_portfolio(True), _link("alpaca_paper")),
        (_portfolio(True), _link("alpaca_live", "configured")),
        sim,
        (_portfolio(True), _link("t212_demo")),
    ]
    assert mode_mod.mode_counts(rows) == [
        {"label": "SMTrading (simulato)", "count": 2, "live": False},
        {"label": "Alpaca · prova", "count": 1, "live": False},
        {"label": "Trading 212 · demo", "count": 2, "live": False},
        {"label": "🔴 Alpaca · reale · sospeso", "count": 1, "live": True},
        {"label": "🔴 Trading 212 · reale", "count": 1, "live": True},
    ]
    assert mode_mod.mode_counts([]) == []
