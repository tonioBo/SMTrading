import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

import smtrading.connectors.models
import smtrading.engine.models
import smtrading.fees.models
import smtrading.instruments.models
import smtrading.ledger.models
import smtrading.portfolios.models
import smtrading.positions.models
import smtrading.risk.models  # noqa: F401
from smtrading.common.config import get_settings
from smtrading.common.db import Base

target_metadata = Base.metadata


def _url() -> str:
    return context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
