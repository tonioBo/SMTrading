"""Agents' Paperclip sessions refreshed after an MCP tool catalog change.

A Paperclip agent keeps its session (and the tool list it was given) between runs, so a
new or changed SMTrading tool reaches it only with a new session. At every reader pass
(the first one at worker start) the catalog fingerprint is compared with the stored one
(`app_settings["paperclip_sessions"]`):

- no stored fingerprint (new install): it is stored, nothing is reset;
- same fingerprint: nothing to do;
- another fingerprint, Paperclip configured and enabled: every agent of the company that
  is not running gets `POST /api/agents/{id}/runtime-state/reset-session`; a running one
  (or one whose status is not known) waits for the next pass. Once every agent is done
  the fingerprint is stored and one Telegram info message is sent. The running check is
  best effort: the status is read from the agents list just before the resets, so an
  agent that starts a run in between can still be reset mid-run (Paperclip offers no
  conditional reset); its next run starts a new session anyway.

The agents already reset for the pending fingerprint are kept, so a later pass never
resets them again; a reset that fails is retried on the next passes, at most
`MAX_ATTEMPTS` times per agent (an error answer that did reset the session must not reset
it every 5 minutes). Failures are logged and shown in the Paperclip status; they never
reach the reader's error count. Every request is made outside any transaction (R9)."""

import copy
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.paperclip.client import PaperclipClient, PaperclipError
from smtrading.paperclip.config import PaperclipConfig
from smtrading.risk.models import AppSetting
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

SESSIONS_KEY = "paperclip_sessions"
MAX_ATTEMPTS = 3
RUNNING = "running"

DEFAULT_SESSIONS: dict[str, Any] = {
    "fingerprint": None,  # the catalog the agents' sessions were refreshed for
    "pending": None,  # a newer catalog whose refresh is not finished
    "done": [],  # Paperclip agent ids already reset for `pending`
    "attempts": {},  # agent id -> failed resets for `pending`
    "given_up": [],  # agent names whose reset failed MAX_ATTEMPTS times
    "waiting": [],  # agent names left for the next pass (running, or the reset failed)
    "last_error": None,
    "updated_at": None,
}

SessionClientFactory = Callable[[PaperclipConfig], PaperclipClient]


def done_text(count: int, given_up: list[str]) -> str:
    agents = "1 agente" if count == 1 else f"{count} agenti"
    text = f"Strumenti degli agenti aggiornati: sessioni ripartite per {agents}"
    if given_up:
        text += f" (non riuscito per: {', '.join(given_up)})"
    return text


async def load_sessions(session: AsyncSession, *, for_update: bool = False
                        ) -> dict[str, Any] | None:
    """The stored state, None when there is none yet (a new install)."""
    q = select(AppSetting.value).where(AppSetting.key == SESSIONS_KEY)
    raw = await session.scalar(q.with_for_update() if for_update else q)
    if not isinstance(raw, dict):
        return None
    state = copy.deepcopy(DEFAULT_SESSIONS)
    state.update({k: v for k, v in raw.items() if k in DEFAULT_SESSIONS})
    return state


async def save_sessions(session: AsyncSession, state: dict[str, Any]) -> None:
    value = {k: state.get(k, v) for k, v in DEFAULT_SESSIONS.items()}
    stmt = insert(AppSetting).values(key=SESSIONS_KEY, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))
    await session.flush()


def sessions_dto(state: dict[str, Any] | None) -> dict[str, Any]:
    """The «Paperclip» status block (no key, no URL)."""
    state = state or DEFAULT_SESSIONS
    return {
        "pending": state["pending"] is not None,
        "refreshed": len(state["done"] or []),
        "waiting": list(state["waiting"] or []),
        "given_up": list(state["given_up"] or []),
        "last_error": state["last_error"],
        "updated_at": state["updated_at"],
    }


def _name(agent: dict) -> str:
    return str(agent.get("name") or agent.get("id"))


class SessionRefresher:
    def __init__(self, sessionmaker: async_sessionmaker, *,
                 client_factory: SessionClientFactory | None = None,
                 clock: Callable[[], datetime]) -> None:
        self._sm = sessionmaker
        self._client_factory = client_factory or (
            lambda cfg: PaperclipClient(cfg, allow_session_reset=True))
        self._clock = clock

    async def refresh(self, cfg: PaperclipConfig, fingerprint: str) -> None:
        async with self._sm() as session, session.begin():
            state = await load_sessions(session, for_update=True)
            if state is None or state["fingerprint"] is None:
                state = copy.deepcopy(DEFAULT_SESSIONS)
                state.update(fingerprint=fingerprint, updated_at=self._clock().isoformat())
                await save_sessions(session, state)
                log.info("mcp tool catalog fingerprint stored (first run)")
                return
            if state["fingerprint"] == fingerprint:
                if state["pending"] is not None:  # the catalog went back: nothing to do
                    state.update({k: copy.deepcopy(DEFAULT_SESSIONS[k]) for k in
                                  ("pending", "done", "attempts", "given_up", "waiting",
                                   "last_error")})
                    await save_sessions(session, state)
                return
        if not cfg.enabled or not cfg.complete or cfg.unreadable:
            return  # the change stays pending until Paperclip is configured
        if state["pending"] != fingerprint:  # a new change: start over
            state.update(pending=fingerprint, done=[], attempts={}, given_up=[])
        await self._reset(cfg, state)

    async def _reset(self, cfg: PaperclipConfig, state: dict[str, Any]) -> None:
        done: list[str] = list(state["done"])
        attempts: dict[str, int] = dict(state["attempts"])
        given_up: list[str] = list(state["given_up"])
        waiting: list[str] = []
        errors: list[str] = []
        try:
            async with self._client_factory(cfg) as client:
                agents = await client.agents()
                for agent in agents:
                    aid = str(agent.get("id") or "")
                    if not aid or aid in done or attempts.get(aid, 0) >= MAX_ATTEMPTS:
                        continue
                    if not agent.get("status") or agent.get("status") == RUNNING:
                        waiting.append(_name(agent))
                        continue
                    try:
                        await client.reset_session(aid)
                    except PaperclipError as e:
                        attempts[aid] = attempts.get(aid, 0) + 1
                        errors.append(f"{_name(agent)}: {e.message}")
                        if attempts[aid] >= MAX_ATTEMPTS:
                            given_up.append(_name(agent))
                        else:
                            waiting.append(_name(agent))
                        continue
                    done.append(aid)
        except PaperclipError as e:
            # the agents could not be listed: nothing was reset in this pass
            log.warning("paperclip session refresh failed: %s", e.message)
            await self._save(state, done=done, attempts=attempts, given_up=given_up,
                             waiting=list(state["waiting"]),
                             last_error=f"sessioni agenti non aggiornate: {e.message}")
            return
        except Exception:
            # keep the resets already made: they must not be made again on the next pass
            await self._save(state, done=done, attempts=attempts, given_up=given_up,
                             waiting=list(state["waiting"]),
                             last_error="sessioni agenti non aggiornate: errore interno")
            raise
        last_error = ("sessione non azzerata per " + "; ".join(errors)) if errors else None
        if errors:
            log.warning("paperclip session reset failed for %s agent(s)", len(errors))
        finished = not waiting
        await self._save(state, done=done, attempts=attempts, given_up=given_up,
                         waiting=waiting, last_error=last_error, finished=finished)

    async def _save(self, state: dict[str, Any], *, done: list[str],
                    attempts: dict[str, int], given_up: list[str], waiting: list[str],
                    last_error: str | None, finished: bool = False) -> None:
        pending = state["pending"]
        async with self._sm() as session, session.begin():
            current = await load_sessions(session, for_update=True) or copy.deepcopy(
                DEFAULT_SESSIONS)
            current.update(done=done, attempts=attempts, given_up=given_up, waiting=waiting,
                           last_error=last_error, pending=pending,
                           updated_at=self._clock().isoformat())
            if finished:
                current.update(fingerprint=pending, pending=None, done=[], attempts={},
                               waiting=[])
                if done or given_up:
                    await enqueue(session, source="system", category="system",
                                  level="info", text=done_text(len(done), given_up))
                log.info("mcp tool catalog changed: %s agent session(s) refreshed",
                         len(done))
            await save_sessions(session, current)
