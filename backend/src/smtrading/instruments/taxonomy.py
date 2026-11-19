"""Sectors and indices of the instruments (plan 22 «Pagina Mercati»).

`SECTORS`: the 11 GICS sectors in Italian, then the three non-equity groups (ETF, Crypto,
Cambi). The catalog stores the Italian label itself (`instruments.sector`).
`ETF_TYPES`: what an ETF holds, stored as its `industry`.
`INDICES`: the index codes of `instruments.indices` and their labels.
"""

SECTORS: tuple[str, ...] = (
    "Tecnologia",
    "Servizi di comunicazione",
    "Beni voluttuari",
    "Beni di prima necessità",
    "Energia",
    "Finanza",
    "Sanità",
    "Industria",
    "Materiali",
    "Immobiliare",
    "Servizi di pubblica utilità",
    "ETF",
    "Crypto",
    "Cambi",
)
EQUITY_SECTORS = SECTORS[:11]

ETF_TYPES: tuple[str, ...] = (
    "Azionario", "Obbligazionario", "Materie prime", "Leva e inverso", "Monetario",
    "Volatilità",
)

INDICES: dict[str, str] = {
    "SP500": "S&P 500",
    "NDX": "NASDAQ-100",
    "FTSEMIB": "FTSE MIB",
    "DAX": "DAX 40",
    "CAC40": "CAC 40",
    "MDAX": "MDAX",
}

# GICS sector (S&P 500 list) and Yahoo / Morningstar sector names -> Italian
SECTOR_ALIASES: dict[str, str] = {
    "information technology": "Tecnologia",
    "technology": "Tecnologia",
    "communication services": "Servizi di comunicazione",
    "telecommunications": "Servizi di comunicazione",
    "consumer discretionary": "Beni voluttuari",
    "consumer cyclical": "Beni voluttuari",
    "consumer staples": "Beni di prima necessità",
    "consumer defensive": "Beni di prima necessità",
    "energy": "Energia",
    "financials": "Finanza",
    "financial services": "Finanza",
    "financial": "Finanza",
    "health care": "Sanità",
    "healthcare": "Sanità",
    "industrials": "Industria",
    "materials": "Materiali",
    "basic materials": "Materiali",
    "real estate": "Immobiliare",
    "utilities": "Servizi di pubblica utilità",
}


def italian_sector(name: str | None) -> str | None:
    """The Italian sector for a GICS / Yahoo sector name, None when unknown."""
    if not name:
        return None
    return SECTOR_ALIASES.get(name.strip().lower())


def index_codes_matching(text: str) -> list[str]:
    """Index codes whose code or label contains `text` (case and punctuation blind):
    "s&p" -> SP500, "nasdaq" -> NDX, "mib" -> FTSEMIB, "dax" -> DAX and MDAX."""

    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    q = norm(text)
    if len(q) < 2:
        return []
    return [code for code, label in INDICES.items() if q in norm(code) or q in norm(label)]
