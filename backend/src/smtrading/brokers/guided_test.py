"""«Prova collegamento»: a guided check of a paper/demo broker link, step by step.

Read-only steps, in order (each reported in Italian, never raising): authentication
(`health_check`), account read (`get_account`, GET only), buying power (from that
snapshot), market status of the sample instrument (the local exchange calendar, no I/O)
and a quote for it (the portal's quote source). A failed authentication skips the broker
steps that need it.

Optional, only with the explicit second confirmation (ORDER_CONFIRM): a tiny order on
the paper/demo account and its immediate cancel: a limit buy of one unit at half the
current price (it cannot fill), sent with the adapter's own `place_order`, canceled with
`cancel_order` and read back with `get_order`. Skipped when the adapter takes no limit
order (eToro: market only, it would execute), without a usable quote, or with the kill
switch on. The test order lives only at the broker: no local order, portfolio or ledger
row is touched.

Never for real money: `run_guided_test` refuses a live adapter (the admin route refuses
LIVE_KINDS links before any adapter exists; the live flow has its own read-only test).
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

from smtrading.brokers.base import AccountSnapshot, BrokerAdapter, BrokerOrder
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import Order, OrderSide, OrderType, TimeInForce
from smtrading.engine.quotesource import QuoteSource
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument

ORDER_CONFIRM = "ORDINE DI PROVA"
STEP_TIMEOUT_S = 20.0
CANCEL_READS = 3
CANCEL_READ_PAUSE_S = 1.0
LIVE_REFUSED = "prova guidata solo su conti paper/demo: mai su denaro reale"

StepStatus = Literal["ok", "error", "skipped"]


@dataclass
class Step:
    key: str
    label: str
    status: StepStatus
    detail: str

    def dto(self) -> dict[str, Any]:
        return asdict(self)


def _num(v: Decimal) -> str:
    return decimal_to_str(v).replace(".", ",")


def _masked(account_id: str) -> str:
    return "••••" + account_id[-4:] if len(account_id) > 4 else "••••"


def probe_order(instrument: Instrument, reference: Decimal) -> Order:
    """A limit buy of one unit at half `reference`, rounded down to the instrument's tick
    and to the cent (brokers refuse sub-penny prices at or above 1), never 0."""
    tick = max(instrument.tick_size or Decimal("0.01"), Decimal("0.01"))
    price = ((reference / 2 / tick).to_integral_value(rounding=ROUND_DOWN) * tick).quantize(
        tick, rounding=ROUND_DOWN)
    return Order(
        id=uuid.uuid4(), symbol=instrument.symbol, side=OrderSide.buy,
        type=OrderType.limit, tif=TimeInForce.day, qty=Decimal(1),
        limit_price=max(price, tick), filled_qty=Decimal(0), broker_attempt=0,
    )


async def _timed(call: Awaitable):
    return await asyncio.wait_for(call, STEP_TIMEOUT_S)


async def run_guided_test(
    adapter: BrokerAdapter, instrument: Instrument | None, qs: QuoteSource, *,
    now: datetime, order_test: bool, kill_switch_on: bool,
    error_text: Callable[[Exception], str] = lambda e: str(e) or type(e).__name__,
) -> list[Step]:
    """The steps' outcomes (module docstring). `error_text` turns a failure into the text
    shown (the caller redacts the link's secrets)."""
    if adapter.live:  # defense in depth: the route refuses live links first
        raise SMTError(ErrorCode.FORBIDDEN, LIVE_REFUSED)
    steps: list[Step] = []

    def add(key: str, label: str, status: StepStatus, detail: str) -> None:
        steps.append(Step(key, label, status, detail))

    try:
        await _timed(adapter.health_check())
        add("auth", "Autenticazione", "ok", "credenziali accettate dal broker")
        auth = True
    except Exception as e:  # noqa: BLE001 - reported as the step's outcome
        add("auth", "Autenticazione", "error", error_text(e))
        auth = False

    snap: AccountSnapshot | None = None
    if not auth:
        add("account", "Lettura del conto", "skipped", "autenticazione non riuscita")
        add("buying_power", "Potere d'acquisto", "skipped", "autenticazione non riuscita")
    else:
        try:
            snap = await _timed(adapter.get_account())
            kind = f", conto {snap.account_type}" if snap.account_type else ""
            add("account", "Lettura del conto", "ok",
                f"conto {_masked(snap.account_id)} in {snap.currency}{kind}: "
                f"{snap.positions_count} posizioni, {snap.open_orders_count} ordini aperti")
        except Exception as e:  # noqa: BLE001
            add("account", "Lettura del conto", "error", error_text(e))
        if snap is None:
            add("buying_power", "Potere d'acquisto", "skipped", "conto non letto")
        elif snap.cash_available > 0:
            add("buying_power", "Potere d'acquisto", "ok",
                f"{_num(snap.cash_available)} {snap.currency} disponibili")
        else:
            add("buying_power", "Potere d'acquisto", "error",
                f"nessuna liquidità disponibile (0 {snap.currency}): versa fondi sul conto "
                "di prova")

    reference: Decimal | None = None
    if instrument is None:
        add("market", "Stato del mercato", "skipped", "nessuno strumento di esempio")
        add("quote", "Quotazione di esempio", "skipped", "nessuno strumento di esempio")
    else:
        try:
            status = market_status(instrument.market, now)
            when = status.next_close if status.is_open else status.next_open
            text = "aperto" if status.is_open else "chiuso"
            if when is not None:
                text += (" (chiude" if status.is_open else " (apre") + \
                    f" {when.strftime('%d/%m %H:%M')} UTC)"
            add("market", "Stato del mercato", "ok", f"{instrument.market}: {text}")
        except Exception as e:  # noqa: BLE001
            add("market", "Stato del mercato", "error", error_text(e))
        try:
            quote = (await _timed(qs.get_quotes([instrument.symbol], max_age_s=60)))[
                instrument.symbol]
            reference = quote.last or quote.bid or quote.ask
            if reference is None or reference <= 0:
                add("quote", "Quotazione di esempio", "error",
                    f"{instrument.symbol}: nessun prezzo nella quotazione")
                reference = None
            else:
                add("quote", "Quotazione di esempio", "ok",
                    f"{instrument.symbol}: {_num(reference)} {instrument.currency}"
                    f" ({quote.provider})")
        except Exception as e:  # noqa: BLE001
            add("quote", "Quotazione di esempio", "error", error_text(e))

    if order_test:
        await _order_step(adapter, instrument, reference, auth=auth,
                          kill_switch_on=kill_switch_on, add=add, error_text=error_text)
    return steps


async def _order_step(adapter, instrument, reference, *, auth, kill_switch_on, add,
                      error_text) -> None:
    label = "Ordine di prova e annullamento"

    def skip(why: str) -> None:
        add("order", label, "skipped", why)

    if kill_switch_on:
        return skip("kill switch attivo: nessun ordine di prova inviato")
    if not auth:
        return skip("autenticazione non riuscita")
    if instrument is None or reference is None:
        return skip("serve una quotazione dello strumento di esempio")
    order = probe_order(instrument, reference)
    try:
        await _timed(adapter.prepare([instrument]))
        supported = adapter.supports(order, instrument)
    except Exception as e:  # noqa: BLE001
        return add("order", label, "error", f"broker non pronto: {error_text(e)}")
    if not supported:
        return skip("il broker non accetta ordini limite su questo strumento: un ordine "
                    "a mercato verrebbe eseguito, quindi non viene inviato")
    what = (f"acquisto limite di 1 {instrument.symbol} a {_num(order.limit_price)} "
            f"{instrument.currency}")
    try:
        placed: BrokerOrder = await _timed(adapter.place_order(order, instrument))
    except Exception as e:  # noqa: BLE001
        return add("order", label, "error",
                   f"{what}: esito incerto ({error_text(e)}); controlla gli ordini aperti "
                   "sul conto di prova e annulla a mano quello con riferimento "
                   f"{adapter.client_ref(order)}")
    if placed.status == "rejected":
        return add("order", label, "error",
                   f"{what}: rifiutato dal broker ({placed.message or 'nessun motivo'})")
    bid = placed.broker_order_id
    try:
        await _timed(adapter.cancel_order(bid))
        final = placed
        for i in range(CANCEL_READS):
            final = await _timed(adapter.get_order(bid))
            if final.status in ("canceled", "expired", "rejected", "filled"):
                break
            if i < CANCEL_READS - 1:
                await asyncio.sleep(CANCEL_READ_PAUSE_S)
    except Exception as e:  # noqa: BLE001
        return add("order", label, "error",
                   f"{what}: inviato (id {bid}) ma annullamento non confermato "
                   f"({error_text(e)}); annullalo a mano sul conto di prova")
    if final.filled_qty > 0 or final.status == "filled":
        return add("order", label, "error",
                   f"{what}: eseguito per {_num(final.filled_qty)} prima dell'annullamento; "
                   "verifica il conto di prova")
    if final.status in ("canceled", "expired"):
        return add("order", label, "ok", f"{what}: accettato (id {bid}) e annullato")
    add("order", label, "error",
        f"{what}: accettato (id {bid}), annullamento richiesto ma non ancora confermato "
        f"(stato {final.status}); controlla il conto di prova")
