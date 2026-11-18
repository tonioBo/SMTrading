"""Hand-off watchdog (worker job, at most one pass every 5 minutes, GET requests only
through the reader's `PaperclipClient`): checks that the hand-offs of
`docs/paperclip-agents/HANDOFFS.md` happen and warns the humans on Telegram when one is
missing («⚠ Passaggio mancante: …», level warning, category `system`, never the activity
feed the agents read). Local time is `get_settings().tz`.

On market days (Monday to Friday, at least one exchange of the portfolios trading per
`market_hours.session_on`; the exchanges are the calendar markets of the non-archived
portfolios' `allowed_markets`, the main ones when the list is empty; crypto does not
count):
- from 08:50: an issue «AGENDA <date>» assigned to the Portfolio Manager;
- from 09:20: an issue «BUONGIORNO <date>» assigned to the Notifier and, once it exists,
  a Telegram «Buongiorno – piano di oggi» queued today (outbox, not failed or skipped).
Every day:
- an open «PROTEGGI · …» / «RISCHIO OBIETTIVO …» issue older than REPLY_WITHIN with no
  comment (comments read only for these candidates, at most MAX_COMMENT_READS a pass; an
  issue found with a comment is not read again);
- an open «ESEGUI …» issue older than EXECUTE_WITHIN while the market of its symbol (the
  third part of the title; unknown: the portfolios' exchanges) is open.

Each miss warns once per item and local day; the warned items and the last pass live in
`app_settings["handoff_watchdog_state"]`, so a restart neither warns again nor reads
Paperclip sooner. Off with `app_settings["handoff_watchdog"]` or with the Paperclip reader
disabled; a failed read is only logged (the reader alerts on repeated failures). Every
Paperclip request is made with no transaction open."""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.errors import SMTError
from smtrading.instruments.market_hours import CALENDARS, market_status, session_on
from smtrading.instruments.models import Instrument
from smtrading.paperclip.client import PaperclipClient, PaperclipError
from smtrading.paperclip.config import PaperclipConfig, load_config
from smtrading.paperclip.issues import CLOSED_STATUSES, find_agent_id
from smtrading.paperclip.reader import _parse_ts
from smtrading.paperclip.watchdog_settings import watchdog_on
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.models import AppSetting, RiskProfile
from smtrading.telegram.models import OutboxStatus, TelegramOutbox
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

STATE_KEY = "handoff_watchdog_state"
INTERVAL_S = 300
ISSUES_LIMIT = 500
AGENDA_BY = time(8, 50)
BUONGIORNO_BY = time(9, 20)
REPLY_WITHIN = timedelta(minutes=45)
EXECUTE_WITHIN = timedelta(minutes=30)
MAX_COMMENT_READS = 20
AGENTS_TTL = timedelta(hours=1)
DEFAULT_MARKETS = ("NYSE", "NASDAQ", "MTA", "XETRA")
PORTFOLIO_MANAGER, NOTIFIER = "Portfolio Manager", "Notifier"
REPLY_PREFIXES = ("PROTEGGI ·", "RISCHIO OBIETTIVO ")
EXECUTE_PREFIX = "ESEGUI "
BUONGIORNO_TEXT = "%buongiorno%piano di oggi%"
TITLE_MAX = 120
MAX_WARNED = 1000
PREFIX = "⚠ Passaggio mancante: "

ClientFactory = Callable[[PaperclipConfig], PaperclipClient]


@dataclass(frozen=True)
class Miss:
    key: str
    text: str


# -- pure checks --------------------------------------------------------------------------

def _open(issue: dict) -> bool:
    return str(issue.get("status") or "").lower() not in CLOSED_STATUSES


def _title(issue: dict) -> str:
    title = " ".join(str(issue.get("title") or "").split())
    return title if len(title) <= TITLE_MAX else title[:TITLE_MAX - 1] + "…"


def _ident(issue: dict) -> str:
    return str(issue.get("identifier") or issue.get("id") or "?")


def _age(issue: dict, now: datetime) -> timedelta | None:
    created = _parse_ts(issue.get("createdAt"))
    return None if created is None else now - created


def _titled(issues: list[dict], head: str) -> list[dict]:
    pattern = re.compile(rf"^{re.escape(head)}(\b|$)")
    return [i for i in issues if pattern.match(str(i.get("title") or "").strip())]


def _assigned(found: list[dict], kind: str, head: str, agent_id: str | None,
              role: str) -> list[Miss]:
    """No issue `head`: one miss; issues but none assigned to `role`: one miss."""
    if not found:
        return []
    if agent_id is None or any(str(i.get("assigneeAgentId")) == agent_id for i in found):
        return []
    return [Miss(f"{kind}_assignee", f"{PREFIX}il task «{head}» ({_ident(found[0])}) non è "
                                     f"assegnato al {role}.")]


def morning_misses(local: datetime, issues: list[dict], agents: dict[str, str | None],
                   buongiorno_sent: bool) -> list[Miss]:
    """The AGENDA / BUONGIORNO checks of a market day at local time `local`."""
    day = local.date().isoformat()
    out: list[Miss] = []
    if local.time() >= AGENDA_BY:
        head = f"AGENDA {day}"
        found = _titled(issues, head)
        if not found:
            out.append(Miss("agenda", f"{PREFIX}alle 08:50 non c'è il task «{head}» al "
                                      f"{PORTFOLIO_MANAGER} (scansione pre-mercato)."))
        out += _assigned(found, "agenda", head, agents.get(PORTFOLIO_MANAGER),
                         PORTFOLIO_MANAGER)
    if local.time() >= BUONGIORNO_BY:
        head = f"BUONGIORNO {day}"
        found = _titled(issues, head)
        if not found:
            out.append(Miss("buongiorno", f"{PREFIX}alle 09:20 non c'è il task «{head}» al "
                                          f"{NOTIFIER}."))
        else:
            out += _assigned(found, "buongiorno", head, agents.get(NOTIFIER), NOTIFIER)
            if not buongiorno_sent:
                out.append(Miss("buongiorno_telegram",
                                f"{PREFIX}alle 09:20 il Telegram «Buongiorno – piano di "
                                f"oggi» del {day} non è stato inviato."))
    return out


def reply_candidates(issues: list[dict], now: datetime) -> list[dict]:
    """Open PROTEGGI / RISCHIO OBIETTIVO issues older than REPLY_WITHIN."""
    out = []
    for i in issues:
        title = str(i.get("title") or "").strip()
        age = _age(i, now)
        if (title.startswith(REPLY_PREFIXES) and _open(i) and age is not None
                and age >= REPLY_WITHIN and i.get("id")):
            out.append(i)
    return out


def reply_miss(issue: dict, now: datetime) -> Miss:
    minutes = int(_age(issue, now).total_seconds() // 60)  # type: ignore[union-attr]
    return Miss(f"reply:{issue['id']}", f"{PREFIX}«{_title(issue)}» ({_ident(issue)}) è "
                                        f"aperto da {minutes} min senza risposta.")


def execute_symbol(title: str) -> str | None:
    parts = [p.strip() for p in title.split("·")]
    return parts[2] or None if len(parts) >= 3 else None


def execute_misses(issues: list[dict], now: datetime,
                   market_open: Callable[[str | None], bool]) -> list[Miss]:
    out = []
    for i in issues:
        title = str(i.get("title") or "").strip()
        age = _age(i, now)
        if not (title.startswith(EXECUTE_PREFIX) and _open(i) and i.get("id")
                and age is not None and age >= EXECUTE_WITHIN):
            continue
        if not market_open(execute_symbol(title)):
            continue
        minutes = int(age.total_seconds() // 60)
        out.append(Miss(f"execute:{i['id']}", f"{PREFIX}«{_title(i)}» ({_ident(i)}) è "
                                              f"ancora aperto da {minutes} min a mercato "
                                              f"aperto."))
    return out


def is_market_day(markets: set[str], day: date) -> bool:
    if day.weekday() >= 5:
        return False
    for market in markets:
        try:
            if session_on(market, day) is not None:
                return True
        except ValueError:
            continue
    return False


def _market_is_open(market: str, now: datetime) -> bool:
    try:
        return market_status(market, now).is_open
    except (SMTError, ValueError):  # an unknown market, a date out of the calendar
        return False


# -- the job ------------------------------------------------------------------------------

async def portfolio_markets(session: AsyncSession) -> set[str]:
    rows = await session.scalars(
        select(RiskProfile.allowed_markets).select_from(Portfolio)
        .outerjoin(RiskProfile, RiskProfile.id == Portfolio.risk_profile_id)
        .where(Portfolio.status != PortfolioStatus.archived))
    out: set[str] = set()
    for allowed in rows:
        allowed = [str(m) for m in (allowed or [])]
        out |= {m for m in allowed if m in CALENDARS} if allowed else set(DEFAULT_MARKETS)
    return out


def _fresh_state(day: str) -> dict[str, Any]:
    return {"day": day, "warned": [], "last_pass_at": None}


async def _load_state(session: AsyncSession, *, for_update: bool = False) -> dict[str, Any]:
    query = select(AppSetting.value).where(AppSetting.key == STATE_KEY)
    raw = await session.scalar(query.with_for_update() if for_update else query)
    if not isinstance(raw, dict):
        return _fresh_state("")
    return {"day": str(raw.get("day") or ""),
            "warned": [str(k) for k in raw.get("warned") or []],
            "last_pass_at": raw.get("last_pass_at")}


async def _save_state(session: AsyncSession, state: dict[str, Any]) -> None:
    stmt = insert(AppSetting).values(key=STATE_KEY, value=state)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                                     set_={"value": state}))


class HandoffWatchdog:
    def __init__(self, sessionmaker: async_sessionmaker, *,
                 client_factory: ClientFactory | None = None,
                 clock: Callable[[], datetime] | None = None, tz: str | None = None,
                 interval_s: float = INTERVAL_S) -> None:
        self._sm = sessionmaker
        self._client_factory = client_factory or (lambda cfg: PaperclipClient(cfg))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._zone = ZoneInfo(tz or get_settings().tz)
        self._interval_s = interval_s
        self._agents: dict[str, str | None] | None = None
        self._agents_at: datetime | None = None
        self._commented: set[str] = set()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("hand-off watchdog failed; retrying in %ss", self._interval_s,
                            exc_info=True)
            await asyncio.sleep(self._interval_s)

    def _due(self, state: dict[str, Any], now: datetime) -> bool:
        last = _parse_ts(state.get("last_pass_at"))
        return last is None or now - last >= timedelta(seconds=self._interval_s) \
            or last > now

    async def tick(self) -> None:
        now = self._clock()
        local = now.astimezone(self._zone)
        today = local.date()
        midnight = datetime.combine(today, time(0), self._zone)
        async with self._sm() as session:
            if not await watchdog_on(session):
                return
            cfg = await load_config(session)
            if not (cfg.enabled and cfg.complete and not cfg.unreadable):
                return
            state = await _load_state(session)
            if not self._due(state, now):
                return
            markets = await portfolio_markets(session)
            sent = await session.scalar(
                select(TelegramOutbox.id).where(
                    TelegramOutbox.created_at >= midnight,
                    TelegramOutbox.text.ilike(BUONGIORNO_TEXT),
                    TelegramOutbox.status.notin_([OutboxStatus.failed, OutboxStatus.skipped]))
                .limit(1)) is not None
        warned = set(state["warned"]) if state["day"] == today.isoformat() else set()
        try:
            issues, uncommented = await self._fetch(cfg, now, warned)
        except PaperclipError as e:
            log.warning("hand-off watchdog: paperclip read failed: %s", e.message)
            await self._record(now, today, [])
            return
        misses: list[Miss] = []
        if is_market_day(markets, today):
            misses += morning_misses(local, issues, self._agents or {}, sent)
        misses += [reply_miss(i, now) for i in uncommented]
        symbols = {s for i in issues
                   if str(i.get("title") or "").startswith(EXECUTE_PREFIX)
                   and (s := execute_symbol(str(i["title"])))}
        symbol_markets: dict[str, str] = {}
        if symbols:
            async with self._sm() as session:
                symbol_markets = dict((await session.execute(
                    select(Instrument.symbol, Instrument.market)
                    .where(Instrument.symbol.in_(symbols)))).all())

        def market_open(symbol: str | None) -> bool:
            market = symbol_markets.get(symbol or "")
            if market is not None:
                return _market_is_open(market, now)
            return any(_market_is_open(m, now) for m in markets)

        misses += execute_misses(issues, now, market_open)
        await self._record(now, today, [m for m in misses if m.key not in warned])

    async def _fetch(self, cfg: PaperclipConfig, now: datetime,
                     warned: set[str]) -> tuple[list[dict], list[dict]]:
        """(issues, the reply candidates with no comment), with no transaction open."""
        async with self._client_factory(cfg) as client:
            issues = await client.issues(ISSUES_LIMIT)
            if (self._agents is None or self._agents_at is None
                    or now - self._agents_at >= AGENTS_TTL):
                try:
                    agents = await client.agents()
                    self._agents = {role: find_agent_id(agents, role)
                                    for role in (PORTFOLIO_MANAGER, NOTIFIER)}
                    self._agents_at = now
                except PaperclipError:
                    log.warning("hand-off watchdog: agents not read", exc_info=True)
            uncommented: list[dict] = []
            reads = 0
            for issue in reply_candidates(issues, now):
                iid = str(issue["id"])
                if iid in self._commented or f"reply:{iid}" in warned:
                    continue
                if reads >= MAX_COMMENT_READS:
                    break
                reads += 1
                if await client.issue_comments(iid):
                    self._commented.add(iid)
                else:
                    uncommented.append(issue)
        return issues, uncommented

    async def _record(self, now: datetime, today: date, misses: list[Miss]) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(insert(AppSetting).values(
                key=STATE_KEY, value=_fresh_state(today.isoformat()))
                .on_conflict_do_nothing(index_elements=["key"]))
            state = await _load_state(session, for_update=True)
            if state["day"] != today.isoformat():
                state = _fresh_state(today.isoformat())
            for miss in misses:
                if miss.key in state["warned"]:
                    continue
                await enqueue(session, source="system", category="system", level="warning",
                              text=miss.text)
                state["warned"].append(miss.key)
            state["warned"] = state["warned"][-MAX_WARNED:]
            state["last_pass_at"] = now.isoformat()
            await _save_state(session, state)
