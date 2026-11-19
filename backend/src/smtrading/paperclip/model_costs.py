"""Agent costs per model («Costi agenti» → «Per modello»), from the Paperclip heartbeat
runs the reader already reads (GET only), kept in `app_settings["paperclip_models"]`:

- `buckets`: {local day: {Paperclip agent id: {model: totals}}}; totals are the USD
  amounts per billing bucket (as `agent_cost_days`), the runs, the first and last run
  time and how many runs had their model *derived* (see below). The last KEEP_DAYS days
  are kept; every reader pass replaces the days it read completely (the oldest day of a
  capped answer is incomplete and left as stored, as for `agent_cost_days`).
- `agents`: per Paperclip agent its name and model history (`adapterConfig.model` of
  `GET …/agents`, the model the agent is configured with), each entry with the time it
  was first seen (`since`, null for the first one ever read);
- `changes`: the model changes seen (agent, from, to, at): `at` is the agent's
  `updatedAt` when it falls between the previous read and this one, else this read.

The model of a run is `usageJson.model` (what Paperclip itself records in its cost
events); a run split over several models (`usageJson.usageByModel`, each part with its
`model` and `costUsd`) is split the same way, the run counted once for its costliest
part. A run without a model (an older Paperclip, a run of an agent the key cannot read
in full) gets the agent's configured model at the time of the run, from the history
(derived, counted in `derived`); before the first read of the agents the first model
seen stands in for it. No history either: «non indicato».

Spend is what Paperclip bills, as for the budget: «a consumo API» + «altro»; the runs
«incluse nell'abbonamento» are counted (runs, full cost) but not billed."""

import copy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.paperclip.costs import _amount, billed_usd, bucket, run_time
from smtrading.risk.models import AppSetting

MODELS_KEY = "paperclip_models"
KEEP_DAYS = 120
MAX_CHANGES = 50
MAX_HISTORY = 20
UNKNOWN = "non indicato"
CENT = Decimal("0.01")
MICRO = Decimal("0.000001")

DEFAULT_MODELS: dict[str, Any] = {"buckets": {}, "agents": {}, "changes": [],
                                  "updated_at": None}


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def agent_model(agent: dict) -> str | None:
    """The model the agent is configured with (`adapterConfig.model`), None if unset."""
    cfg = agent.get("adapterConfig")
    model = cfg.get("model") if isinstance(cfg, dict) else None
    return model.strip()[:128] if isinstance(model, str) and model.strip() else None


# -- agents' configured model -------------------------------------------------------------

def update_agents(stored: dict[str, Any], remote: list[dict], now: datetime,
                  last_read: datetime | None) -> list[dict[str, Any]]:
    """Updates `stored["agents"]` with the agents read now; returns the new changes."""
    agents = stored.setdefault("agents", {})
    changes: list[dict[str, Any]] = []
    for a in remote:
        pid = str(a.get("id") or "")
        if not pid:
            continue
        model = agent_model(a)
        entry = agents.setdefault(pid, {"name": None, "history": []})
        entry["name"] = str(a.get("name") or entry.get("name") or pid)[:128]
        history = entry.setdefault("history", [])
        if model is None:
            continue
        if not history:
            history.append({"model": model, "since": None})
            continue
        if history[-1]["model"] == model:
            continue
        updated = _parse(a.get("updatedAt"))
        at = updated if updated is not None and updated <= now and (
            last_read is None or updated > last_read) else now
        changes.append({"agent_id": pid, "name": entry["name"], "from": history[-1]["model"],
                        "to": model, "at": at.isoformat()})
        history.append({"model": model, "since": at.isoformat()})
        del history[:-MAX_HISTORY]
    if changes:
        stored["changes"] = (list(stored.get("changes") or []) + changes)[-MAX_CHANGES:]
    return changes


def model_at(history: list[dict[str, Any]], ts: datetime) -> str | None:
    """The configured model at `ts` (the first one seen for earlier times)."""
    found = None
    for h in history or []:
        since = _parse(h.get("since"))
        if since is None or since <= ts:
            found = h.get("model")
        elif found is None:
            return h.get("model")
    return found


# -- runs → buckets ---------------------------------------------------------------------

def _empty() -> dict[str, Any]:
    return {"api_usd": "0", "subscription_usd": "0", "other_usd": "0", "runs": 0,
            "derived": 0, "first_at": None, "last_at": None}


def _add(totals: dict[str, Any], field: str, amount: Decimal) -> None:
    totals[field] = str(Decimal(totals[field]) + amount)


def _parts(usage: dict) -> list[tuple[str, Decimal]]:
    parts = usage.get("usageByModel")
    if not isinstance(parts, list):
        return []
    out = []
    for p in parts:
        if not isinstance(p, dict):
            return []
        model, cost = p.get("model"), _amount(p.get("costUsd"))
        if not isinstance(model, str) or not model.strip() or cost is None:
            return []
        out.append((model.strip()[:128], cost))
    return out


def bucket_runs(runs: list[dict], tz: str, agents: dict[str, Any], *, truncated: bool
                ) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """{day ISO: {agent id: {model: totals}}} of `runs` (local days in `tz`); with
    `truncated` the day of the oldest run is dropped (incomplete)."""
    zone = ZoneInfo(tz)
    out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    oldest: datetime | None = None
    for run in runs:
        if not isinstance(run, dict):
            continue
        agent, ts = run.get("agentId"), run_time(run)
        if not agent or ts is None:
            continue
        agent = str(agent)
        oldest = ts if oldest is None or ts < oldest else oldest
        usage = run.get("usageJson") if isinstance(run.get("usageJson"), dict) else {}
        amount = billed_usd(usage)
        field = f"{bucket(usage.get('billingType'))}_usd"
        parts = _parts(usage)
        model = usage.get("model").strip()[:128] if isinstance(usage.get("model"), str) \
            and usage.get("model").strip() else None
        derived = False
        if parts and amount is not None and sum(c for _, c in parts) > 0:
            # split as Paperclip does: each part its reported cost, the total kept
            total = sum(c for _, c in parts)
            shares = [(m, amount * c / total) for m, c in parts]
            main = model or max(parts, key=lambda p: p[1])[0]
        else:
            if model is None:
                model = model_at((agents.get(agent) or {}).get("history") or [], ts)
                derived = model is not None
            main = model or UNKNOWN
            shares = [(main, amount)] if amount is not None else []
        day = out.setdefault(ts.astimezone(zone).date().isoformat(), {}) \
            .setdefault(agent, {})
        for m, share in shares:
            _add(day.setdefault(m, _empty()), field, share.quantize(MICRO))
        totals = day.setdefault(main, _empty())
        totals["runs"] += 1
        totals["derived"] += int(derived)
        iso = ts.astimezone(UTC).isoformat()
        if totals["first_at"] is None or iso < totals["first_at"]:
            totals["first_at"] = iso
        if totals["last_at"] is None or iso > totals["last_at"]:
            totals["last_at"] = iso
    if truncated and oldest is not None:
        out.pop(oldest.astimezone(zone).date().isoformat(), None)
    return out


def merge_buckets(stored: dict[str, Any], fresh: dict[str, Any], today: date) -> None:
    buckets = stored.setdefault("buckets", {})
    buckets.update(fresh)
    first = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for day in [d for d in buckets if d < first]:
        del buckets[day]


# -- storage ----------------------------------------------------------------------------

async def load_models(session: AsyncSession, *, for_update: bool = False) -> dict[str, Any]:
    q = select(AppSetting.value).where(AppSetting.key == MODELS_KEY)
    raw = await session.scalar(q.with_for_update() if for_update else q)
    state = copy.deepcopy(DEFAULT_MODELS)
    if isinstance(raw, dict):
        state.update({k: v for k, v in raw.items() if k in DEFAULT_MODELS})
    return state


async def save_models(session: AsyncSession, state: dict[str, Any]) -> None:
    value = {k: state.get(k, v) for k, v in DEFAULT_MODELS.items()}
    stmt = insert(AppSetting).values(key=MODELS_KEY, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                                     set_={"value": value}))
    await session.flush()


async def record_models(session: AsyncSession, runs: list[dict], remote_agents: list[dict],
                        *, tz: str, now: datetime, truncated: bool) -> None:
    """One reader pass: the agents' models first (a run without a model of this pass
    gets the model just read), then the runs."""
    state = await load_models(session, for_update=True)
    update_agents(state, remote_agents, now, _parse(state.get("updated_at")))
    fresh = bucket_runs(runs, tz, state["agents"], truncated=truncated)
    merge_buckets(state, fresh, now.astimezone(ZoneInfo(tz)).date())
    state["updated_at"] = now.isoformat()
    await save_models(session, state)


# -- reports ----------------------------------------------------------------------------

def _dec(v: Any) -> Decimal:
    return Decimal(str(v or 0))


class _Acc:
    def __init__(self) -> None:
        self.api = self.sub = self.other = Decimal(0)
        self.runs = self.derived = 0
        self.agents: set[str] = set()

    def add(self, totals: dict[str, Any], agent: str) -> None:
        self.api += _dec(totals.get("api_usd"))
        self.sub += _dec(totals.get("subscription_usd"))
        self.other += _dec(totals.get("other_usd"))
        self.runs += int(totals.get("runs") or 0)
        self.derived += int(totals.get("derived") or 0)
        self.agents.add(agent)

    def dto(self, model: str) -> dict[str, Any]:
        billed = self.api + self.other
        full = billed + self.sub
        return {
            "model": model,
            "billed_usd": billed.quantize(CENT), "api_usd": self.api.quantize(CENT),
            "subscription_usd": self.sub.quantize(CENT), "other_usd": self.other.quantize(CENT),
            "runs": self.runs, "derived_runs": self.derived,
            "billed_per_run_usd": (billed / self.runs).quantize(Decimal("0.0001"))
            if self.runs else None,
            "full_per_run_usd": (full / self.runs).quantize(Decimal("0.0001"))
            if self.runs else None,
            "agents": sorted(self.agents),
        }


def _names(state: dict[str, Any]) -> dict[str, str]:
    return {pid: (a.get("name") or pid) for pid, a in (state.get("agents") or {}).items()}


def _report(items: list[tuple[str, str, dict[str, Any]]], names: dict[str, str]
            ) -> list[dict[str, Any]]:
    accs: dict[str, _Acc] = {}
    for agent, model, totals in items:
        accs.setdefault(model, _Acc()).add(totals, names.get(agent, agent))
    rows = [acc.dto(m) for m, acc in accs.items()]
    return sorted(rows, key=lambda r: (-r["billed_usd"], -r["runs"], r["model"]))


def _items(state: dict[str, Any], keep) -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for day, agents in (state.get("buckets") or {}).items():
        for agent, models in (agents or {}).items():
            for model, totals in (models or {}).items():
                if keep(day, agent, model, totals):
                    out.append((agent, model, totals))
    return out


def by_model(state: dict[str, Any], from_: date, to: date) -> list[dict[str, Any]]:
    lo, hi = from_.isoformat(), to.isoformat()
    return _report(_items(state, lambda d, *_: lo <= d <= hi), _names(state))


def current_models(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Per Paperclip agent its model now and since when (null: since before the first
    read)."""
    rows = []
    for pid, a in (state.get("agents") or {}).items():
        history = a.get("history") or []
        rows.append({"paperclip_agent_id": pid, "name": a.get("name") or pid,
                     "model": history[-1]["model"] if history else None,
                     "since": history[-1].get("since") if history else None})
    return sorted(rows, key=lambda r: r["name"])


def compare(state: dict[str, Any], at: datetime, days: int, tz: str, now: datetime
            ) -> dict[str, Any]:
    """Per model, the `days` local days before `at` against the time from `at` on (at
    most `days` days). The day of `at` is split by run time: a model's runs of that day
    all before `at` go before, all from `at` on go after; one with runs on both sides
    is reported in `straddling` and left out (the stored totals are per day)."""
    zone = ZoneInfo(tz)
    local = at.astimezone(zone)
    at_day = local.date()
    first = (at_day - timedelta(days=days)).isoformat()
    last = (at_day + timedelta(days=days)).isoformat()
    iso_at = at.astimezone(UTC).isoformat()
    before, after, straddling = [], [], []
    for day, agents in (state.get("buckets") or {}).items():
        for agent, models in (agents or {}).items():
            for model, totals in (models or {}).items():
                item = (agent, model, totals)
                if first <= day < at_day.isoformat():
                    before.append(item)
                elif at_day.isoformat() < day <= last:
                    after.append(item)
                elif day == at_day.isoformat():
                    if (totals.get("last_at") or "") < iso_at:
                        before.append(item)
                    elif (totals.get("first_at") or "") >= iso_at:
                        after.append(item)
                    else:
                        straddling.append({"agent": agent, "model": model,
                                           "runs": int(totals.get("runs") or 0)})
    names = _names(state)
    for s in straddling:
        s["agent"] = names.get(s["agent"], s["agent"])
    end = min(at + timedelta(days=days), now)
    return {
        "at": at.isoformat(), "days": days,
        "before": {"from": (at_day - timedelta(days=days)).isoformat(), "to": iso_at,
                   "models": _report(before, names)},
        "after": {"from": iso_at, "to": end.astimezone(UTC).isoformat(),
                  "models": _report(after, names)},
        "straddling": straddling,
    }
