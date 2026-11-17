"""The goals' status-change alerts (worker job, at start and every 5 minutes).

Each run first samples the benchmarks of the active beat_index goals
(`goals.benchmark.BenchmarkSampler`, with the market data router), then reads the
progress of every active goal (`goals.progress.active_progress`, current period not
over) and compares it with the last status seen for that goal and period
(`goal_alerts`, so a restart sends nothing again; the row keeps the goal's kind and
target, and starts over when either changes):

- min: becoming «in ritardo» → «🟠 Obiettivo <name> in ritardo: x% su y% (tempo trascorso
  z%)» and, with Paperclip on, an «OBIETTIVO …» issue to the CEO; back «in linea» → «🟢
  Obiettivo <name> di nuovo in linea: …», reaching it → «🎉 Obiettivo <name> raggiunto: …»;
- cap: becoming «oltre il tetto» → «🔴 …»; back under it → «🟢 … di nuovo sotto il tetto»;
- max_loss: «vicino al limite» → «🟠 …» once per period; «superato» → «🔴 …»; both open
  a «RISCHIO OBIETTIVO …» issue to the Risk Manager and one to the CEO (nothing is
  blocked: the agents decide); back from «superato» → «🟢 … di nuovo nel limite»;
- beat_index: no alert (its status is in the summaries and get_goals).

A warning is sent once per transition (`alerted` holds the warning announced and not
recovered); a recovery is announced only past a margin (hysteresis, so a goal hovering
at its threshold does not flip-flop): «in linea» again once pct ≥ expected − 0,25 pp
(«raggiunto» always), under the cap once pct ≤ cap − 0,25 pp, back in the limit once the
loss < limit − 0,5 pp. At most one recovery per local day per goal, period and warning
(`sent`): a second one the same day is held (the warning stays the last message, the
recovery is sent on a later run), so the last message never says «in linea» while the
goal is behind; a warning after a same-day recovery is always sent. The messages go to
Telegram as `system` (never the activity feed the agents read). The issues are
deduplicated per goal, period and kind (their keys in `goal_alerts.issues`). A refused
one (a 4xx answer, but 408 and 429) counts as an attempt (at most ISSUE_ATTEMPTS); an
outage (network, timeout, 5xx, 408/429) or an agent not found in Paperclip counts
nothing and is tried again with a backoff (5 minutes, doubling, at most an hour) while
its period lasts; after a timeout the retry first adopts an open issue with the same
title (`paperclip.issues`). The Paperclip configuration is read only when an issue is
due; every Paperclip and market data request is made with no transaction open."""

import asyncio
import copy
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.goals.benchmark import BenchmarkSampler
from smtrading.goals.models import Goal, GoalAlert
from smtrading.goals.progress import (
    ABOVE_CAP,
    BEHIND,
    BREACHED,
    NEAR_LIMIT,
    ON_TRACK,
    REACHED,
    WITHIN,
    active_progress,
    current_period,
    visible_goals,
)
from smtrading.paperclip.client import PaperclipError
from smtrading.paperclip.config import PaperclipConfig, load_config
from smtrading.paperclip.issues import RISK_MANAGER, PaperclipIssues, find_agent_id
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

INTERVAL_S = 300
ISSUE_ATTEMPTS = 3  # refused answers (4xx) before an issue is given up
RETRY_FIRST, RETRY_MAX = timedelta(minutes=5), timedelta(hours=1)
BACK_MARGIN_PP = Decimal("0.25")  # min / cap: recovery margin
LOSS_BACK_MARGIN_PP = Decimal("0.5")  # max_loss: back in the limit below limit − this
_REFUSED = re.compile(r"^HTTP 4(?!08|29)\d\d$")
CEO_NAMES = ("CEO SMTrading", "CEO")
CEO, RM = "CEO", "RM"
_Q2 = Decimal("0.01")

# event -> (warning status it announces | None for a recovery, emoji, level)
BEHIND_EV, BACK_EV = "behind", "back"
ABOVE_EV, BELOW_EV = "above_cap", "below_cap"
NEAR_EV, BREACHED_EV, WITHIN_EV = "near", "breached", "back_within"


# -- texts (pure) ------------------------------------------------------------------------


def _num(value: Decimal) -> str:
    q = abs(value.quantize(_Q2, rounding=ROUND_HALF_EVEN))
    return f"{q:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _signed(value: Decimal) -> str:
    q = value.quantize(_Q2, rounding=ROUND_HALF_EVEN)
    return ("+" if q > 0 else "-" if q < 0 else "") + _num(q)


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")


def label(p: dict[str, Any]) -> str:
    return p["name"] + (f" · {p['portfolio_name']}" if p["portfolio_name"] else "")


def _period(p: dict[str, Any]) -> str:
    return f"{p['period_start']:%d/%m}–{p['period_end']:%d/%m/%Y}"


def alert_text(event: str, p: dict[str, Any]) -> str:
    """The Telegram text of `event` for the progress `p`."""
    name, target = label(p), p["target_pct"]
    if event == BEHIND_EV:
        return (f"🟠 Obiettivo {name} in ritardo: {_signed(p['pct'])}% su +{_plain(target)}% "
                f"(tempo trascorso {_plain(p['elapsed_pct'])}%)")
    if event == BACK_EV and p["status"] == REACHED:
        return f"🎉 Obiettivo {name} raggiunto: {_signed(p['pct'])}% su +{_plain(target)}%"
    if event == BACK_EV:
        return f"🟢 Obiettivo {name} di nuovo in linea: {_signed(p['pct'])}% su +{_plain(target)}%"
    if event == ABOVE_EV:
        return (f"🔴 Obiettivo {name} oltre il tetto: {_signed(p['pct'])}% su un tetto di "
                f"+{_plain(target)}%")
    if event == BELOW_EV:
        return (f"🟢 Obiettivo {name} di nuovo sotto il tetto: {_signed(p['pct'])}% su un "
                f"tetto di +{_plain(target)}%")
    loss = f"perdita {_num(p['loss_pct'])}% su un limite del {_plain(target)}%"
    if event == NEAR_EV:
        return f"🟠 Obiettivo {name} vicino al limite: {loss}"
    if event == BREACHED_EV:
        return f"🔴 Obiettivo {name} limite superato: {loss} (nessun blocco automatico)"
    if event == WITHIN_EV:
        return f"🟢 Obiettivo {name} di nuovo nel limite: {loss}"
    raise ValueError(event)


def _figures(p: dict[str, Any]) -> str:
    lines = [f"- periodo: {_period(p)} (tempo trascorso {_plain(p['elapsed_pct'])}%)",
             f"- variazione: {_signed(p['pct'])}% · stato: {p['status']}"]
    if p["start_eur"] is not None and p["current_eur"] is not None:
        lines.append(f"- equity: da {_num(p['start_eur'])} € a {_num(p['current_eur'])} €")
    return "\n".join(lines)


def issue_for(event: str, p: dict[str, Any], role: str) -> dict[str, str]:
    """{title, description, priority} of the issue of `event` for `role` (CEO | RM)."""
    name = label(p)
    if event == BEHIND_EV:
        return {
            "title": f"OBIETTIVO {name} · in ritardo · {_period(p)}",
            "priority": "high",
            "description": (
                f"L'obiettivo «{p['name']}» (+{_plain(p['target_pct'])}%) è in ritardo.\n"
                f"{_figures(p)}\n\n"
                "Leggi `get_goals` e segui «Piano per gli obiettivi»: tappe, ricerca di "
                "nuove strategie se serve. Il rischio viene sempre prima: meglio sotto "
                "l'obiettivo che fuori dalle regole."),
        }
    what = "limite superato" if event == BREACHED_EV else "vicino al limite"
    who = "CEO" if role == CEO else "Risk Manager"
    return {
        "title": f"RISCHIO OBIETTIVO {name} · {what} · {_period(p)} · {who}",
        "priority": "critical" if event == BREACHED_EV else "high",
        "description": (
            f"L'obiettivo di rischio «{p['name']}» (perdita massima "
            f"{_plain(p['target_pct'])}% nel periodo) è {what}: perdita "
            f"{_num(p['loss_pct'])}%.\n{_figures(p)}\n\n"
            "Nessun blocco automatico: valuta subito con `get_goals`, `get_portfolio` e "
            "`get_risk_limits` come ridurre il rischio e non aprire nuove posizioni su "
            "questo ambito finché non l'avete rivisto. La decisione è vostra."),
    }


# -- transitions (pure) ------------------------------------------------------------------


WARNINGS = {BEHIND: BEHIND_EV, ABOVE_CAP: ABOVE_EV, BREACHED: BREACHED_EV}
RECOVERIES = {BEHIND: ({ON_TRACK, REACHED}, BACK_EV), ABOVE_CAP: ({ON_TRACK}, BELOW_EV),
              BREACHED: ({NEAR_LIMIT, WITHIN}, WITHIN_EV)}
KIND_WARNINGS = {"min": {BEHIND}, "cap": {ABOVE_CAP}, "max_loss": {BREACHED}}
ISSUES = {BEHIND_EV: (CEO,), NEAR_EV: (RM, CEO), BREACHED_EV: (RM, CEO)}


def recovered(alerted: str, p: dict[str, Any]) -> bool:
    """Whether the progress `p` recovers from the warning `alerted` (with the margins of
    the module docstring)."""
    statuses, _ = RECOVERIES[alerted]
    if p["status"] not in statuses:
        return False
    target, pct = p["target_pct"], p["pct"]
    if alerted == BEHIND:
        return (p["status"] == REACHED
                or pct >= target * p["elapsed_pct"] / 100 - BACK_MARGIN_PP)
    if alerted == ABOVE_CAP:
        return pct <= target - BACK_MARGIN_PP
    return p["loss_pct"] < target - LOSS_BACK_MARGIN_PP


def step(row: GoalAlert, p: dict[str, Any], today: date) -> list[str]:
    """The events of the progress `p` for the alert state `row` (updated in place)."""
    kind, status = p["kind"], p["status"]
    events: list[str] = []
    sent = dict(row.sent or {})
    if row.alerted is not None and recovered(row.alerted, p):
        event = RECOVERIES[row.alerted][1]
        if sent.get(event) != today.isoformat() or status == REACHED:
            events.append(event)
            sent[event] = today.isoformat()
            row.alerted = None
    if status in KIND_WARNINGS.get(kind, ()) and row.alerted != status:
        event = WARNINGS[status]
        events.append(event)
        sent[event] = today.isoformat()
        row.alerted = status
    if kind == "max_loss":
        if status == NEAR_LIMIT and not row.near_sent and row.alerted is None:
            events.append(NEAR_EV)
        if status in (NEAR_LIMIT, BREACHED):
            row.near_sent = True
    row.status = status
    row.sent = sent
    return events


def _due(entry: dict, now: datetime) -> bool:
    """A queued issue still to create, whose retry time has come."""
    if entry.get("ident") or int(entry.get("attempts") or 0) >= ISSUE_ATTEMPTS:
        return False
    retry_at = entry.get("retry_at")
    return retry_at is None or datetime.fromisoformat(retry_at) <= now


def _later(entry: dict, now: datetime) -> None:
    """A transient failure: no attempt counted, tried again after the backoff."""
    retries = int(entry.get("retries") or 0)
    entry["retries"] = retries + 1
    entry["retry_at"] = (now + min(RETRY_FIRST * 2 ** retries, RETRY_MAX)).isoformat()


# -- the job -----------------------------------------------------------------------------

IssuesFactory = Callable[[PaperclipConfig], PaperclipIssues]


def _usable(cfg: PaperclipConfig) -> bool:
    return bool(cfg.enabled and cfg.complete and not cfg.unreadable)


async def _paperclip_on(session: AsyncSession) -> bool:
    return _usable(await load_config(session))


class GoalAlerts:
    def __init__(self, sessionmaker: async_sessionmaker, router=None, *,
                 issues_factory: IssuesFactory | None = None,
                 clock: Callable[[], datetime] | None = None, tz: str | None = None,
                 interval_s: float = INTERVAL_S) -> None:
        self._sm = sessionmaker
        self._router = router  # None: no benchmark sampling
        self._issues_factory = issues_factory or (lambda cfg: PaperclipIssues(cfg))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._zone = ZoneInfo(tz or get_settings().tz)
        self._interval_s = interval_s
        self._sampler = BenchmarkSampler(sessionmaker, router, self._zone)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("goal alerts failed; retrying in %ss", self._interval_s,
                            exc_info=True)
            await asyncio.sleep(self._interval_s)

    async def _benchmark_needs(self, today: date) -> dict[str, set]:
        """Benchmark -> the starts of the current periods of the beat_index goals."""
        async with self._sm() as session:
            goals = list(await session.scalars(
                select(Goal).where(Goal.active.is_(True), Goal.kind == "beat_index")))
        needs: dict[str, set] = {}
        for g in goals:
            needs.setdefault(g.benchmark, set()).add(current_period(g, today).start)
        return needs

    async def tick(self) -> None:
        now = self._clock()
        today = now.astimezone(self._zone).date()
        if self._router is not None:
            try:
                needs = await self._benchmark_needs(today)
                await self._sampler.run(needs, now)
            except Exception:
                log.warning("benchmark sampling failed", exc_info=True)
        due = False
        async with self._sm() as session, session.begin():
            paperclip: bool | None = None
            for p in await active_progress(session, now, self._zone):
                if today > p["period_end"]:
                    continue
                row = await session.get(GoalAlert, (p["goal_id"], p["period_start"]),
                                        with_for_update=True)
                if row is None:
                    row = GoalAlert(goal_id=p["goal_id"], period_start=p["period_start"],
                                    status=p["status"], kind=p["kind"],
                                    target_pct=p["target_pct"], near_sent=False, sent={},
                                    issues={})
                    session.add(row)
                elif row.kind != p["kind"] or row.target_pct != p["target_pct"]:
                    # the goal was changed: its alerts start over
                    row.kind, row.target_pct = p["kind"], p["target_pct"]
                    row.alerted, row.near_sent, row.sent, row.issues = None, False, {}, {}
                issues = copy.deepcopy(row.issues or {})
                for event in step(row, p, today):
                    await enqueue(session, source="system", category="system",
                                  level="info" if event in (BACK_EV, BELOW_EV, WITHIN_EV)
                                  else "warning", text=alert_text(event, p))
                    roles = ISSUES.get(event, ())
                    if roles and paperclip is None:
                        paperclip = await _paperclip_on(session)
                    for role in roles if paperclip else ():
                        key = f"{event}:{role}"
                        if key not in issues:
                            issues[key] = {**issue_for(event, p, role), "assignee": role,
                                           "ident": None, "attempts": 0, "uncertain": False}
                row.issues = issues
                due = due or any(_due(e, now) for e in issues.values())
        if due:
            await self._create_issues(today, now)

    async def _pending(self, today: date, now: datetime) -> list[tuple]:
        """(goal_id, period_start, key, entry) of the issues due, current periods only."""
        async with self._sm() as session:
            live = {}
            for goal, _ in await visible_goals(session):
                period = current_period(goal, today)
                if today <= period.end:
                    live[goal.id] = period.start
            if not live:
                return []
            rows = list(await session.scalars(
                select(GoalAlert).where(GoalAlert.goal_id.in_(list(live)))))
        return [(r.goal_id, r.period_start, key, dict(entry))
                for r in rows if live.get(r.goal_id) == r.period_start
                for key, entry in (r.issues or {}).items() if _due(entry, now)]

    async def _create_issues(self, today: date, now: datetime) -> None:
        pending = await self._pending(today, now)
        if not pending:
            return
        async with self._sm() as session:
            cfg = await load_config(session)
        if not _usable(cfg):
            return
        # key -> ("created", ident) | ("refused", None) | ("later", timed_out)
        results: dict[tuple, tuple[str, Any]] = {}
        try:
            async with self._issues_factory(cfg) as client:
                agents = await client.agents()
                ids = {RM: find_agent_id(agents, RISK_MANAGER),
                       CEO: next((a for a in (find_agent_id(agents, n) for n in CEO_NAMES)
                                  if a), None)}
                for goal_id, start, key, entry in pending:
                    assignee = ids.get(entry.get("assignee"))
                    if assignee is None:
                        log.warning("goal issue: agent %s not found in Paperclip",
                                    entry.get("assignee"))
                        results[(goal_id, start, key)] = ("later", False)
                        continue
                    try:
                        found = (await client.open_issue_with_title(entry["title"])
                                 if entry.get("uncertain") else None)
                        ident = found or await client.create_issue(
                            title=entry["title"], description=entry["description"],
                            assignee_agent_id=assignee,
                            priority=entry.get("priority") or "high")
                        results[(goal_id, start, key)] = ("created", ident)
                    except PaperclipError as e:
                        log.warning("goal issue not created in Paperclip: %s", e.message)
                        results[(goal_id, start, key)] = (
                            ("refused", None) if _REFUSED.match(e.message)
                            else ("later", e.message == "timeout"))
        except PaperclipError as e:
            log.warning("goal issues: Paperclip unavailable: %s", e.message)
        except Exception:
            log.warning("goal issues: unexpected error", exc_info=True)
        for goal_id, start, key, _ in pending:
            results.setdefault((goal_id, start, key), ("later", False))
        async with self._sm() as session, session.begin():
            for (goal_id, start, key), (outcome, value) in results.items():
                row = await session.get(GoalAlert, (goal_id, start), with_for_update=True)
                if row is None or key not in (row.issues or {}):
                    continue
                issues = copy.deepcopy(row.issues)
                entry = issues[key]
                if entry.get("ident"):
                    continue
                if outcome == "created" and value:
                    entry.update(ident=value, uncertain=False, retry_at=None)
                elif outcome == "refused":
                    entry["attempts"] = int(entry.get("attempts") or 0) + 1
                else:
                    if value:
                        entry["uncertain"] = True
                    _later(entry, now)
                row.issues = issues
