"""«Fiume dei flussi» v2: the price sources behind the «Prezzi» node (the lens on it).

From what SMTrading already publishes, nothing called outside:
- the stream manager's status (`md:stream_status`, marketdata.streaming): per stream
  (provider, asset class) its state and symbols, and the symbols polled instead;
- the connectors (enabled, last health check);
- the provider quota counters (marketdata.quota): the router's REST reads this minute and
  this hour, per provider.

    state   live («in diretta»: a stream connected), connecting («in collegamento»),
            refused («rifiutata»: the stream is backing off, refused or failing),
            on_demand («a richiesta»: read through the router when asked), unknown
            («stato non disponibile»: a streaming provider and no published status)

Stream ticks have no counter: `updates_per_min` is null for streams (the page shows the
symbols and the state without a rate); `reads_minute` / `reads_hour` are the router's own
reads. Names from `flow.map.PRICE_SOURCE_LABELS` (one source, guarded)."""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import smtrading.marketdata.providers  # noqa: F401  (registration)
from smtrading.autorules.feed import read_stream_status
from smtrading.common.logsetup import redact_log_text
from smtrading.connectors.models import Connector
from smtrading.flow.map import price_source_label
from smtrading.marketdata import quota
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.streaming import BACKOFF, CONNECTED, CONNECTING

log = logging.getLogger(__name__)

SYMBOL_LIST_MAX = 50
STATE_IT = {"live": "in diretta", "connecting": "in collegamento", "refused": "rifiutata",
            "on_demand": "a richiesta", "unknown": "stato non disponibile"}
_STREAM_STATE = {CONNECTED: "live", CONNECTING: "connecting", BACKOFF: "refused"}
_RANK = ("live", "connecting", "refused")


def can_stream(name: str) -> bool:
    cls = PROVIDERS.get(name)
    return cls is not None and cls.stream is not DataProvider.stream


async def stream_status(redis) -> dict | None:
    try:
        return await read_stream_status(redis)
    except Exception:
        log.warning("flow: stream status unreadable", exc_info=True)
        return None


def _streams_of(status: dict | None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for key, s in sorted(((status or {}).get("streams") or {}).items()):
        if not isinstance(s, dict) or ":" not in key:
            continue
        provider, asset_class = key.split(":", 1)
        symbols = [x for x in s.get("symbols") or [] if isinstance(x, str)]
        out.setdefault(provider, []).append({
            "asset_class": asset_class,
            "state": _STREAM_STATE.get(s.get("state"), "refused"),
            "symbols": symbols, "note": s.get("note")})
    return out


async def price_sources(session: AsyncSession, redis, *, now: datetime | None = None
                        ) -> dict[str, Any]:
    """{checked_at, stream_status, polling, sources: [...], disabled: [label, ...]}."""
    now = now or datetime.now(UTC)
    status = await stream_status(redis)
    streams = _streams_of(status)
    connectors = {c.name: c for c in await session.scalars(select(Connector))}
    enabled = sorted({n for n, c in connectors.items() if c.enabled and n in PROVIDERS}
                     | set(streams))
    try:
        used = await quota.usage(redis, enabled, now)
    except Exception:
        log.warning("flow: quota counters unreadable", exc_info=True)
        used = {}
    sources = []
    for name in enabled:
        mine = streams.get(name, [])
        if mine:
            state = min((s["state"] for s in mine), key=_RANK.index)
        elif can_stream(name) and status is None:
            state = "unknown"
        else:
            state = "on_demand"
        symbols = sorted({x for s in mine for x in s["symbols"]})
        notes = [s["note"] for s in mine if s["note"]]
        u = used.get(name)
        c = connectors.get(name)
        sources.append({
            "key": name, "label": price_source_label(name),
            "mode": "stream" if can_stream(name) else "request",
            "state": state, "state_it": STATE_IT[state],
            "symbols": len(symbols), "symbol_list": symbols[:SYMBOL_LIST_MAX],
            "updates_per_min": None,
            "reads_minute": u.minute if u else None, "reads_hour": u.hour if u else None,
            "note": "; ".join(notes) or None,
            "health": c.health_status if c else None,
            "last_error": (redact_log_text(c.last_error) if c and c.last_error
                           and c.health_status == "error" else None),
            "streams": [{"asset_class": s["asset_class"], "state": s["state"],
                         "symbols": len(s["symbols"]), "note": s["note"]} for s in mine],
        })
    order = {"live": 0, "connecting": 1, "refused": 2, "unknown": 3, "on_demand": 4}
    sources.sort(key=lambda s: (s["mode"] != "stream" or not s["streams"],
                                order[s["state"]], s["label"]))
    disabled = sorted(price_source_label(n) for n in PROVIDERS if n not in enabled)
    return {"checked_at": now, "stream_status": status is not None,
            "polling": sorted(x for x in (status or {}).get("polling") or []
                              if isinstance(x, str)),
            "sources": sources, "disabled": disabled}
