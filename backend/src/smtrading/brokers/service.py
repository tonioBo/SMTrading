import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.base import BrokerAdapter
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.registry import adapter_class
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.portfolios.models import Portfolio


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
    pf = await session.get(Portfolio, portfolio_id)
    if pf is None:
        raise SMTError(ErrorCode.NOT_FOUND, f"portfolio {portfolio_id} not found")
    if link_id is not None:
        await _get(session, link_id)
    pf.broker_link_id = link_id
    await session.flush()
    return pf
