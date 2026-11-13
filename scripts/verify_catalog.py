"""Check every row of the instrument catalog against live market data providers.

Not part of the test suite (it needs the network). Run from backend/:

    uv run python ../scripts/verify_catalog.py                    # Yahoo (default)
    uv run python ../scripts/verify_catalog.py --providers yahoo,coingecko
    uv run python ../scripts/verify_catalog.py --drop             # rewrite CSVs without misses

A row is resolved when the first provider of --providers that can map it returns a quote
with a positive last price. Rows no listed provider can map are reported as "unmapped".
Keyless providers only: configuration (API keys) is not read.
"""

import argparse
import asyncio
import csv
import sys
from collections import Counter

import httpx

import smtrading.marketdata.providers  # noqa: F401  (registers the providers)
from smtrading.instruments.catalog import CATALOG_DIR, COLUMNS, CatalogRow, load_catalog
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.registry import PROVIDERS

BATCH = 25


async def _quote_rows(provider, rows: list[CatalogRow], pause: float) -> dict[str, str]:
    """symbol -> "ok" | reason, for the rows this provider can map."""
    items, out = [], {}
    for r in rows:
        try:
            items.append((r.symbol, provider.map_symbol(
                r.symbol, r.asset_class, r.provider_symbols.get(provider.name))))
        except ProviderUnsupported:
            continue
    for i in range(0, len(items), BATCH):
        if i:
            await asyncio.sleep(pause)  # stay under the providers' rate limits
        batch = items[i:i + BATCH]
        try:
            quotes = await provider.get_quotes(batch)
        except Exception as e:  # noqa: BLE001 - report and keep going
            out.update({s: f"error: {type(e).__name__}: {e}"[:160] for s, _ in batch})
            continue
        for s, ps in batch:
            q = quotes.get(s)
            if q is None:
                out[s] = f"missing ({ps})"
            elif q.last is None or q.last <= 0:
                out[s] = f"zero price ({ps})"
            else:
                out[s] = "ok"
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="yahoo")
    ap.add_argument("--drop", action="store_true", help="remove unresolved rows from the CSVs")
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between batches")
    args = ap.parse_args()
    names = [n.strip() for n in args.providers.split(",") if n.strip()]
    rows = list(load_catalog())
    status: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as http:
        for name in names:
            provider = PROVIDERS[name]({}, http)
            todo = [r for r in rows if status.get(r.symbol, "unmapped") != "ok"]
            for sym, result in (await _quote_rows(provider, todo, args.pause)).items():
                if result == "ok" or sym not in status or status[sym] == "unmapped":
                    status[sym] = result if result == "ok" else f"{name}: {result}"
    for r in rows:
        status.setdefault(r.symbol, "unmapped")
    bad = {s: v for s, v in status.items() if v != "ok"}
    by_market = Counter(r.market for r in rows)
    ok_market = Counter(r.market for r in rows if status[r.symbol] == "ok")
    print(f"catalog rows: {len(rows)}  resolved: {len(rows) - len(bad)}  unresolved: {len(bad)}")
    for m in sorted(by_market):
        print(f"  {m:16} {ok_market[m]:4}/{by_market[m]}")
    for s, v in sorted(bad.items()):
        print(f"  UNRESOLVED {s}: {v}")
    if args.drop and bad:
        for path in sorted(CATALOG_DIR.glob("*.csv")):
            with path.open(newline="", encoding="utf-8") as f:
                kept = [r for r in csv.DictReader(f) if r["symbol"] not in bad]
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
                w.writeheader()
                w.writerows(kept)
        print(f"dropped {len(bad)} rows")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
