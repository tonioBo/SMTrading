"""The curated instrument catalog: one CSV per area (us, eu, crypto, forex) next to this file.

Columns: symbol, name, market, asset_class, currency, lot_size, tick_size, shortable,
provider_symbols ("provider=symbol;provider=symbol", may be empty), sector (one of
`taxonomy.SECTORS`, may be empty), industry (may be empty), indices (index codes of
`taxonomy.INDICES`, ";"-separated, may be empty). Every row was checked against the market
data providers with scripts/verify_catalog.py; sectors and industries were read once from
the providers (scripts/enrich_catalog.py) and are baked in: no provider call at runtime.

`seed_catalog` inserts the rows that are missing and never touches existing ones, so admin
edits (and deactivations) always win. `backfill_metadata` then fills only the NULL sector /
industry / indices of the existing catalog symbols (rows created before those columns).
"""

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path

from sqlalchemy import bindparam, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.instruments.market_hours import MARKETS
from smtrading.instruments.models import AssetClass, Instrument, InstrumentSource
from smtrading.instruments.taxonomy import INDICES, SECTORS

CATALOG_DIR = Path(__file__).resolve().parent
COLUMNS = (
    "symbol", "name", "market", "asset_class", "currency", "lot_size", "tick_size",
    "shortable", "provider_symbols", "sector", "industry", "indices",
)
_BATCH = 500


@dataclass(frozen=True)
class CatalogRow:
    symbol: str
    name: str
    market: str
    asset_class: AssetClass
    currency: str
    lot_size: Decimal
    tick_size: Decimal
    shortable: bool
    provider_symbols: dict[str, str]
    sector: str | None = None
    industry: str | None = None
    indices: tuple[str, ...] = ()

    def values(self) -> dict:
        return {
            "symbol": self.symbol, "name": self.name, "asset_class": self.asset_class,
            "market": self.market, "currency": self.currency, "lot_size": self.lot_size,
            "tick_size": self.tick_size, "shortable": self.shortable, "active": True,
            "provider_symbols": dict(self.provider_symbols),
            "source": InstrumentSource.catalog, "added_by": None,
            "sector": self.sector, "industry": self.industry, "indices": list(self.indices),
        }


def _positive(raw: str, where: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{where}: not a number: {raw!r}") from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{where}: must be positive: {raw!r}")
    return value


def _overrides(raw: str, where: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in raw.split(";"))):
        provider, sep, symbol = part.partition("=")
        if not sep or not provider.strip() or not symbol.strip():
            raise ValueError(f"{where}: bad provider_symbols entry {part!r}")
        out[provider.strip()] = symbol.strip()
    return out


def parse_row(raw: dict[str, str], where: str) -> CatalogRow:
    symbol = (raw.get("symbol") or "").strip()
    if not symbol or symbol != symbol.upper():
        raise ValueError(f"{where}: symbol must be non-empty upper case: {symbol!r}")
    name = (raw.get("name") or "").strip()
    if not name or len(name) > 256:
        raise ValueError(f"{where}: name must have 1-256 characters")
    market = raw.get("market", "")
    if market not in MARKETS:
        raise ValueError(f"{where}: unknown market {market!r}")
    try:
        asset_class = AssetClass(raw.get("asset_class", ""))
    except ValueError:
        raise ValueError(f"{where}: unknown asset_class {raw.get('asset_class')!r}") from None
    currency = raw.get("currency", "")
    if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
        raise ValueError(f"{where}: bad currency {currency!r}")
    shortable = raw.get("shortable", "")
    if shortable not in ("true", "false"):
        raise ValueError(f"{where}: shortable must be true or false")
    sector = (raw.get("sector") or "").strip() or None
    if sector is not None and sector not in SECTORS:
        raise ValueError(f"{where}: unknown sector {sector!r}")
    industry = (raw.get("industry") or "").strip() or None
    if industry is not None and len(industry) > 128:
        raise ValueError(f"{where}: industry longer than 128 characters")
    indices = tuple(filter(None, (p.strip() for p in (raw.get("indices") or "").split(";"))))
    if any(code not in INDICES for code in indices) or len(set(indices)) != len(indices):
        raise ValueError(f"{where}: bad indices {raw.get('indices')!r}")
    return CatalogRow(
        symbol=symbol, name=name, market=market, asset_class=asset_class, currency=currency,
        lot_size=_positive(raw.get("lot_size", ""), f"{where} lot_size"),
        tick_size=_positive(raw.get("tick_size", ""), f"{where} tick_size"),
        shortable=shortable == "true",
        provider_symbols=_overrides(raw.get("provider_symbols") or "", where),
        sector=sector, industry=industry, indices=indices,
    )


@cache
def load_catalog(directory: Path = CATALOG_DIR) -> tuple[CatalogRow, ...]:
    """Every catalog row, validated. Raises ValueError on a malformed file or a duplicate."""
    rows: dict[str, CatalogRow] = {}
    for path in sorted(directory.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if tuple(reader.fieldnames or ()) != COLUMNS:
                raise ValueError(f"{path.name}: columns must be {','.join(COLUMNS)}")
            for line, raw in enumerate(reader, start=2):
                row = parse_row(raw, f"{path.name}:{line}")
                if row.symbol in rows:
                    raise ValueError(f"{path.name}:{line}: duplicate symbol {row.symbol}")
                rows[row.symbol] = row
    return tuple(rows.values())


async def seed_catalog(session: AsyncSession) -> int:
    """Insert the missing catalog rows (ON CONFLICT DO NOTHING); returns how many."""
    rows = [r.values() for r in load_catalog()]
    inserted = 0
    for i in range(0, len(rows), _BATCH):
        result = await session.execute(
            insert(Instrument)
            .values(rows[i:i + _BATCH])
            .on_conflict_do_nothing(index_elements=["symbol"])
            .returning(Instrument.symbol)
        )
        inserted += len(result.all())
    return inserted


async def backfill_metadata(session: AsyncSession) -> int:
    """Fill the NULL sector / industry / indices of existing catalog symbols from the CSVs
    (rows inserted before those columns existed). A value already set is never touched,
    whatever its origin, so an admin edit wins; idempotent. Returns how many rows changed."""
    rows = {r.symbol: r for r in load_catalog()}
    symbols = list(rows)
    todo: list[str] = []
    for i in range(0, len(symbols), _BATCH):
        found = await session.execute(
            select(Instrument.symbol, Instrument.sector, Instrument.industry,
                   Instrument.indices).where(
                Instrument.symbol.in_(symbols[i:i + _BATCH]),
                or_(Instrument.sector.is_(None), Instrument.industry.is_(None),
                    Instrument.indices.is_(None)),
            )
        )
        # only a NULL the catalog has a value for (crypto and forex have no industry)
        todo += [
            sym for sym, sector, industry, indices in found.all()
            if (sector is None and rows[sym].sector) or (industry is None and rows[sym].industry)
            or indices is None
        ]
    params = [
        {"sym": s, "s": rows[s].sector, "i": rows[s].industry, "x": list(rows[s].indices)}
        for s in todo
    ]
    if not params:
        return 0
    stmt = (
        update(Instrument)
        .where(Instrument.symbol == bindparam("sym"))
        .values(
            sector=func.coalesce(Instrument.sector, bindparam("s")),
            industry=func.coalesce(Instrument.industry, bindparam("i")),
            indices=func.coalesce(
                Instrument.indices, bindparam("x", type_=Instrument.indices.type)),
        )
        .execution_options(synchronize_session=False)
    )
    await (await session.connection()).execute(stmt, params)
    return len(params)
