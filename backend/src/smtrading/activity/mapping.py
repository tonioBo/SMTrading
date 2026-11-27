"""Pure: domain events and notifications -> ActivityDraft (type, level, direct, summary).

`direct` marks what SMTrading sends to Telegram by itself (critical and executed
facts). Summaries are short Italian sentences built from the payload; a missing key
never raises (the hook runs inside business transactions).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from smtrading.activity.models import ActivityLevel
from smtrading.common.money import decimal_to_str

MAX_SUMMARY = 500


@dataclass(frozen=True)
class ActivityDraft:
    type: str
    level: ActivityLevel
    direct: bool
    summary: str
    data: dict = field(default_factory=dict)


def _v(payload: dict, key: str, default: str = "?") -> str:
    value = payload.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return decimal_to_str(value)
    if isinstance(value, bool):
        return "sì" if value else "no"
    return str(getattr(value, "value", value))


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 1] + "…"


def _with_message(text: str, payload: dict) -> str:
    message = payload.get("message")
    return f"{text}: {message}" if message else text


def _fill(prefix: str, *, total: bool = False) -> Callable[[dict], str]:
    """`total`: the order's last fill, reported as the order's cumulative filled quantity
    and average price (a fill split in pieces is one «Ordine eseguito»)."""
    def summary(p: dict) -> str:
        qty_key, price_key = "qty", "price"
        if total and p.get("filled_qty") and p.get("avg_price"):
            qty_key, price_key = "filled_qty", "avg_price"
        return (f"{prefix}: {_v(p, 'side')} {_v(p, qty_key)} {_v(p, 'symbol')}"
                f" a {_v(p, price_key)} {_v(p, 'currency', '')}").rstrip()
    return summary


def _kill_switch(p: dict) -> str:
    if not p.get("on"):
        return "Kill switch disattivato"
    return f"Kill switch attivato ({_v(p, 'canceled', '0')} ordini annullati)"


def _reset(p: dict) -> str:
    return (f"Portafoglio azzerato: liquidità {_v(p, 'new_cash')} {_v(p, 'currency', '')}"
            f" ({_v(p, 'canceled_orders', '0')} ordini annullati,"
            f" {_v(p, 'closed_positions', '0')} posizioni chiuse)")


def _cash(p: dict) -> str:
    what = "Deposito" if p.get("type") == "deposit" else "Prelievo"
    return f"{what} di {_v(p, 'amount')} {_v(p, 'currency', '')}".rstrip()


def _halt(p: dict) -> str:
    where = " su tutti i portafogli" if p.get("scope") == "global" else ""
    reason = p.get("reason")
    if p.get("cleared"):
        return f"Aperture riattivate{where}" + (f": {reason}" if reason else "")
    if p.get("replaces"):  # a new reason for the same halt: never a revocation (M3)
        return f"Motivo aggiornato{where}: {_v(p, 'reason')}"
    canceled = p.get("canceled") or 0
    tail = f" ({canceled} ordini annullati)" if canceled else ""
    return f"Aperture sospese{where}: {_v(p, 'reason')}{tail}"


QUOTE_SUSPECT_REASONS = {"jump": "salto improvviso", "disagree": "fonti in disaccordo"}

# event type -> (level, direct, summary)
_EVENTS: dict[str, tuple[ActivityLevel, bool, Callable[[dict], str]]] = {
    "order.accepted": (ActivityLevel.info, False,
                       lambda p: f"Ordine accettato: {_v(p, 'symbol')}"),
    "order.filled": (ActivityLevel.executed, True, _fill("Ordine eseguito", total=True)),
    "order.partially_filled": (ActivityLevel.info, False,
                               _fill("Ordine eseguito in parte")),
    "order.rejected": (ActivityLevel.warning, True,
                       lambda p: _with_message(f"Ordine rifiutato: {_v(p, 'symbol')}", p)),
    "order.canceled": (ActivityLevel.info, False,
                       lambda p: f"Ordine annullato: {_v(p, 'symbol')}"),
    "order.expired": (ActivityLevel.info, False,
                      lambda p: f"Ordine scaduto: {_v(p, 'symbol')}"),
    "order.quote_suspect": (ActivityLevel.warning, False, lambda p: (
        f"Ordine {_v(p, 'symbol')} in attesa: prezzo da verificare"
        f" ({QUOTE_SUSPECT_REASONS.get(str(p.get('reason')), 'non confermato')}),"
        " si esegue con la prossima quotazione affidabile")),
    "portfolio.reset": (ActivityLevel.info, False, _reset),
    "portfolio.cash": (ActivityLevel.info, False, _cash),
    "halt.set": (ActivityLevel.critical, True, _halt),
    "halt.cleared": (ActivityLevel.critical, True, lambda p: _halt({**p, "cleared": True})),
}


def activity_for_event(type: str, payload: dict) -> ActivityDraft | None:
    """The activity of a domain event, or None for events that are not recorded."""
    # data is the raw payload (bools, ints, Decimal, UUID): record_activity json_safes it
    if type == "kill_switch":
        level = ActivityLevel.critical if payload.get("on") else ActivityLevel.warning
        return ActivityDraft(type, level, True, _truncate(_kill_switch(payload)), payload)
    spec = _EVENTS.get(type)
    if spec is None:
        return None
    level, direct, summary = spec
    return ActivityDraft(type, level, direct, _truncate(summary(payload)), payload)


_CRITICAL = {"liquidation", "daily_loss", "broker.order_unknown"}
_WARNING = {"margin_warning", "order.rejected", "broker.liquidation_waiting",
            "broker.fill_price_missing", "order.replace_failed",
            "broker.renewal_failed"}

_NOTIFICATIONS: dict[str, Callable[[dict], str]] = {
    "liquidation": lambda p: (
        f"Liquidazione del portafoglio: {len(p.get('orders') or [])} ordini di chiusura"
    ),
    "daily_loss": lambda p: (
        f"Perdita giornaliera di {_v(p, 'loss_eur')} EUR: portafoglio in sola riduzione"
    ),
    "broker.order_unknown": lambda p: (
        f"Esito dell'ordine {_v(p, 'symbol')} sconosciuto al broker ({_v(p, 'reason')})"
    ),
    "margin_warning": lambda p: (
        f"Margine vicino alla soglia: equity {_v(p, 'equity_eur')} EUR,"
        f" mantenimento {_v(p, 'maint_req_eur')} EUR"
    ),
    "order.rejected": lambda p: _with_message(f"Ordine rifiutato: {_v(p, 'symbol')}", p),
    "broker.liquidation_waiting": lambda p: (
        f"Liquidazione di {_v(p, 'symbol')} in attesa di altri ordini al broker"
    ),
    "broker.fill_price_missing": lambda p: (
        f"Prezzo di esecuzione di {_v(p, 'symbol')} non fornito dal broker"
    ),
    "broker.renewal_failed": lambda p: (
        f"Ordine {_v(p, 'symbol')} scaduto al broker dopo un'esecuzione parziale"
        f" ({_v(p, 'filled_qty')} su {_v(p, 'qty')}): non rinnovato, verifica la protezione"
    ),
    "order.replace_failed": lambda p: (
        f"Modifica dell'ordine {_v(p, 'symbol')} non riuscita: l'ordine è stato eseguito"
    ),
    "price_alert": lambda p: (
        f"Alert di prezzo su {_v(p, 'symbol')}: {_v(p, 'condition')} {_v(p, 'value')}"
        f" (prezzo {_v(p, 'price')})"
    ),
    "instrument.added": lambda p: f"Nuovo strumento: {_v(p, 'symbol')}",
    "split_orders_canceled": lambda p: (
        f"Frazionamento di {_v(p, 'symbol')}: {_v(p, 'canceled', '0')} ordini annullati"
    ),
}


def activity_for_notification(kind: str, payload: dict) -> ActivityDraft:
    """The activity of a persisted notification (its type is the kind)."""
    if kind in _CRITICAL:
        level, direct = ActivityLevel.critical, True
    elif kind in _WARNING:
        level, direct = ActivityLevel.warning, True
    else:
        level, direct = ActivityLevel.info, False
    summary = _NOTIFICATIONS.get(kind, lambda p: f"Notifica: {kind}")(payload)
    return ActivityDraft(kind, level, direct, _truncate(summary), payload)
