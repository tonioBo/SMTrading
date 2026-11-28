"""Guards of the «Fiume dei flussi» lens (spec v2 §5): every MCP tool and resource has a
family, every market-data provider of the router has a display name in the map module."""

from smtrading.flow.map import (
    FAMILIES,
    PRICE_SOURCE_LABELS,
    RESOURCE_DEST,
    RESOURCE_FAMILY,
    TOOL_DEST,
    TOOL_FAMILY,
    family,
    price_source_label,
)
from smtrading.mcp.server import build_mcp


def test_families_in_order():
    assert [(f["key"], f["label"]) for f in FAMILIES] == [
        ("orders", "ordini"), ("market", "dati di mercato"),
        ("risk", "rischio e previsioni"), ("research", "ricerca"),
        ("decisions", "decisioni e attività"), ("messages", "messaggi")]


async def test_every_mcp_tool_has_a_family():
    names = {t.name for t in await build_mcp().list_tools()}
    assert names <= set(TOOL_FAMILY), sorted(names - set(TOOL_FAMILY))
    assert set(TOOL_FAMILY) <= names, sorted(set(TOOL_FAMILY) - names)  # no stale entry
    assert set(TOOL_FAMILY) == set(TOOL_DEST)
    keys = {f["key"] for f in FAMILIES}
    assert set(TOOL_FAMILY.values()) <= keys
    assert set(TOOL_FAMILY.values()) == keys  # no empty family
    assert TOOL_FAMILY["place_order"] == "orders"
    assert TOOL_FAMILY["get_quote"] == "market"
    assert TOOL_FAMILY["get_forecast"] == "risk"
    assert TOOL_FAMILY["run_backtest"] == "research"
    assert TOOL_FAMILY["log_decision"] == "decisions"
    assert TOOL_FAMILY["send_telegram"] == "messages"


async def test_every_mcp_resource_has_a_family():
    mcp = build_mcp()
    uris = [str(r.uri) for r in await mcp.list_resources()]
    uris += [t.uri_template for t in await mcp.list_resource_templates()]
    schemes = {u.split("://", 1)[0] for u in uris}
    assert schemes <= set(RESOURCE_FAMILY), sorted(schemes - set(RESOURCE_FAMILY))
    assert set(RESOURCE_FAMILY) == set(RESOURCE_DEST)
    assert family("resource:markets://hours") == "market"
    assert family("get_bars") == "market"
    assert family("no_such_tool") is None


def test_every_market_data_provider_has_a_name():
    import smtrading.marketdata.providers  # noqa: F401  (registration)
    from smtrading.marketdata.registry import PROVIDERS

    assert set(PROVIDERS) <= set(PRICE_SOURCE_LABELS), sorted(set(PROVIDERS) - set(PRICE_SOURCE_LABELS))
    assert set(PRICE_SOURCE_LABELS) <= set(PROVIDERS)  # no stale entry
    assert all(PRICE_SOURCE_LABELS.values())
    assert price_source_label("alpaca") == "Alpaca"
    assert price_source_label("kraken") == "Kraken"
    assert price_source_label("nuovo") == "nuovo"
