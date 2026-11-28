"""The node map of the «Fiume dei flussi»: nodes, links, where each MCP tool goes.

Columns, left to right: 0 agents · 1 orchestration (Paperclip) · 2 MCP door and automatic
rules · 3 engine and data (Neurograph, Motore ordini, Database, Prezzi) · 4 outputs
(BrokerSync, Simulato, Telegram) · 5 brokers. Agent nodes are the `agents` rows, broker
nodes the `broker_links` kinds that exist (one per kind). `kind` is the colour family.

Destinations of an MCP call: "eng" (Motore ordini), "px" (Prezzi), "ng" (Neurograph),
"db" (Database), "tg" (Telegram), "mcp" (the call ends at the MCP). Calls to "eng" and
"tg" are one way (an order, a message); the others answer with data (`… → MCP → Paperclip`).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents.models import Agent
from smtrading.brokers.models import BrokerLink

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
           "description": "Forchetta di rischio GARCH (get_forecast), sulla VPN"},
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


def _agent_description(name: str) -> str:
    return AGENT_DESCRIPTIONS.get(" ".join(name.split()).casefold(), AGENT_DEFAULT_DESCRIPTION)


async def flow_map(session: AsyncSession) -> dict:
    """{nodes: [{key, label, kind, column, description}], links: [[a, b], ...]}: every node
    and every pair that an event can produce (forward direction)."""
    agents = (await session.execute(select(Agent.id, Agent.name).order_by(Agent.name))).all()
    kinds = sorted((await session.scalars(select(BrokerLink.kind).distinct())).all())

    nodes = [{"key": agent_key(a.id), "label": a.name, "kind": "agent", "column": 0,
              "description": _agent_description(a.name)} for a in agents]
    nodes += [{"key": k, **n} for k, n in STATIC_NODES.items()]
    nodes += [{"key": broker_key(k), "label": broker_label(k), "kind": "broker", "column": 5,
               "description": f"Collegamento broker: {broker_label(k)}"} for k in kinds]

    links = [[agent_key(a.id), "pc"] for a in agents]
    links.append(["pc", "mcp"])
    links += [["mcp", d] for d in DESTS if d != "mcp"]
    links += [["eng", "sync"], ["eng", "sim"], ["eng", "tg"], ["px", "rules"],
              ["rules", "eng"]]
    links += [["sync", broker_key(k)] for k in kinds]
    return {"nodes": nodes, "links": links}
