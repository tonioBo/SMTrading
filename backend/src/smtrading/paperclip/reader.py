"""Paperclip reader (worker job, every 5 minutes, GET requests only).

1. Costs: the last heartbeat runs, grouped per local day and agent, upserted into
   `agent_cost_days`.
2. Isolation: the «Isolate … SMTrading» tool policies against the baseline (taken on the
   first read, reconfirmed by the admin). Removed or modified policies send one critical
   Telegram message (category `system`) per new signature; added ones are only listed.
3. Errors: one critical message at the third consecutive failed read, none after it until
   a read succeeds.
4. Board key expiry: one notice 7 days before `expiresAt` (none when there is no expiry).

Every request is made before the transaction is opened (R9). Nothing goes to the agents'
activity feed."""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.paperclip.client import MAX_RUNS, PaperclipClient, PaperclipError
from smtrading.paperclip.config import PaperclipConfig, load_config, load_state, save_state
from smtrading.paperclip.costs import daily_costs
from smtrading.paperclip.models import AgentCostDay
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

POLICY_KEYS = ("id", "policyType", "selectors", "conditions", "config", "priority",
               "enabled", "name")
ISOLATION_NAME = re.compile(r"^Isolate .*SMTrading")
NO_POLICIES = ("nessuna regola di isolamento trovata: controlla l'id azienda e i permessi "
               "della chiave")
ERRORS_BEFORE_ALERT = 3
EXPIRY_NOTICE = timedelta(days=7)

ClientFactory = Callable[[PaperclipConfig], PaperclipClient]


# -- policies (pure) ------------------------------------------------------------------

def normalize_policy(p: dict) -> dict:
    out = {k: p.get(k) for k in POLICY_KEYS}
    out["id"] = str(p.get("id") or p.get("policyId") or "")
    return out


def isolation_policies(policies: list[dict]) -> list[dict]:
    """The normalized SMTrading isolation policies, sorted by id: filtered on the name
    when the policies have one, all of them otherwise."""
    items = [normalize_policy(p) for p in policies]
    items = [p for p in items if p["id"]]
    if any(p.get("name") for p in items):
        items = [p for p in items if isinstance(p.get("name"), str)
                 and ISOLATION_NAME.match(p["name"])]
    return sorted(items, key=lambda p: p["id"])


def diff_policies(baseline: list[dict], current: list[dict]) -> dict[str, list[str]]:
    old = {p["id"]: p for p in baseline}
    new = {p["id"]: p for p in current}
    return {
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(i for i in set(old) & set(new) if old[i] != new[i]),
        "added": sorted(set(new) - set(old)),
    }


def _signature(changes: dict[str, list[str]], current: list[dict]) -> str:
    by_id = {p["id"]: p for p in current}
    payload = {"removed": changes["removed"],
               "changed": [by_id[i] for i in changes["changed"]]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def isolation_text(changes: dict[str, list[str]]) -> str:
    return (f"🔴 Paperclip: le regole di isolamento di SMTrading sono cambiate "
            f"({len(changes['removed'])} rimosse, {len(changes['changed'])} modificate). "
            f"Controlla Paperclip, poi premi «Riconferma» nel portale.")


def set_baseline(state: dict, policies: list[dict], now: datetime) -> None:
    state.update(baseline=policies, baseline_at=now.isoformat(), isolation="ok",
                 changes={"removed": [], "changed": [], "added": []}, alerted_signature=None)


# -- key expiry -------------------------------------------------------------------------

def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


async def _key_expiry(client: PaperclipClient, cfg: PaperclipConfig) -> tuple[bool, str | None]:
    """(known, expiresAt ISO or None). Best-effort: any failure is «not known»."""
    try:
        key_id = cfg.board_key_id or await client.current_key_id()
        if not key_id:
            return False, None
        for k in await client.board_api_keys():
            if str(k.get("id")) == key_id:
                ts = _parse_ts(k.get("expiresAt"))
                return True, ts.isoformat() if ts else None
    except PaperclipError:
        pass
    return False, None


def expiry_text(expires: datetime, now: datetime) -> str:
    when = expires.astimezone(ZoneInfo(get_settings().tz)).strftime("%d/%m/%Y")
    if expires <= now:
        return (f"🔴 Paperclip: la chiave board è scaduta il {when}. Creane una nuova e "
                f"aggiornala nel portale.")
    return (f"🟠 Paperclip: la chiave board scade il {when}. Creane una nuova e aggiornala "
            f"nel portale.")


def key_expiring(state: dict, now: datetime) -> bool:
    expires = _parse_ts(state.get("key_expires_at"))
    return expires is not None and now >= expires - EXPIRY_NOTICE


# -- the job ----------------------------------------------------------------------------

class PaperclipReader:
    def __init__(self, sessionmaker: async_sessionmaker, *,
                 client_factory: ClientFactory | None = None,
                 clock: Callable[[], datetime] | None = None, interval_s: float = 300) -> None:
        self._sm = sessionmaker
        self._client_factory = client_factory or (lambda cfg: PaperclipClient(cfg))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("paperclip read failed; retrying in %ss", self._interval_s,
                            exc_info=True)
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> None:
        async with self._sm() as session:
            cfg = await load_config(session)
        if not cfg.enabled:
            async with self._sm() as session, session.begin():
                state = await load_state(session, for_update=True)
                if state["isolation"] != "off":
                    state["isolation"] = "off"
                    await save_state(session, state)
            return
        try:
            fetched = await self._fetch(cfg)
        except PaperclipError as e:
            await self._record_error(e.message)
            return
        await self._record(fetched)

    async def _fetch(self, cfg: PaperclipConfig) -> dict[str, Any]:
        """Every request of the tick (outside any transaction)."""
        if cfg.unreadable:
            raise PaperclipError("chiave board illeggibile: reinseriscila")
        async with self._client_factory(cfg) as client:
            runs = await client.heartbeat_runs(MAX_RUNS)
            policies = await client.tool_policies()
            try:
                names = {str(a.get("id")): str(a.get("name"))
                         for a in await client.agents() if a.get("id") and a.get("name")}
            except PaperclipError:
                names = {}  # the id stands for the name
            expiry_known, expires_at = await _key_expiry(client, cfg)
        return {"runs": runs, "policies": isolation_policies(policies), "names": names,
                "expiry_known": expiry_known, "expires_at": expires_at}

    async def _record_error(self, message: str) -> None:
        async with self._sm() as session, session.begin():
            state = await load_state(session, for_update=True)
            state["consecutive_errors"] = int(state["consecutive_errors"] or 0) + 1
            state["last_error"] = message
            if state["consecutive_errors"] >= ERRORS_BEFORE_ALERT and not state["error_alerted"]:
                await enqueue(session, source="system", category="system", level="critical",
                              text=f"🔴 Paperclip non raggiungibile da 3 letture: {message}")
                state["error_alerted"] = True
            await save_state(session, state)
        log.warning("paperclip read failed: %s", message)

    async def _record(self, fetched: dict[str, Any]) -> None:
        now = self._clock()
        async with self._sm() as session, session.begin():
            state = await load_state(session, for_update=True)
            await _upsert_costs(session, fetched["runs"], fetched["names"])
            state.update(consecutive_errors=0, error_alerted=False, last_error=None,
                         last_ok_at=now.isoformat())
            await self._check_isolation(session, state, fetched["policies"], now)
            if fetched["expiry_known"]:
                state["key_expires_at"] = fetched["expires_at"]
            await self._check_expiry(session, state, now)
            await save_state(session, state)

    async def _check_isolation(self, session: AsyncSession, state: dict,
                               policies: list[dict], now: datetime) -> None:
        if state["baseline"] is None:
            if policies:
                set_baseline(state, policies, now)
            else:
                state.update(isolation="unknown", last_error=NO_POLICIES)
            return
        changes = diff_policies(state["baseline"], policies)
        state["changes"] = changes
        if not (changes["removed"] or changes["changed"]):
            state.update(isolation="ok", alerted_signature=None)
            return
        state["isolation"] = "changed"
        signature = _signature(changes, policies)
        if signature != state["alerted_signature"]:
            await enqueue(session, source="system", category="system", level="critical",
                          text=isolation_text(changes))
            state["alerted_signature"] = signature

    async def _check_expiry(self, session: AsyncSession, state: dict, now: datetime) -> None:
        expires_at = state.get("key_expires_at")
        if not key_expiring(state, now) or state.get("expiry_alerted") == expires_at:
            return
        expires = _parse_ts(expires_at)
        if expires is None:
            return
        await enqueue(session, source="system", category="system",
                      level="critical" if expires <= now else "warning",
                      text=expiry_text(expires, now))
        state["expiry_alerted"] = expires_at


async def _upsert_costs(session: AsyncSession, runs: list[dict], names: dict[str, str]) -> None:
    rows = daily_costs(runs, get_settings().tz, truncated=len(runs) >= MAX_RUNS)
    if not rows:
        return
    values = [{"day": day, "paperclip_agent_id": agent[:64],
               "paperclip_agent_name": names.get(agent, agent)[:128],
               "api_usd": r.api_usd, "subscription_usd": r.subscription_usd,
               "other_usd": r.other_usd, "runs": r.runs}
              for (day, agent), r in rows.items()]
    stmt = insert(AgentCostDay).values(values)
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["day", "paperclip_agent_id"],
        set_={c: getattr(stmt.excluded, c) for c in
              ("paperclip_agent_name", "api_usd", "subscription_usd", "other_usd", "runs")}
        | {"updated_at": datetime.now(UTC)},
    ))
