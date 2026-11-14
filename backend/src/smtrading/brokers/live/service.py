"""The activation flow of a real-money link and the real portfolio's TOTP actions.

keys (`configured`) -> read-only test -> optional dry run (`dry_run`) -> «Attiva denaro
reale» (`armed`, TOTP) -> one-click disarm (back to `configured`, no TOTP).

- The keys are encrypted with `aad=b"broker_live"` and never returned: only `••••` plus
  the last 4 characters of the key ID (`key_masked`) and the masked account ID.
- Every function that decrypts or receives keys registers them with `register_secret`
  (masked in every log text of the process).
- The read-only test does only GET requests, outside any transaction (no network I/O
  under a lock); a different account behind the same credentials puts the link back in
  `configured` and the linked portfolio in reduce-only (one critical alert,
  `live.account_changed`). Keys replaced while the test ran: its result is discarded.
  An Alpaca account must be a cash account (multiplier 1, `account_type` "cash"): a
  margin account fails the test and cannot be armed.
- Arming needs the admin TOTP, the three confirmations and the link name, a successful
  read-only test of an empty account from the last 15 minutes, a working Telegram (a
  test message sent in the last 24 hours) and a linked portfolio that passes
  `check_live_portfolio` (base currency = account currency, for both brokers). It never
  bypasses the kill switch or a trading halt: the live gate and the pre-trade checks
  still apply.
- Disarming rejects the never-sent orders through engine.service.reject_broker_order
  (held bracket children canceled, notified, untracked) with one `live.order.rejected`
  each; the others get a cancellation request.
- Linking a portfolio (allowed in `dry_run` and `armed`), resuming it after an automatic
  reduce-only and «Chiudi tutte le posizioni reali» (`armed` only) need the TOTP too.
- Lock order (R8.9, portfolios.locks): link (FOR NO KEY UPDATE), portfolio, orders.
- Every step is recorded in the append-only `live_audit`.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.admin.auth import _check_totp
from smtrading.admin.common import MASK, redact
from smtrading.admin.models import AdminUser
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.live.account import (
    LIVE_FIELDS,
    account_fingerprint,
    decrypt_live_config,
    encrypt_live_config,
    key_masked,
    live_secrets,
    register_config_secrets,
    snapshot_record,
)
from smtrading.brokers.live.audit import record_live_audit
from smtrading.brokers.live.gate import LiveClient, make_live_client
from smtrading.brokers.live.models import LiveState
from smtrading.brokers.live.protect import (
    live_alert,
    record_live_order_activity,
    trip_reduce_only,
)
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.registry import adapter_class
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import broker_live
from smtrading.engine.models import (
    OPEN_STATUSES,
    BrokerState,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from smtrading.engine.quotesource import QuoteSource
from smtrading.engine.service import reject_broker_order
from smtrading.events import POST_COMMIT_KEY, queue_event
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.portfolios.locks import lock_broker_link, lock_live_portfolio, lock_portfolio
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import get_cash_balances, is_live_reason, set_portfolio_status
from smtrading.positions.models import Position
from smtrading.risk.models import RiskProfile
from smtrading.telegram.config import is_ready, load_config
from smtrading.telegram.models import TelegramOutbox

log = logging.getLogger(__name__)

LIVE_TEST_MAX_AGE_S = 900
TELEGRAM_TEST_MAX_AGE = timedelta(hours=24)
KEYS_REPLACED_REASON = "chiavi sostituite"
ACCOUNT_CHANGED_REASON = "conto cambiato"
DISARMED_MESSAGE = "denaro reale disattivato"
TOTP_REQUIRED_MESSAGE = "Attiva prima l'autenticazione a due fattori"
NOT_DEDICATED_MESSAGE = "Il conto non è dedicato: chiudi posizioni e ordini dall'app del broker"
CASH_ACCOUNT_MESSAGE = "Il conto Alpaca deve essere un conto cash (senza margine)"
KEYS_CHANGED_MESSAGE = "chiavi sostituite durante il test: ripeti il test"
FLATTEN_NOT_ARMED_MESSAGE = ("denaro reale non attivo sul collegamento: chiudi le posizioni"
                             " dall'app del broker")
CLOSE_REAL_POSITIONS_MESSAGE = "Chiudi prima le posizioni reali"



@dataclass(frozen=True)
class Actor:
    """Who did it (an admin email, `system`, `cli`), from where."""

    email: str
    ip: str | None = None
    user_agent: str | None = None


def _who(actor: Actor | str) -> dict[str, str | None]:
    if isinstance(actor, Actor):
        return {"actor": actor.email, "ip": actor.ip, "user_agent": actor.user_agent}
    return {"actor": str(actor), "ip": None, "user_agent": None}


def _invalid(message: str, field: str, **details: Any) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message,
                    {"field": field, **{k: str(v) for k, v in details.items()}})


def _now() -> datetime:
    return datetime.now(UTC)


# -- keys ------------------------------------------------------------------------------

def _clean_config(kind: str, config: dict) -> dict:
    if kind not in LIVE_KINDS:
        raise _invalid(f"tipo di collegamento reale sconosciuto: {kind}", "kind")
    key_field, secret_field, extra = LIVE_FIELDS[kind]
    base = config.get("base_url")
    if base and str(base).rstrip("/") != adapter_class(kind).BASE_URL:
        raise SMTError(ErrorCode.FORBIDDEN, f"{kind}: base URL non consentito",
                       {"field": "base_url"})
    unknown = set(config) - {key_field, secret_field, "base_url"} - extra
    if unknown:
        raise _invalid(f"campi non ammessi: {', '.join(sorted(unknown))}", min(unknown))
    out: dict[str, Any] = {}
    for f in (key_field, secret_field):
        value = config.get(f)
        if not isinstance(value, str) or not value.strip():
            raise _invalid(f"{f} obbligatorio", f)
        if value.startswith(MASK):
            raise _invalid(f"inserisci la chiave completa in {f}", f)
        out[f] = value.strip()
    for f in extra:
        if f in config:
            out[f] = config[f]
    return out


async def _validate_with_adapter(kind: str, config: dict) -> None:
    """Builds the adapter on a client that can never send anything (config check only)."""
    client = offline_live_client(uuid.uuid4())
    try:
        adapter_class(kind)(dict(config), client)
    finally:
        await client.aclose()


def _refuse(request: httpx.Request) -> httpx.Response:
    raise RuntimeError("adattatore live in sola configurazione: nessuna richiesta di rete")


def offline_live_client(link_id: uuid.UUID, sessionmaker: async_sessionmaker | None = None
                    ) -> LiveClient:
    """A LiveClient whose inner transport refuses every request (config-only adapters)."""
    return make_live_client(sessionmaker, link_id, inner=httpx.MockTransport(_refuse))


# -- link lookups --------------------------------------------------------------------

async def _live_link(session: AsyncSession, link_id: uuid.UUID, *, lock: bool = True
                     ) -> BrokerLink:
    """The real-money link; with `lock` FOR NO KEY UPDATE, before any portfolio (R8.9)."""
    if lock:
        link = await lock_broker_link(session, link_id)
    else:
        link = await session.scalar(select(BrokerLink).where(BrokerLink.id == link_id))
    if link is None or link.kind not in LIVE_KINDS:
        raise SMTError(ErrorCode.NOT_FOUND, "collegamento reale non trovato",
                       {"link_id": str(link_id)})
    return link


async def _linked_portfolios(session: AsyncSession, link_id: uuid.UUID) -> list[Portfolio]:
    return list(await session.scalars(
        select(Portfolio).where(Portfolio.broker_link_id == link_id).order_by(Portfolio.id)))


def _require_totp_enabled(admin: AdminUser) -> None:
    if not admin.totp_enabled or admin.totp_secret_encrypted is None:
        raise SMTError(ErrorCode.FORBIDDEN, TOTP_REQUIRED_MESSAGE, {"field": "totp"})


async def _verify_totp(redis: Redis, admin: AdminUser, totp: str | None) -> None:
    try:
        await _check_totp(redis, admin, totp)
    except SMTError as e:
        raise SMTError(e.code, e.message, {**(e.details or {}), "field": "totp"}) from None


# -- keys ----------------------------------------------------------------------------

async def save_live_keys(
    session: AsyncSession, *, name: str, kind: str, config: dict, actor: Actor,
    link_id: uuid.UUID | None = None,
) -> BrokerLink:
    """A new real-money link, or new keys for an existing one: always `configured` (a
    new read-only test and a new TOTP are needed to arm it again)."""
    clean = _clean_config(kind, config)
    register_config_secrets(kind, clean)
    await _validate_with_adapter(kind, clean)
    name = name.strip()
    if not name:
        raise _invalid("nome obbligatorio", "name")
    clash = select(BrokerLink.id).where(BrokerLink.name == name)
    if link_id is not None:
        clash = clash.where(BrokerLink.id != link_id)
    if await session.scalar(clash) is not None:
        raise _invalid(f"il collegamento {name} esiste già", "name")
    if link_id is None:
        link = BrokerLink(name=name, kind=kind, live_state=LiveState.configured,
                          health_failures=0)
        session.add(link)
    else:
        link = await _live_link(session, link_id)
        if link.kind != kind:
            raise _invalid("il tipo del collegamento non si può cambiare", "kind")
        if link.live_state in (LiveState.armed, LiveState.dry_run):
            await disarm_live_link(session, link.id, actor=actor, reason=KEYS_REPLACED_REASON)
        link.name = name
    link.config_encrypted = encrypt_live_config(clean)
    link.live_state = LiveState.configured
    link.armed_at = None
    link.armed_by = None
    link.last_test_at = None  # the new keys need their own read-only test
    await session.flush()
    await record_live_audit(
        session, action="keys_saved", link_id=link.id, **_who(actor),
        details={"key_masked": key_masked(kind, clean), "replaced": link_id is not None,
                 "kind": kind},
    )
    return link


# -- read-only test --------------------------------------------------------------------

def _is_empty(snapshot: dict | None) -> bool:
    return bool(snapshot) and snapshot.get("positions_count") == 0 \
        and snapshot.get("open_orders_count") == 0


def _error_text(e: Exception, secrets: list[str]) -> str:
    if isinstance(e, SMTError):
        text = e.message
    elif isinstance(e, httpx.TimeoutException | TimeoutError):
        text = "timeout"
    else:
        text = f"{type(e).__name__}: {e}"
    return redact(text, secrets)[:300]


async def test_live_link(
    sessionmaker: async_sessionmaker, link_id: uuid.UUID, *, actor: Actor,
    http: LiveClient | None = None,
) -> dict[str, Any]:
    """The read-only test (GET only, through the live gate): reads the account and stores
    its snapshot; commits on its own. A different account than the one of the previous
    test puts the link back in `configured` (orders canceled or rejected), the linked
    portfolio in reduce-only and raises a critical alert; the link then needs another test.
    Failures are recorded (`test_failed`) and raised as PROVIDER_UNAVAILABLE, keys masked."""
    async with sessionmaker() as session:
        link = await _live_link(session, link_id, lock=False)
        kind = link.kind
        keys = link.config_encrypted  # the keys this test reads with
        config = decrypt_live_config(link)
        await session.commit()
    secrets = live_secrets(kind, config)
    if http is not None and not (isinstance(http, LiveClient) and http.link_id == link_id):
        raise SMTError(ErrorCode.FORBIDDEN, "gli adattatori live richiedono il cancello")
    own = http is None
    client = http if http is not None else make_live_client(sessionmaker, link_id)
    try:  # no transaction is open during the I/O
        adapter = adapter_class(kind)(config, client)
        snap = await adapter.get_account()
    except Exception as e:  # noqa: BLE001 - every failure is recorded, then raised
        message = _error_text(e, secrets)
        async with sessionmaker() as session:
            await record_live_audit(session, action="test_failed", link_id=link_id,
                                    **_who(actor), details={"error": message})
            await session.commit()
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE,
                       f"test del collegamento non riuscito: {message}",
                       {"link_id": str(link_id)}) from None
    finally:
        if own:
            await client.aclose()

    now = _now()
    snapshot = snapshot_record(snap)
    empty = _is_empty(snapshot)
    fp = account_fingerprint(kind, snap.account_id)
    margin = not _cash_account(kind, snapshot)
    async with sessionmaker() as session:
        link = await _live_link(session, link_id)
        if link.config_encrypted != keys:  # replaced while the test ran: not these keys
            await record_live_audit(session, action="test_failed", link_id=link_id,
                                    **_who(actor), details={"error": KEYS_CHANGED_MESSAGE})
            await session.commit()
            raise _invalid(KEYS_CHANGED_MESSAGE, "test")
        changed = link.account_fingerprint is not None and link.account_fingerprint != fp
        link.account_fingerprint = fp
        link.last_account_snapshot = snapshot
        link.last_snapshot_at = now
        # after a change, or on a margin account: test again
        link.last_test_at = None if changed or margin else now
        if changed:
            await account_changed(session, link, actor)
        details = {**snapshot, "empty": empty, "account_changed": changed}
        if margin:
            details["error"] = CASH_ACCOUNT_MESSAGE
        await record_live_audit(session, action="test_failed" if margin else "test_ok",
                                link_id=link_id, **_who(actor), details=details)
        await session.commit()
    if margin:
        raise _invalid(CASH_ACCOUNT_MESSAGE, "account_type",
                       account_type=snapshot.get("account_type"))
    return {**snapshot, "empty": empty, "account_changed": changed}


test_live_link.__test__ = False  # a service function, not a pytest test


ACCOUNT_CHANGED_SUMMARY = ("Conto diverso dietro le stesse credenziali: denaro reale"
                           " disattivato, portafoglio in reduce-only")


async def account_changed(session: AsyncSession, link: BrokerLink, actor: Actor | str, *,
                          data: dict | None = None) -> list[uuid.UUID]:
    """Another account behind the keys (read-only test or reconciliation): disarmed
    (`conto cambiato`) when it was not `configured`, every linked portfolio in reduce-only
    and ONE critical alert, `live.account_changed` (the disarm and the reduce-only are
    recorded in the audit without their own alerts). The link row is locked by the
    caller. Returns the linked portfolios."""
    was = link.live_state
    if was != LiveState.configured:
        await disarm_live_link(session, link.id, actor=actor, reason=ACCOUNT_CHANGED_REASON,
                               alert=False)
    portfolios = await _linked_portfolios(session, link.id)
    for p in portfolios:
        await trip_reduce_only(session, p.id, cause="fingerprint_changed",
                               details={"link_id": link.id}, alert=False,
                               actor=_who(actor)["actor"] or "system")
    await live_alert(
        session, kind="account_changed", summary=ACCOUNT_CHANGED_SUMMARY,
        portfolio_id=portfolios[0].id if portfolios else None, link_id=link.id,
        data={"link_name": link.name, "from": was, **(data or {})},
    )
    return [p.id for p in portfolios]


# -- dry run, arm, disarm ------------------------------------------------------------------

def _test_age_ok(link: BrokerLink, now: datetime) -> bool:
    return (link.last_test_at is not None
            and now - link.last_test_at <= timedelta(seconds=LIVE_TEST_MAX_AGE_S))


def _cash_account(kind: str, snapshot: dict | None) -> bool:
    """R8.10: an Alpaca live account must be a cash account (multiplier 1)."""
    return kind != "alpaca_live" or (snapshot or {}).get("account_type") == "cash"


def _require_fresh_empty_test(link: BrokerLink, now: datetime) -> None:
    if not _test_age_ok(link, now) or link.account_fingerprint is None:
        raise _invalid("esegui il test in sola lettura (valido 15 minuti)", "test")
    if not _is_empty(link.last_account_snapshot):
        raise _invalid(NOT_DEDICATED_MESSAGE, "test")
    if not _cash_account(link.kind, link.last_account_snapshot):
        raise _invalid(CASH_ACCOUNT_MESSAGE, "account_type")


async def set_dry_run(session: AsyncSession, link_id: uuid.UUID, on: bool, *,
                      actor: Actor) -> BrokerLink:
    link = await _live_link(session, link_id)
    if link.live_state == LiveState.armed:
        raise _invalid("disattiva prima il denaro reale", "live_state")
    if on and link.live_state != LiveState.dry_run:
        if link.last_test_at is None or link.account_fingerprint is None:
            raise _invalid("esegui prima il test in sola lettura", "test")
        if not _is_empty(link.last_account_snapshot):
            raise _invalid(NOT_DEDICATED_MESSAGE, "test")
        if not _cash_account(link.kind, link.last_account_snapshot):
            raise _invalid(CASH_ACCOUNT_MESSAGE, "account_type")
        link.live_state = LiveState.dry_run
        action = "dry_run_on"
    elif not on and link.live_state == LiveState.dry_run:
        link.live_state = LiveState.configured
        action = "dry_run_off"
    else:
        return link
    await session.flush()
    await record_live_audit(session, action=action, link_id=link.id, **_who(actor))
    return link


async def _telegram_ok(session: AsyncSession, now: datetime) -> bool:
    cfg = await load_config(session)
    if not is_ready(cfg) or cfg.paused:
        return False
    sent = await session.scalar(
        select(TelegramOutbox.id).where(
            TelegramOutbox.category == "test", TelegramOutbox.status == "sent",
            TelegramOutbox.sent_at >= now - TELEGRAM_TEST_MAX_AGE,
        ).limit(1))
    return sent is not None


async def arm_live_link(
    session: AsyncSession, redis: Redis, link_id: uuid.UUID, *, admin: AdminUser,
    totp: str | None, confirm_name: str, accept_risks: bool, dedicated_account: bool,
    limits_set: bool, actor: Actor, qs: QuoteSource | None = None,
) -> BrokerLink:
    """«Attiva denaro reale»: every check in order (each error names its `field`), the
    TOTP last; then `armed` with a critical alert."""
    _require_totp_enabled(admin)
    if not (accept_risks and dedicated_account and limits_set):
        raise _invalid("conferma i rischi, il conto dedicato e i limiti", "confirm")
    link = await _live_link(session, link_id)
    if (confirm_name or "").strip() != link.name:
        raise _invalid("scrivi il nome esatto del collegamento", "confirm_name")
    if link.live_state not in (LiveState.configured, LiveState.dry_run):
        raise _invalid("denaro reale già attivo", "live_state")
    now = _now()
    _require_fresh_empty_test(link, now)
    config = decrypt_live_config(link)
    snapshot = link.last_account_snapshot or {}
    if link.kind == "alpaca_live" and config.get("crypto_enabled") in (True, "true") \
            and snapshot.get("crypto_enabled") is not True:
        raise _invalid("la cripto non è attiva sul conto: disattivala nel collegamento",
                       "crypto_enabled")
    if not await _telegram_ok(session, now):
        raise _invalid("Telegram deve funzionare: configura il bot e invia un messaggio di"
                       " prova (ultime 24 ore)", "telegram")
    portfolios = await _linked_portfolios(session, link.id)
    for p in portfolios:
        await check_live_portfolio(session, p, link, qs=qs)
    await _verify_totp(redis, admin, totp)
    previous = link.live_state
    link.live_state = LiveState.armed
    link.armed_at = now
    link.armed_by = admin.email[:128]
    link.disarmed_reason = None
    await session.flush()
    portfolio_id = portfolios[0].id if portfolios else None
    await record_live_audit(session, action="armed", link_id=link.id,
                            portfolio_id=portfolio_id, **_who(actor),
                            details={"from": previous})
    await live_alert(session, kind="armed", summary=f"Denaro reale attivato su {link.name}",
                     portfolio_id=portfolio_id, link_id=link.id,
                     data={"link_name": link.name, "by": "admin"})
    return link


async def disarm_live_link(session: AsyncSession, link_id: uuid.UUID, *,
                           actor: Actor | str, reason: str, alert: bool = True) -> int:
    """«Disattiva denaro reale»: one click, no TOTP. Back to `configured`; the open orders
    already at (or maybe at) the broker get a cancellation request (BrokerSync sends the
    DELETE, which the gate always lets through), the ones never sent are rejected with
    LIVE_NOT_ARMED through engine.service.reject_broker_order (their held bracket
    children canceled, notified, untracked) with a `live.order.rejected` each. Held
    children of an entry at the broker stay with it (BrokerSync settles them). Lock order
    (R8.9): link, portfolios, orders. `alert=False`: the caller sends its own alert (an
    account change). Returns how many orders it touched."""
    link = await _live_link(session, link_id)
    previous = link.live_state
    portfolios = await _linked_portfolios(session, link.id)
    for p in portfolios:
        await lock_portfolio(session, p.id)  # lock order: link, portfolio, its orders
    orders = list(await session.scalars(
        select(Order).where(Order.broker_link_id == link.id,
                            Order.portfolio_id.in_([p.id for p in portfolios]),
                            Order.status.in_(OPEN_STATUSES))
        .order_by(Order.created_at, Order.id).with_for_update()
        .execution_options(populate_existing=True)))
    touched = 0
    now = _now()
    for o in orders:
        if o.status not in OPEN_STATUSES or o.status == OrderStatus.held:
            continue  # canceled with its entry, or waiting for an entry at the broker
        if broker_live(o):
            if o.broker_status != BrokerState.CANCEL_REQUESTED:
                o.broker_status = BrokerState.CANCEL_REQUESTED
                o.updated_at = now
                queue_event(session, "order.cancel_requested", {
                    "order_id": o.id, "portfolio_id": o.portfolio_id,
                    "agent_id": o.agent_id, "symbol": o.symbol, "by": "system",
                })
                touched += 1
        else:
            o.broker_status = None
            await reject_broker_order(session, o, ErrorCode.LIVE_NOT_ARMED, DISARMED_MESSAGE)
            await record_live_order_activity(session, o, "rejected", message=DISARMED_MESSAGE)
            touched += 1
    link.live_state = LiveState.configured
    link.disarmed_at = now
    link.disarmed_reason = reason[:200]
    await session.flush()
    portfolio_id = portfolios[0].id if portfolios else None
    await record_live_audit(session, action="disarmed", link_id=link.id,
                            portfolio_id=portfolio_id, **_who(actor),
                            details={"reason": reason, "from": previous, "orders": touched})
    if alert and previous in (LiveState.armed, LiveState.dry_run):
        await live_alert(session, kind="disarmed",
                         summary=f"Denaro reale disattivato su {link.name}: {reason}",
                         portfolio_id=portfolio_id, link_id=link.id,
                         data={"link_name": link.name, "reason": reason, "orders": touched})
    return touched


# -- the real portfolio ----------------------------------------------------------------

async def _portfolio_cash_in(session: AsyncSession, portfolio: Portfolio, ccy: str,
                             qs: QuoteSource | None) -> Decimal:
    total = Decimal(0)
    for asset, balance in (await get_cash_balances(session, portfolio.id)).items():
        if not balance:
            continue
        if asset == ccy:
            total += balance
            continue
        if qs is None:
            raise _invalid("cambio non disponibile per confrontare la liquidità", "cash")
        total += balance * await qs.get_fx_rate(asset, ccy)
    return total


async def check_live_portfolio(session: AsyncSession, portfolio: Portfolio, link: BrokerLink,
                               *, qs: QuoteSource | None = None) -> None:
    """The conditions a portfolio needs to trade real money on `link` (INVALID_ORDER with
    the `field` otherwise)."""
    profile = (await session.get(RiskProfile, portfolio.risk_profile_id)
               if portfolio.risk_profile_id is not None else None)
    if profile is None or profile.max_order_notional_eur is None or (
            profile.max_daily_loss_pct is None and profile.max_daily_loss_eur is None):
        raise _invalid("il profilo di rischio deve avere un limite per ordine e un limite di"
                       " perdita giornaliera", "risk_profile")
    if profile.short_allowed:
        raise _invalid("il profilo di rischio non deve consentire lo short", "short_allowed")
    if portfolio.leverage != 1:
        raise _invalid("la leva del portafoglio deve essere 1", "leverage")
    snapshot = link.last_account_snapshot or {}
    if not snapshot:
        raise _invalid("esegui prima il test in sola lettura", "test")
    broker_ccy = str(snapshot.get("currency") or "")
    if portfolio.base_currency != broker_ccy:  # R8.5: both brokers
        raise _invalid(f"la valuta del portafoglio deve essere {broker_ccy}", "base_currency")
    other = await session.scalar(
        select(Portfolio.id).where(Portfolio.broker_link_id == link.id,
                                   Portfolio.id != portfolio.id).limit(1))
    if other is not None:
        raise _invalid("il collegamento reale è già usato da un altro portafoglio", "link_id")
    if not _is_empty(snapshot):
        raise _invalid(NOT_DEDICATED_MESSAGE, "account")
    broker_cash = Decimal(str(snapshot.get("cash_available") or 0))
    ours = await _portfolio_cash_in(session, portfolio, broker_ccy, qs)
    if ours > broker_cash:
        raise _invalid("la liquidità del portafoglio supera quella disponibile al broker",
                       "cash", portfolio_cash=ours, broker_cash=broker_cash)


async def link_live_portfolio(
    session: AsyncSession, redis: Redis, portfolio_id: uuid.UUID, link_id: uuid.UUID, *,
    admin: AdminUser, totp: str | None, actor: Actor, qs: QuoteSource | None = None,
) -> Portfolio:
    """Links a portfolio to a real-money link in `dry_run` or `armed` (TOTP)."""
    from smtrading.brokers.service import check_relink, link_portfolio

    _require_totp_enabled(admin)
    link = await _live_link(session, link_id)  # lock order: link, then portfolio (R8.9)
    pf = await lock_live_portfolio(session, portfolio_id)
    if link.live_state not in (LiveState.dry_run, LiveState.armed):
        raise _invalid("avvia la prova a secco o attiva il denaro reale prima di collegare",
                       "live_state")
    _require_fresh_empty_test(link, _now())
    await check_live_portfolio(session, pf, link, qs=qs)
    await check_relink(session, pf, link.id)
    await _verify_totp(redis, admin, totp)
    pf = await link_portfolio(session, pf.id, link.id, actor=actor, _allow_live=True)
    await record_live_audit(session, action="portfolio_linked", link_id=link.id,
                            portfolio_id=pf.id, **_who(actor),
                            details={"portfolio_name": pf.name, "live_state": link.live_state})
    return pf


async def resume_live_portfolio(
    session: AsyncSession, redis: Redis, portfolio_id: uuid.UUID, *, admin: AdminUser,
    totp: str | None, actor: Actor,
) -> Portfolio:
    """«Ho verificato, riprendi»: a portfolio put in reduce-only by a live anomaly goes
    back to `active` (TOTP). The only path that may (set_portfolio_status allow_live)."""
    _require_totp_enabled(admin)
    pf = await lock_live_portfolio(session, portfolio_id)
    if pf.status != PortfolioStatus.reduce_only or not is_live_reason(pf.reduce_only_reason):
        raise _invalid("il portafoglio non è sospeso per un'anomalia del denaro reale",
                       "status")
    await _verify_totp(redis, admin, totp)
    reason = pf.reduce_only_reason
    set_portfolio_status(session, pf, PortfolioStatus.active, allow_live=True)
    await session.flush()
    queue_event(session, "portfolio.status", {"portfolio_id": pf.id, "status": pf.status})
    if pf.broker_link_id is not None:
        await record_live_audit(session, action="resumed", link_id=pf.broker_link_id,
                                portfolio_id=pf.id, **_who(actor), details={"reason": reason})
    else:
        log.warning("live portfolio %s resumed without a link: no live audit", pf.id)
    return pf


async def flatten_live_portfolio(
    session: AsyncSession, redis: Redis, qs: QuoteSource, portfolio_id: uuid.UUID, *,
    admin: AdminUser, totp: str | None, actor: Actor, now: datetime | None = None,
) -> list[uuid.UUID]:
    """«Chiudi tutte le posizioni reali» (TOTP, link `armed` only: otherwise nothing could
    be sent and the admin closes from the broker's app): one reduce-only market order per
    open position, flagged `live_flatten` (they pass the kill switch, Ruling R8.2).
    Returns the ids of the orders placed; a position whose order is refused is recorded
    in the audit details (`failed`). The agents' resting orders (openings included) are
    NOT canceled: the kill switch, a halt or «Disattiva denaro reale» does that."""
    from smtrading.engine.service import PlaceOrderRequest, place_order

    _require_totp_enabled(admin)
    link_id = await session.scalar(select(Portfolio.broker_link_id)
                                   .where(Portfolio.id == portfolio_id))
    link = await lock_broker_link(session, link_id) if link_id is not None else None
    pf = await lock_live_portfolio(session, portfolio_id)  # lock order: link, portfolio
    if link is None or link.kind not in LIVE_KINDS or pf.broker_link_id != link.id:
        raise _invalid("il portafoglio non è collegato a un conto reale", "portfolio_id")
    if link.live_state != LiveState.armed:
        raise _invalid(FLATTEN_NOT_ARMED_MESSAGE, "live_state")
    await _verify_totp(redis, admin, totp)
    positions = list(await session.scalars(
        select(Position).where(Position.portfolio_id == pf.id, Position.qty != 0)
        .order_by(Position.symbol)))
    placed: list[uuid.UUID] = []
    failed: list[dict[str, str]] = []
    for pos in positions:
        asset_class = await session.scalar(
            select(Instrument.asset_class).where(Instrument.symbol == pos.symbol))
        req = PlaceOrderRequest(
            portfolio_id=pf.id, symbol=pos.symbol,
            side=OrderSide.sell if pos.qty > 0 else OrderSide.buy, type=OrderType.market,
            tif=TimeInForce.gtc if asset_class == AssetClass.crypto else TimeInForce.day,
            qty=abs(pos.qty),
            source="admin", reduce_only=True, live_flatten=True,
        )
        mark = len(session.info.setdefault(POST_COMMIT_KEY, []))
        try:
            async with session.begin_nested():
                order = await place_order(session, qs, req, now=now, redis=redis)
            placed.append(order.id)
        except SMTError as e:
            del session.info.setdefault(POST_COMMIT_KEY, [])[mark:]  # rolled back with it
            failed.append({"symbol": pos.symbol, "code": str(e.code), "message": e.message})
    await record_live_audit(session, action="flatten", link_id=link.id, portfolio_id=pf.id,
                            **_who(actor),
                            details={"orders": placed, "failed": failed})
    await live_alert(session, kind="flatten",
                     summary=f"Chiusura di tutte le posizioni reali: {len(placed)} ordini",
                     portfolio_id=pf.id, link_id=link.id,
                     data={"orders": placed, "failed": failed})
    return placed
