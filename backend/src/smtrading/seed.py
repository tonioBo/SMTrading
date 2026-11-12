import asyncio
import logging
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.db import get_sessionmaker
from smtrading.fees.models import FeeRule, FeeScope, FeeSide
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio

log = logging.getLogger("smtrading.seed")

# Serializes concurrent seed runs (e.g. two migrate containers), transaction-scoped.
SEED_LOCK_KEY = 7300002

DEFAULT_FEE_RULE_NAME = "Default globale"
DEMO_PORTFOLIO_NAME = "Demo"

_US = (AssetClass.us_equity, "USD", "0.000001", "0.01", True)
_EU_MTA = (AssetClass.eu_equity, "EUR", "1", "0.001", True)
_EU_XETRA = (AssetClass.eu_equity, "EUR", "1", "0.01", True)
_CRYPTO = (AssetClass.crypto, "USD", "0.00000001", "0.01", False)
_FX = (AssetClass.forex, "USD", "1", "0.00001", False)

# symbol, name, market, (asset_class, currency, lot, tick, shortable)
_INSTRUMENTS = [
    ("AAPL", "Apple Inc.", "NASDAQ", _US),
    ("MSFT", "Microsoft Corp.", "NASDAQ", _US),
    ("NVDA", "NVIDIA Corp.", "NASDAQ", _US),
    ("SPY", "SPDR S&P 500 ETF", "NYSE", _US),
    ("ENI.MI", "Eni S.p.A.", "MTA", _EU_MTA),
    ("ISP.MI", "Intesa Sanpaolo", "MTA", _EU_MTA),
    ("SAP.DE", "SAP SE", "XETRA", _EU_XETRA),
    ("VWCE.DE", "Vanguard FTSE All-World UCITS ETF", "XETRA", _EU_XETRA),
    ("BTC-USD", "Bitcoin", "CRYPTO", _CRYPTO),
    ("ETH-USD", "Ethereum", "CRYPTO", _CRYPTO),
    ("EURUSD", "Euro / US Dollar", "FX", _FX),
    ("GBPUSD", "British Pound / US Dollar", "FX", _FX),
]


async def run_seed(session: AsyncSession) -> None:
    """Idempotent seed. Does not commit. Existing rows (and admin edits) are left untouched."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SEED_LOCK_KEY})
    rows = [
        {
            "symbol": symbol,
            "name": name,
            "asset_class": asset_class,
            "market": market,
            "currency": currency,
            "lot_size": Decimal(lot),
            "tick_size": Decimal(tick),
            "shortable": shortable,
            "active": True,
            "provider_symbols": {},
        }
        for symbol, name, market, (asset_class, currency, lot, tick, shortable) in _INSTRUMENTS
    ]
    await session.execute(
        insert(Instrument).values(rows).on_conflict_do_nothing(index_elements=["symbol"])
    )

    if await session.scalar(
        select(FeeRule.id).where(FeeRule.name == DEFAULT_FEE_RULE_NAME).limit(1)
    ) is None:
        session.add(
            FeeRule(
                name=DEFAULT_FEE_RULE_NAME,
                scope=FeeScope.global_,
                side=FeeSide.both,
                currency="EUR",
                fixed=Decimal(1),
                pct=Decimal("0.1"),
                min_fee=Decimal(1),
                max_fee=Decimal(20),
                fx_pct=Decimal("0.25"),
            )
        )

    if await session.scalar(
        select(Portfolio.id).where(Portfolio.name == DEMO_PORTFOLIO_NAME)
    ) is None:
        await create_portfolio(
            session, name=DEMO_PORTFOLIO_NAME, initial_cash=Decimal(100000), base_currency="EUR"
        )
    await session.flush()


async def _main() -> None:
    async with get_sessionmaker()() as session:
        await run_seed(session)
        await session.commit()
    log.info("seed complete: %d instruments, default fee rule, Demo portfolio", len(_INSTRUMENTS))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
