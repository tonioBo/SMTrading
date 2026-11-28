"""Guards of the «Fiume dei flussi» map (spec «Sempre aggiornata»): a new MCP tool, MCP
resource or broker kind without a destination / label fails here."""

from smtrading.flow.map import (
    BROKER_LABELS,
    DESTS,
    ONE_WAY_DESTS,
    RESOURCE_DEST,
    STATIC_NODES,
    TOOL_DEST,
    destination,
)
from smtrading.mcp.server import build_mcp


async def test_every_mcp_tool_has_a_destination():
    names = {t.name for t in await build_mcp().list_tools()}
    assert names <= set(TOOL_DEST), sorted(names - set(TOOL_DEST))
    assert set(TOOL_DEST) <= names, sorted(set(TOOL_DEST) - names)  # no stale entry
    assert set(TOOL_DEST.values()) <= {"eng", "px", "ng", "db", "tg", "mcp"}
    assert TOOL_DEST["get_forecast"] == "ng" and TOOL_DEST["place_order"] == "eng" and TOOL_DEST["get_bars"] == "px"
    assert TOOL_DEST["send_telegram"] == "tg"


async def test_every_mcp_resource_has_a_destination():
    mcp = build_mcp()
    uris = [str(r.uri) for r in await mcp.list_resources()]
    uris += [t.uri_template for t in await mcp.list_resource_templates()]
    schemes = {u.split("://", 1)[0] for u in uris}
    assert schemes <= set(RESOURCE_DEST), sorted(schemes - set(RESOURCE_DEST))
    assert destination("resource:portfolio://abc") == "db"
    assert destination("resource:markets://hours") == "px"


def test_destination_of_an_unknown_method_is_the_mcp():
    assert destination("no_such_tool") == "mcp"
    assert destination("get_quote") == "px"


def test_every_broker_kind_has_a_label():
    from smtrading.admin.routes_brokers import KINDS
    from smtrading.brokers.live import LIVE_KINDS
    assert set(KINDS) | set(LIVE_KINDS) <= set(BROKER_LABELS)
    assert BROKER_LABELS["alpaca_paper"] == "Alpaca (prova)"
    assert BROKER_LABELS["t212_live"] == "Trading 212 reale"


def test_static_nodes():
    assert set(STATIC_NODES) == {"pc", "mcp", "rules", "ng", "eng", "db", "px", "sync", "sim", "tg"}
    columns = {k: n["column"] for k, n in STATIC_NODES.items()}
    assert columns == {"pc": 1, "mcp": 2, "rules": 2, "ng": 3, "eng": 3, "db": 3, "px": 3,
                       "sync": 4, "sim": 4, "tg": 4}
    kinds = {"agent", "core", "db", "data", "ng", "tg", "broker"}
    for n in STATIC_NODES.values():
        assert n["kind"] in kinds and n["label"] and n["description"]
    assert set(DESTS) - {"mcp"} <= set(STATIC_NODES)
    assert ONE_WAY_DESTS == {"eng", "tg"}


def test_the_nightly_export_link_database_to_neurograph():
    from smtrading.flow.map import STATIC_LINKS, link_key
    assert ("db", "ng") in STATIC_LINKS
    assert link_key("db", "ng") == "db|ng" and link_key("ng", "db") == "db|ng"
    assert link_key("mcp", "ng") == "mcp|ng"  # get_forecast keeps its own link
