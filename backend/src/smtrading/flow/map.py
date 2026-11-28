"""The node map of the «Fiume dei flussi»: nodes, links, where each MCP tool goes.

Columns, left to right: 0 agents · 1 orchestration (Paperclip) · 2 MCP door and automatic
rules · 3 engine and data (Neurograph, Motore ordini, Database, Prezzi) · 4 outputs
(BrokerSync, Simulato, Telegram) · 5 brokers. Agent nodes are the `agents` rows, broker
nodes the `broker_links` kinds that exist (one per kind). `kind` is the colour family.

Destinations of an MCP call: "eng" (Motore ordini), "px" (Prezzi), "ng" (Neurograph),
"db" (Database), "tg" (Telegram), "mcp" (the call ends at the MCP). Calls to "eng" and
"tg" are one way (an order, a message); the others answer with data (`… → MCP → Paperclip`).
Database → Neurograph is the nightly export (not an MCP call).

The lens (v2): every MCP tool and resource belongs to one family (`TOOL_FAMILY`,
`RESOURCE_FAMILY`), every market-data provider of the router has a display name
(`PRICE_SOURCE_LABELS`); guards in `tests/unit/test_flow_families.py`.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents.models import Agent
from smtrading.brokers.models import BrokerLink
from smtrading.instruments.market_hours import local_date, session_on

DESTS = ("eng", "px", "ng", "db", "tg", "mcp")
ONE_WAY_DESTS = frozenset({"eng", "tg"})

_ENG, _PX, _NG, _DB, _TG = "eng", "px", "ng", "db", "tg"

# MCP tool name -> destination node (guard: tests/unit/test_flow_map.py)
TOOL_DEST: dict[str, str] = {
    # tools_portfolio: portfolio reads from the database
    "list_portfolios": _DB, "get_portfolio": _DB, "get_positions": _DB,
    "get_transactions": _DB, "get_performance": _DB, "get_risk_limits": _DB,
    # tools_orders: the order engine (risk checks, cash, orders); plain reads of the
    # orders table answer from the database
    "check_tradable": _ENG, "preview_order": _ENG, "place_order": _ENG,
    "place_bracket_order": _ENG, "place_oco_order": _ENG, "modify_order": _ENG,
    "cancel_order": _ENG, "cancel_all_orders": _ENG, "close_position": _ENG,
    "close_all_positions": _ENG,
    "get_order": _DB, "list_orders": _DB,
    # tools_market: market data, news, instruments
    "search_instruments": _PX, "get_instrument": _PX, "get_quote": _PX, "get_bars": _PX,
    "get_market_status": _PX, "get_indicators": _PX, "get_fx_rate": _PX, "get_news": _PX,
    "get_fundamentals": _PX, "get_earnings_calendar": _PX, "get_dividends": _PX,
    "screen": _PX,
    # tools_alerts / tools_watch: watchlists and alerts kept in the database
    "get_triggered_alerts": _DB, "list_watchlists": _DB, "create_watchlist": _DB,
    "add_to_watchlist": _DB, "remove_from_watchlist": _DB, "delete_watchlist": _DB,
    "create_alert": _DB, "list_alerts": _DB, "delete_alert": _DB,
    # tools_backtest / tools_research
    "run_backtest": _DB, "get_backtest": _DB, "list_backtests": _DB,
    "run_walk_forward": _DB, "run_param_sweep": _DB, "run_portfolio_backtest": _DB,
    "get_research": _DB, "list_research": _DB,
    # tools_activity / tools_halt / tools_goals / tools_docs
    "log_decision": _DB, "get_decisions": _DB, "get_activity_feed": _DB,
    "set_trading_halt": _DB, "get_trading_halt": _DB, "get_goals": _DB,
    "get_instructions_appendix": _DB,
    # tools_telegram / tools_forecast
    "send_telegram": _TG,
    "get_forecast": _NG,
}

# MCP resources (audited as `resource:<uri>`): URI scheme -> destination
RESOURCE_DEST: dict[str, str] = {"portfolio": _DB, "fees": _DB, "markets": _PX}

# the lens (spec v2 §5): the families an MCP node opens into, in their order on the page
FAMILIES: tuple[dict[str, str], ...] = (
    {"key": "orders", "label": "ordini"},
    {"key": "market", "label": "dati di mercato"},
    {"key": "risk", "label": "rischio e previsioni"},
    {"key": "research", "label": "ricerca"},
    {"key": "decisions", "label": "decisioni e attività"},
    {"key": "messages", "label": "messaggi"},
)
_ORD, _MKT, _RSK, _RES, _DEC, _MSG = (f["key"] for f in FAMILIES)

# MCP tool name -> family (guard: tests/unit/test_flow_families.py)
TOOL_FAMILY: dict[str, str] = {
    # portfolios and orders
    "list_portfolios": _ORD, "get_portfolio": _ORD, "get_positions": _ORD,
    "get_transactions": _ORD, "get_performance": _ORD,
    "check_tradable": _ORD, "preview_order": _ORD, "place_order": _ORD,
    "place_bracket_order": _ORD, "place_oco_order": _ORD, "modify_order": _ORD,
    "cancel_order": _ORD, "cancel_all_orders": _ORD, "close_position": _ORD,
    "close_all_positions": _ORD, "get_order": _ORD, "list_orders": _ORD,
    # market data, news, instruments, watchlists and alerts
    "search_instruments": _MKT, "get_instrument": _MKT, "get_quote": _MKT, "get_bars": _MKT,
    "get_market_status": _MKT, "get_indicators": _MKT, "get_fx_rate": _MKT, "get_news": _MKT,
    "get_fundamentals": _MKT, "get_earnings_calendar": _MKT, "get_dividends": _MKT,
    "screen": _MKT, "get_triggered_alerts": _MKT, "list_watchlists": _MKT,
    "create_watchlist": _MKT, "add_to_watchlist": _MKT, "remove_from_watchlist": _MKT,
    "delete_watchlist": _MKT, "create_alert": _MKT, "list_alerts": _MKT, "delete_alert": _MKT,
    # risk limits, the trading halt, Neurograph's risk range
    "get_risk_limits": _RSK, "set_trading_halt": _RSK, "get_trading_halt": _RSK,
    "get_forecast": _RSK,
    # backtests and research
    "run_backtest": _RES, "get_backtest": _RES, "list_backtests": _RES,
    "run_walk_forward": _RES, "run_param_sweep": _RES, "run_portfolio_backtest": _RES,
    "get_research": _RES, "list_research": _RES,
    # decisions, activity, goals, the instructions appendix
    "log_decision": _DEC, "get_decisions": _DEC, "get_activity_feed": _DEC,
    "get_goals": _DEC, "get_instructions_appendix": _DEC,
    # messages
    "send_telegram": _MSG,
}

# MCP resources: URI scheme -> family
RESOURCE_FAMILY: dict[str, str] = {"portfolio": _ORD, "fees": _ORD, "markets": _MKT}

# the market-data router's providers (`marketdata.registry.PROVIDERS`) -> display name
# (guard: tests/unit/test_flow_families.py)
PRICE_SOURCE_LABELS: dict[str, str] = {
    "alpaca": "Alpaca", "kraken": "Kraken", "yahoo": "Yahoo", "etoro": "eToro",
    "binance": "Binance", "finnhub": "Finnhub", "twelvedata": "Twelve Data",
    "coingecko": "CoinGecko", "revolutx": "Revolut X", "frankfurter": "Frankfurter (cambi)",
    "rssnews": "Notizie RSS",
}

BROKER_LABELS: dict[str, str] = {
    "alpaca_paper": "Alpaca (prova)",
    "alpaca_live": "Alpaca reale",
    "etoro_demo": "eToro (demo)",
    "etoro_live": "eToro reale",
    "t212_demo": "Trading 212 (demo)",
    "t212_live": "Trading 212 reale",
}

STATIC_NODES: dict[str, dict] = {
    "pc": {"label": "Paperclip", "kind": "agent", "column": 1,
           "description": "Orchestratore: sveglia gli agenti, task e sessioni"},
    "mcp": {"label": "MCP SMTrading", "kind": "core", "column": 2,
            "description": f"La porta degli agenti: {len(TOOL_DEST)} strumenti, "
                           "ogni chiamata registrata"},
    "rules": {"label": "Regole automatiche", "kind": "core", "column": 2,
              "description": "Scalping: entra ed esce da solo in meno di 1 s"},
    "ng": {"label": "Neurograph", "kind": "ng", "column": 3,
           "description": "Forchetta di rischio GARCH (get_forecast) e ricezione notturna "
                          "di decisioni, ordini ed esecuzioni, sulla VPN"},
    "eng": {"label": "Motore ordini", "kind": "core", "column": 3,
            "description": "Controlli di rischio, prenotazione cassa, ordini e protezioni"},
    "db": {"label": "Database", "kind": "db", "column": 3,
           "description": "Portafogli, ordini, decisioni, registro attività"},
    "px": {"label": "Prezzi", "kind": "data", "column": 3,
           "description": "Flussi in tempo reale Alpaca e Kraken, Yahoo, eToro"},
    "sync": {"label": "BrokerSync", "kind": "core", "column": 4,
             "description": "Invia ai broker, legge esecuzioni, riconcilia"},
    "sim": {"label": "Simulato", "kind": "broker", "column": 4,
            "description": "Esecuzioni simulate dal motore"},
    "tg": {"label": "Telegram", "kind": "tg", "column": 4,
           "description": "Avvisi, riepiloghi, domande al titolare"},
}

# what each known agent role does (by agent name, case and spaces ignored)
AGENT_DESCRIPTIONS: dict[str, str] = {
    "ceo": "Approva strategie e allocazioni, guida il piano verso l'obiettivo",
    "portfolio manager": "Decide entrate e uscite, invia gli ordini",
    "risk manager": "Controlla limiti, stop e rischio di ogni ordine",
    "quant researcher": "Cerca e prova strategie sullo storico",
    "market analyst": "Analizza mercati, indicatori e livelli",
    "execution supervisor": "Verifica esecuzioni, stop e obiettivi attivi",
    "news analyst": "Legge le notizie, mette in pausa prima degli eventi",
    "performance auditor": "Confronta risultati reali e backtest",
    "notifier": "Manda i messaggi su Telegram",
}
AGENT_DEFAULT_DESCRIPTION = "Agente"


# the links between static nodes, in their forward direction (plus agent -> pc and
# sync -> broker for each agent and broker node)
STATIC_LINKS: tuple[tuple[str, str], ...] = (
    ("pc", "mcp"),
    *(("mcp", d) for d in DESTS if d != "mcp"),
    ("eng", "sync"), ("eng", "sim"), ("eng", "tg"), ("px", "rules"), ("rules", "eng"),
    ("db", "ng"),  # the nightly export of decisions, orders and fills (smtrading.neurograph)
)


def _forward(a: str, b: str) -> bool:
    return ((a.startswith("agent:") and b == "pc") or (a == "sync" and b.startswith("broker:"))
            or (a, b) in STATIC_LINKS)


def link_key(a: str, b: str) -> str:
    """The key "a|b" of the link a hop travels, in the link's forward direction (a return
    hop MCP -> Paperclip travels the link "pc|mcp")."""
    return f"{b}|{a}" if not _forward(a, b) and _forward(b, a) else f"{a}|{b}"


def agent_key(agent_id) -> str:
    return f"agent:{agent_id}"


def broker_key(kind: str) -> str:
    return f"broker:{kind}"


def broker_label(kind: str) -> str:
    return BROKER_LABELS.get(kind, kind)


def destination(method: str) -> str:
    """Where an audited MCP method goes: a tool by TOOL_DEST, a resource by its URI scheme,
    anything unknown ends at the MCP."""
    if method.startswith("resource:"):
        return RESOURCE_DEST.get(method.removeprefix("resource:").split("://", 1)[0], "mcp")
    return TOOL_DEST.get(method, "mcp")


def family(method: str) -> str | None:
    """The lens family of an audited MCP method (None: unknown)."""
    if method.startswith("resource:"):
        return RESOURCE_FAMILY.get(method.removeprefix("resource:").split("://", 1)[0])
    return TOOL_FAMILY.get(method)


def price_source_label(name: str) -> str:
    return PRICE_SOURCE_LABELS.get(name, name)


def families() -> list[dict]:
    """[{key, label, tools: [tool, ...]}] in page order, tools in catalog order."""
    return [{**f, "tools": [t for t, k in TOOL_FAMILY.items() if k == f["key"]]}
            for f in FAMILIES]


US_MARKET = "NYSE"
_SESSION_LOOKBACK_DAYS = 15


def last_session(now: datetime) -> dict | None:
    """{from, to} (UTC) of the last completed regular US equities session before `now`
    (NYSE calendar, holidays and early closes applied): the replay preset «ultima seduta
    USA»."""
    day = local_date(US_MARKET, now)
    for _ in range(_SESSION_LOOKBACK_DAYS):
        s = session_on(US_MARKET, day)
        if s is not None and s[1] <= now:
            return {"from": s[0], "to": s[1]}
        day -= timedelta(days=1)
    return None


def _agent_description(name: str) -> str:
    return AGENT_DESCRIPTIONS.get(" ".join(name.split()).casefold(), AGENT_DEFAULT_DESCRIPTION)


async def flow_map(session: AsyncSession) -> dict:
    """{nodes: [{key, label, kind, column, description}], links: [[a, b], ...], families:
    [{key, label, tools}], price_sources: [{key, label}]}: every node and every pair that
    an event can produce (forward direction), the lens families and the price sources,
    last_session: {from, to} | null (the «ultima seduta USA» preset)."""
    agents = (await session.execute(select(Agent.id, Agent.name).order_by(Agent.name))).all()
    kinds = sorted((await session.scalars(select(BrokerLink.kind).distinct())).all())

    nodes = [{"key": agent_key(a.id), "label": a.name, "kind": "agent", "column": 0,
              "description": _agent_description(a.name)} for a in agents]
    nodes += [{"key": k, **n} for k, n in STATIC_NODES.items()]
    nodes += [{"key": broker_key(k), "label": broker_label(k), "kind": "broker", "column": 5,
               "description": f"Collegamento broker: {broker_label(k)}"} for k in kinds]

    links = [[agent_key(a.id), "pc"] for a in agents]
    links += [list(p) for p in STATIC_LINKS]
    links += [["sync", broker_key(k)] for k in kinds]
    return {"nodes": nodes, "links": links, "families": families(),
            "last_session": last_session(datetime.now(UTC)),
            "price_sources": [{"key": k, "label": v} for k, v in PRICE_SOURCE_LABELS.items()]}
