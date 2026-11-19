"""Fill the sector, industry and indices columns of the instrument catalog CSVs.

Not part of the test suite (it needs the network, or saved answers). Run from backend/:

    uv run python ../scripts/enrich_catalog.py --wiki DIR [--yahoo-cache FILE] [--write]

Sources, in order:
- indices: the Wikipedia constituent pages the catalog was built from, saved as HTML in
  DIR (List_of_S%26P_500_companies.html, List_of_NASDAQ-100_companies.html, FTSE_MIB.html,
  DAX.html, CAC_40.html, MDAX.html);
- sector: the GICS sector of the S&P 500 page, else Yahoo's sector (assetProfile), mapped
  to the Italian list of instruments.taxonomy; ETFs (Yahoo quoteType ETF, or the issuer /
  "ETF" in the name) are «ETF», crypto «Crypto», forex «Cambi»;
- industry: Yahoo's industry, else the GICS sub-industry; for ETFs the asset type
  (taxonomy.ETF_TYPES) from Yahoo's fund category and the name.

Yahoo is called once per stock or ETF without an answer in --yahoo-cache (JSON lines
{symbol, quoteType, sector, industry, category}), about one call a second; answers are
appended to the cache. Without --write it only prints the coverage.
"""

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

from smtrading.instruments.catalog import CATALOG_DIR, COLUMNS
from smtrading.instruments.taxonomy import italian_sector

# Yahoo has no profile for these (checked 2026-11-19; Finnhub's profile is US-only on our
# plan): GICS sector of the company from its own reports; Xetra-Gold is a gold ETC
MANUAL: dict[str, tuple[str, str]] = {
    "1COV.DE": ("Materiali", "Specialty Chemicals"),
    "ML.PA": ("Beni voluttuari", "Auto Parts"),
    "4GLD.DE": ("ETF", "Materie prime"),
}

_ISSUERS = re.compile(
    r"\b(ETF|UCITS|iShares|Xtrackers|Lyxor|Amundi|SPDR|Vanguard|Invesco|ProShares|"
    r"Direxion|WisdomTree|VanEck|Schwab|JPMorgan Equity Premium|ARK Innovation)\b", re.IGNORECASE)
_LEVERAGE = re.compile(r"\b(Ultra\w*|[23]x|Leveraged|Short|Bear|Inverse)\b", re.IGNORECASE)
_MONEY = re.compile(r"(0-3 Month|1-3 Month T-Bill|Treasury Bill|Money Market|T-Bill)", re.IGNORECASE)
_BOND = re.compile(r"\b(Bond|Treasury|Aggregate|Govt|Government|Corporate|TIPS|Muni\w*|"
                   r"Inflation)\b", re.IGNORECASE)
_COMMODITY = re.compile(r"\b(Gold|Silver|Oil|Natural Gas|Commodit\w*|Physical)\b", re.IGNORECASE)
# a fund of companies of a commodity sector (miners, oil & gas producers) holds shares
_SHARES = re.compile(r"\b(Miners|STOXX|S&P|Exploration|Production|Producers|Equity)\b", re.IGNORECASE)


def etf_type(name: str, category: str | None) -> str:
    cat = (category or "").lower()
    if "volatil" in name.lower() or "vix" in name.lower():
        return "Volatilità"
    if cat.startswith("trading--") or _LEVERAGE.search(name):
        return "Leva e inverso"
    if _MONEY.search(name):
        return "Monetario"
    if "commodit" in cat or (_COMMODITY.search(name) and not _SHARES.search(name)):
        return "Materie prime"
    if "bond" in cat or "government" in cat or _BOND.search(name):
        return "Obbligazionario"
    return "Azionario"


def wiki(directory: Path) -> tuple[dict[str, set[str]], dict[str, tuple[str, str]]]:
    import pandas as pd

    tags: dict[str, set[str]] = {}
    gics: dict[str, tuple[str, str]] = {}

    def tag(sym: str, code: str) -> None:
        tags.setdefault(str(sym).strip().upper(), set()).add(code)

    t = pd.read_html(directory / "List_of_S%26P_500_companies.html")[0]
    for s, sec, sub in zip(t.Symbol, t["GICS Sector"], t["GICS Sub-Industry"], strict=True):
        s = str(s).replace(".", "-")
        tag(s, "SP500")
        gics[s] = (sec, sub)
    for s in pd.read_html(directory / "List_of_NASDAQ-100_companies.html")[0].Ticker:
        tag(str(s).replace(".", "-"), "NDX")
    for s in pd.read_html(directory / "FTSE_MIB.html")[2].Ticker:
        tag(s, "FTSEMIB")
    for s in pd.read_html(directory / "DAX.html")[4].Ticker:
        tag(s, "DAX")
    for s in pd.read_html(directory / "CAC_40.html")[4].Ticker:
        tag(s, "CAC40")
    for s in pd.read_html(directory / "MDAX.html")[2].Symbol:
        tag(f"{s}.DE", "MDAX")
    return tags, gics


def yahoo(rows: list[dict], cache: Path | None) -> dict[str, dict]:
    known: dict[str, dict] = {}
    if cache and cache.exists():
        for line in cache.read_text().splitlines():
            rec = json.loads(line)
            if not rec.get("err"):
                known[rec["symbol"]] = rec
    todo = [r for r in rows if r["asset_class"] in ("us_equity", "eu_equity")
            and r["symbol"] not in known]
    if todo:
        from smtrading.marketdata.providers.yahoo import _ticker

        out = cache.open("a") if cache else None
        for r in todo:
            overrides = dict(p.split("=", 1) for p in r["provider_symbols"].split(";") if p)
            info = _ticker(overrides.get("yahoo") or r["symbol"]).info or {}
            rec = {"symbol": r["symbol"], **{k: info.get(k) for k in (
                "quoteType", "sector", "industry", "category")}}
            known[r["symbol"]] = rec
            if out:
                out.write(json.dumps(rec) + "\n")
            time.sleep(1)
    return known


def enrich(row: dict, tags: dict, gics: dict, y: dict) -> dict:
    sym, ac, name = row["symbol"], row["asset_class"], row["name"]
    info = y.get(sym) or {}
    sector = industry = None
    if sym in MANUAL:
        sector, industry = MANUAL[sym]
    elif ac == "crypto":
        sector = "Crypto"
    elif ac == "forex":
        sector = "Cambi"
    elif info.get("quoteType") == "ETF" or (
            sym not in gics and not info.get("sector") and _ISSUERS.search(name)):
        sector, industry = "ETF", etf_type(name, info.get("category"))
    else:
        g = gics.get(sym)
        sector = italian_sector(g[0]) if g else None
        sector = sector or italian_sector(info.get("sector"))
        industry = info.get("industry") or (g[1] if g else None)
    return {**row, "sector": sector or "", "industry": (industry or "")[:128],
            "indices": ";".join(c for c in ("SP500", "NDX", "FTSEMIB", "DAX", "CAC40", "MDAX")
                                if c in tags.get(sym, ()))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", type=Path, required=True)
    ap.add_argument("--yahoo-cache", type=Path)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    tags, gics = wiki(args.wiki)
    files = {p: list(csv.DictReader(p.open(newline="", encoding="utf-8")))
             for p in sorted(CATALOG_DIR.glob("*.csv"))}
    y = yahoo([r for rows in files.values() for r in rows], args.yahoo_cache)
    stats: Counter = Counter()
    for path, rows in files.items():
        out = [enrich(r, tags, gics, y) for r in rows]
        for r in out:
            stats["rows"] += 1
            stats["sector"] += bool(r["sector"])
            stats["industry"] += bool(r["industry"])
            if r["asset_class"].endswith("equity") and r["sector"] not in ("", "ETF"):
                stats["stocks"] += 1
            if r["asset_class"].endswith("equity") and not r["sector"]:
                print("no sector:", r["symbol"], r["name"], file=sys.stderr)
        if args.write:
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
                w.writeheader()
                w.writerows(out)
    print(dict(stats))


if __name__ == "__main__":
    main()
