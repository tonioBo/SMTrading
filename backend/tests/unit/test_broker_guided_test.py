"""«Prova collegamento»: the guided steps with fake adapters (no network)."""

from datetime import UTC, datetime
from decimal import Decimal as D
from typing import ClassVar

import pytest

from smtrading.brokers.base import AccountSnapshot, BrokerAdapter, BrokerOrder
from smtrading.brokers.guided_test import ORDER_CONFIRM, probe_order, run_guided_test
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.instruments.models import AssetClass, Instrument

OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
AAPL = Instrument(symbol="AAPL", name="Apple", asset_class=AssetClass.us_equity,
                  market="NASDAQ", currency="USD", tick_size=D("0.01"))


class Fake(BrokerAdapter):
    kind: ClassVar[str] = "fake_guided"
    supported_types: ClassVar[frozenset] = frozenset({OrderType.market, OrderType.limit})
    supported_asset_classes: ClassVar[frozenset] = frozenset(AssetClass)

    def __init__(self, **kw) -> None:
        super().__init__({}, None)
        self.auth_error = kw.get("auth_error")
        self.cash = kw.get("cash", D(1000))
        self.reject = kw.get("reject")
        self.after_cancel = kw.get("after_cancel", "canceled")
        self.calls: list[str] = []
        self.placed = []

    async def health_check(self) -> None:
        self.calls.append("health")
        if self.auth_error:
            raise RuntimeError(self.auth_error)

    async def get_account(self) -> AccountSnapshot:
        self.calls.append("account")
        return AccountSnapshot("PA12345678", "cash", "USD", self.cash, 2, 1, None, None, {})

    async def place_order(self, order, instrument) -> BrokerOrder:
        self.calls.append("place")
        self.placed.append(order)
        if self.reject:
            return BrokerOrder("", "rejected", D(0), None, self.reject)
        return BrokerOrder("bo-1", "accepted", D(0), None, None)

    async def cancel_order(self, broker_order_id) -> None:
        self.calls.append(f"cancel:{broker_order_id}")

    async def get_order(self, broker_order_id) -> BrokerOrder:
        self.calls.append("get")
        return BrokerOrder(broker_order_id, self.after_cancel, D(0), None, None)


class MarketOnly(Fake):
    supported_types: ClassVar[frozenset] = frozenset({OrderType.market})


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_quote("AAPL", D("199.90"), D("200.10"), D("200.00"))
    return q


def _by_key(steps):
    return {s.key: s for s in steps}


async def test_read_only_steps_all_ok_and_no_order(qs):
    a = Fake()
    steps = await run_guided_test(a, AAPL, qs, now=OPEN, order_test=False,
                                  kill_switch_on=False)
    assert [s.key for s in steps] == ["auth", "account", "buying_power", "market", "quote"]
    assert all(s.status == "ok" for s in steps)
    by = _by_key(steps)
    assert "••••5678" in by["account"].detail and "PA12345678" not in by["account"].detail
    assert by["buying_power"].detail == "1000 USD disponibili"
    assert by["market"].detail.startswith("NASDAQ: aperto")
    assert by["quote"].detail.startswith("AAPL: 200 USD")
    assert "place" not in a.calls


async def test_failed_auth_skips_the_broker_steps(qs):
    a = Fake(auth_error="credenziali non valide")
    steps = _by_key(await run_guided_test(a, AAPL, qs, now=OPEN, order_test=True,
                                          kill_switch_on=False))
    assert steps["auth"].status == "error" and steps["auth"].detail == "credenziali non valide"
    assert steps["account"].status == steps["buying_power"].status == "skipped"
    assert steps["order"].status == "skipped" and "account" not in a.calls


async def test_no_cash_is_an_error_step(qs):
    steps = _by_key(await run_guided_test(Fake(cash=D(0)), AAPL, qs, now=OPEN,
                                          order_test=False, kill_switch_on=False))
    assert steps["buying_power"].status == "error"


async def test_order_step_places_a_far_limit_and_cancels_it(qs):
    a = Fake()
    steps = _by_key(await run_guided_test(a, AAPL, qs, now=OPEN, order_test=True,
                                          kill_switch_on=False))
    assert steps["order"].status == "ok", steps["order"].detail
    (order,) = a.placed
    assert order.type == OrderType.limit and order.qty == 1 and order.limit_price == D(100)
    assert a.calls[-2:] == ["cancel:bo-1", "get"]
    assert "annullato" in steps["order"].detail


async def test_order_step_reports_a_cancel_not_confirmed(qs, monkeypatch):
    from smtrading.brokers import guided_test

    monkeypatch.setattr(guided_test, "CANCEL_READ_PAUSE_S", 0)
    a = Fake(after_cancel="accepted")
    steps = _by_key(await run_guided_test(a, AAPL, qs, now=OPEN, order_test=True,
                                          kill_switch_on=False))
    assert steps["order"].status == "error" and "non ancora confermato" in steps["order"].detail
    assert a.calls.count("get") == guided_test.CANCEL_READS


async def test_order_step_rejection_is_reported(qs):
    steps = _by_key(await run_guided_test(Fake(reject="market closed"), AAPL, qs, now=OPEN,
                                          order_test=True, kill_switch_on=False))
    assert steps["order"].status == "error" and "market closed" in steps["order"].detail


@pytest.mark.parametrize("adapter,kill,why", [
    (MarketOnly(), False, "ordini limite"),
    (Fake(), True, "kill switch"),
])
async def test_order_step_skipped(qs, adapter, kill, why):
    steps = _by_key(await run_guided_test(adapter, AAPL, qs, now=OPEN, order_test=True,
                                          kill_switch_on=kill))
    assert steps["order"].status == "skipped" and why in steps["order"].detail
    assert "place" not in adapter.calls


async def test_order_step_without_quote_is_skipped():
    a = Fake()
    steps = _by_key(await run_guided_test(a, AAPL, FakeQuoteSource(), now=OPEN,
                                          order_test=True, kill_switch_on=False))
    assert steps["quote"].status == "error" and steps["order"].status == "skipped"
    assert "place" not in a.calls


async def test_live_adapter_is_refused(qs):
    class Live(Fake):
        live: ClassVar[bool] = True

    a = Live()
    with pytest.raises(SMTError) as e:
        await run_guided_test(a, AAPL, qs, now=OPEN, order_test=False, kill_switch_on=False)
    assert e.value.code == ErrorCode.FORBIDDEN and a.calls == []


def test_probe_order_price_never_zero():
    tiny = Instrument(symbol="X", name="X", asset_class=AssetClass.us_equity,
                      market="NASDAQ", currency="USD", tick_size=D("0.01"))
    assert probe_order(tiny, D("0.01")).limit_price == D("0.01")
    assert probe_order(AAPL, D("200.05")).limit_price == D("100.02")
    assert ORDER_CONFIRM == "ORDINE DI PROVA"
