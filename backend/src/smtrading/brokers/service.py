import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.base import BrokerAdapter
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


def _validated(kind: str, config: dict) -> dict:
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
    link = await _get(session, link_id)
    if link.config_encrypted is None:
        return {}
    return decrypt_json(link.config_encrypted, get_settings().master_key)


async def get_adapter(session: AsyncSession, link_id: uuid.UUID, http) -> BrokerAdapter:
    link = await _get(session, link_id)
    config = await get_link_config(session, link_id)
    return adapter_class(link.kind)(config, http)


async def link_portfolio(
    session: AsyncSession, portfolio_id: uuid.UUID, link_id: uuid.UUID | None
) -> Portfolio:
    pf = await lock_live_portfolio(session, portfolio_id)  # archived: never linked
    if link_id is not None:
        await _get(session, link_id)
    if link_id == pf.broker_link_id:
        return pf
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
    pf.broker_link_id = link_id
    await session.flush()
    return pf
