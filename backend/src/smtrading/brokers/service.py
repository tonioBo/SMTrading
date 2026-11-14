import contextlib
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.base import BrokerAdapter
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.registry import adapter_class
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import broker_live
from smtrading.engine.models import OPEN_STATUSES, Order, OrderType
from smtrading.portfolios.locks import lock_live_portfolio
from smtrading.portfolios.models import Portfolio
from smtrading.positions.models import Position

USE_LIVE_FLOW_MESSAGE = "usa la procedura «denaro reale»"


def _refuse_live_kind(kind: str) -> None:
    """Real-money links are created, changed and linked only by brokers.live.service."""
    if kind in LIVE_KINDS:
        raise SMTError(ErrorCode.INVALID_ORDER, USE_LIVE_FLOW_MESSAGE, {"field": "kind"})


def _validated(kind: str, config: dict) -> dict:
    _refuse_live_kind(kind)
    adapter_class(kind)(config, None)  # raises FORBIDDEN for non-paper base URLs
    return config


async def _get(session: AsyncSession, link_id: uuid.UUID) -> BrokerLink:
    link = await session.get(BrokerLink, link_id)
    if link is None:
        raise SMTError(ErrorCode.NOT_FOUND, f"broker link {link_id} not found")
    return link


async def create_link(session: AsyncSession, name: str, kind: str, config: dict) -> BrokerLink:
    exists = await session.scalar(select(BrokerLink.id).where(BrokerLink.name == name))
    if exists is not None:
        raise SMTError(ErrorCode.INVALID_ORDER, f"broker link {name} already exists")
    link = BrokerLink(
        name=name,
        kind=kind,
        config_encrypted=encrypt_json(_validated(kind, config), get_settings().master_key),
    )
    session.add(link)
    await session.flush()
    return link


async def update_link(
    session: AsyncSession,
    link_id: uuid.UUID,
    *,
    name: str | None = None,
    enabled: bool | None = None,
    config: dict | None = None,
) -> BrokerLink:
    link = await _get(session, link_id)
    _refuse_live_kind(link.kind)
    if name is not None:
        link.name = name
    if enabled is not None:
        link.enabled = enabled
    if config is not None:
        link.config_encrypted = encrypt_json(
            _validated(link.kind, config), get_settings().master_key
        )
    await session.flush()
    return link


async def list_links(session: AsyncSession) -> list[BrokerLink]:
    return list((await session.scalars(select(BrokerLink).order_by(BrokerLink.name))).all())


async def get_link_config(session: AsyncSession, link_id: uuid.UUID) -> dict:
    """The decrypted config. Live links: decrypted with their own context
    (aad=b"broker_live") and their keys registered for log masking."""
    link = await _get(session, link_id)
    if link.kind in LIVE_KINDS:
        from smtrading.brokers.live.account import decrypt_live_config

        return decrypt_live_config(link)
    if link.config_encrypted is None:
        return {}
    return decrypt_json(link.config_encrypted, get_settings().master_key)


async def get_adapter(
    session: AsyncSession, link_id: uuid.UUID, http, *,
    sessionmaker: async_sessionmaker | None = None,
) -> BrokerAdapter:
    """The adapter of a link on `http`. A live link always gets a LiveClient: `http`
    itself when it is the LiveClient of this link; with `http=None` (config only) one
    whose inner transport refuses every request; otherwise a new one from
    make_live_client (gate state read with `sessionmaker`, default: a sessionmaker on
    the session's bind), owned by the caller through `adapter.http` (close it)."""
    link = await _get(session, link_id)
    config = await get_link_config(session, link_id)
    if link.kind in LIVE_KINDS:
        from smtrading.brokers.live.gate import LiveClient, make_live_client
        from smtrading.brokers.live.service import offline_live_client

        maker = sessionmaker or async_sessionmaker(bind=session.bind, expire_on_commit=False)
        if http is None:
            http = offline_live_client(link_id, maker)
        elif not (isinstance(http, LiveClient) and http.link_id == link_id):
            http = make_live_client(maker, link_id)
    return adapter_class(link.kind)(config, http)


@contextlib.asynccontextmanager
async def open_adapter(
    session: AsyncSession, link_id: uuid.UUID, http, *,
    sessionmaker: async_sessionmaker | None = None,
) -> AsyncIterator[BrokerAdapter]:
    """get_adapter as an async context: a client get_adapter built itself (a live link's
    LiveClient) is closed on exit; the caller's `http` stays open."""
    adapter = await get_adapter(session, link_id, http, sessionmaker=sessionmaker)
    try:
        yield adapter
    finally:
        own = getattr(adapter, "http", http)
        if own is not http and own is not None:
            await own.aclose()


async def check_relink(session: AsyncSession, pf: Portfolio, link_id: uuid.UUID | None
                       ) -> None:
    """The rules of a link change of a (locked) portfolio, without changing anything."""
    if link_id == pf.broker_link_id:
        return
    if pf.broker_link_id is not None:
        current = await session.get(BrokerLink, pf.broker_link_id)
        if current is not None and current.kind in LIVE_KINDS and await session.scalar(
            select(Position.symbol)
            .where(Position.portfolio_id == pf.id, Position.qty != 0).limit(1)
        ) is not None:
            raise SMTError(
                ErrorCode.INVALID_ORDER, "Chiudi prima le posizioni reali",
                {"portfolio_id": str(pf.id), "field": "positions"},
            )
    open_orders = list(await session.scalars(
        select(Order).where(Order.portfolio_id == pf.id, Order.status.in_(OPEN_STATUSES))
    ))
    # orders at (or maybe at) the broker must finish first; unsent ones go to the
    # simulator on unlink (BrokerSync releases them)
    if any(o.broker_link_id is not None and broker_live(o) for o in open_orders):
        raise SMTError(
            ErrorCode.INVALID_ORDER,
            "ordini in corso sul broker: annullali e attendi la conferma prima di cambiare"
            " il collegamento",
            {"portfolio_id": str(pf.id)},
        )
    # Ruling R10: on a linked portfolio nothing fills locally; only trailing stops are
    # simulated (and handed to the broker on trigger)
    if link_id is not None and any(
        o.broker_link_id is None and o.type != OrderType.trailing_stop for o in open_orders
    ):
        raise SMTError(
            ErrorCode.INVALID_ORDER,
            "annulla prima gli ordini aperti simulati del portafoglio",
            {"portfolio_id": str(pf.id)},
        )
    # the broker does not hold the simulated positions: a close would be routed for qty
    # the account does not have (a short on Alpaca paper)
    if link_id is not None and await session.scalar(
        select(Position.symbol).where(Position.portfolio_id == pf.id, Position.qty != 0).limit(1)
    ) is not None:
        raise SMTError(
            ErrorCode.INVALID_ORDER,
            "Chiudi prima le posizioni aperte: il broker non le conosce",
            {"portfolio_id": str(pf.id)},
        )


async def link_portfolio(
    session: AsyncSession, portfolio_id: uuid.UUID, link_id: uuid.UUID | None, *,
    actor=None, _allow_live: bool = False,
) -> Portfolio:
    """Links (or, with None, unlinks) a portfolio. A real-money link is linked only by
    brokers.live.service.link_live_portfolio (TOTP); unlinking from one needs no open
    positions and is recorded in live_audit (`actor`: a live service Actor or a name)."""
    pf = await lock_live_portfolio(session, portfolio_id)  # archived: never linked
    if link_id is not None:
        target = await _get(session, link_id)
        if target.kind in LIVE_KINDS and not _allow_live and link_id != pf.broker_link_id:
            raise SMTError(ErrorCode.FORBIDDEN, USE_LIVE_FLOW_MESSAGE, {"field": "link_id"})
    if link_id == pf.broker_link_id:
        return pf
    await check_relink(session, pf, link_id)
    previous = await session.get(BrokerLink, pf.broker_link_id) if pf.broker_link_id else None
    pf.broker_link_id = link_id
    await session.flush()
    if previous is not None and previous.kind in LIVE_KINDS:
        from smtrading.brokers.live.audit import record_live_audit
        from smtrading.brokers.live.service import Actor

        who = actor if isinstance(actor, Actor) else Actor(str(actor or "admin"))
        await record_live_audit(
            session, action="portfolio_unlinked", link_id=previous.id, portfolio_id=pf.id,
            actor=who.email, ip=who.ip, user_agent=who.user_agent,
            details={"portfolio_name": pf.name, "new_link_id": link_id},
        )
    return pf
