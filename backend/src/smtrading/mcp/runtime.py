"""Shared MCP runtime and the tool runner every MCP tool goes through.

Tool bodies are `async def fn(ctx: ToolCtx) -> Any`. `run_tool` gives each call its own
session and a fresh `MemoQuoteSource` over the runtime quote source, then:

- success: serialise with `dto.to_jsonable`, `commit()`, `dispatch_post_commit`;
- `SMTError`: rollback + `discard_post_commit`; with `commit_on_error=True` an error
  flagged `persisted` (a rejected order written to the session) is committed and
  dispatched instead, so the rejection is stored. The error is returned as is;
- `pydantic.ValidationError` / `ValueError` / `decimal.InvalidOperation`: INVALID_ORDER
  (a ValueError is logged, its text is not returned);
- anything else: logged with its stack, returned as INTERNAL "internal error".

The result is a `CallToolResult` with one JSON text block: the payload on success, or
`{"code","message","details"}` with `isError: true`. (mcp 2.x prefixes the text of a
raised `ToolError` with "Error executing tool <name>: ", so errors are returned, not
raised.) Every call writes one audit row, in its own session, with latency and outcome
("ok" or the error code).
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import InvalidOperation
from typing import Any
from uuid import UUID

import pydantic
from mcp.types import CallToolResult, TextContent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.agents.service import write_audit
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import MemoQuoteSource, QuoteSource
from smtrading.events import discard_post_commit, dispatch_post_commit
from smtrading.marketdata.router import MarketDataRouter
from smtrading.mcp.auth import current_agent
from smtrading.mcp.dto import (
    AGENT_REJECT_CODE,
    AGENT_REJECT_MESSAGE,
    agent_safe,
    mentions_dry_run,
    to_jsonable,
)
from smtrading.risk.settings import get_mcp_hide_mode

log = logging.getLogger("smtrading.mcp")

OUTCOME_OK = "ok"
_SECRET_PARAMS = frozenset({"token", "authorization"})


@dataclass
class McpRuntime:
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Any
    qs: QuoteSource  # the MarketDataRouter in production
    router: MarketDataRouter | None  # for market-data tools; None in tests without one


@dataclass
class ToolCtx:
    session: AsyncSession
    agent_id: UUID
    qs: MemoQuoteSource  # per call
    redis: Any
    router: MarketDataRouter | None


_runtime: McpRuntime | None = None


def set_runtime(runtime: McpRuntime | None) -> None:
    global _runtime
    _runtime = runtime


def get_runtime_or_none() -> McpRuntime | None:
    return _runtime


def get_runtime() -> McpRuntime:
    if _runtime is None:
        raise RuntimeError("MCP runtime not initialised (app lifespan not running)")
    return _runtime


def _text(payload: Any, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, allow_nan=False))],
        is_error=is_error,
    )


def _error(code: str, message: str, details: dict | None = None) -> CallToolResult:
    return _text({"code": code, "message": message, "details": details or {}}, is_error=True)


async def _execute(
    runtime: McpRuntime,
    agent_id: UUID,
    fn: Callable[[ToolCtx], Awaitable[Any]],
    commit_on_error: bool,
) -> Any:
    async with runtime.sessionmaker() as session:
        ctx = ToolCtx(session, agent_id, MemoQuoteSource(runtime.qs), runtime.redis,
                      runtime.router)
        hide: list[bool] = []  # mcp_hide_mode, read at most once per call

        async def hidden() -> bool:
            if not hide:
                hide.append(await get_mcp_hide_mode(session))
            return hide[0]

        try:
            # serialised inside the transaction: a result that cannot be rendered
            # rolls the call back instead of reporting an error for committed work
            result = to_jsonable(await fn(ctx))
            # hide mode first: with it off the json.dumps pre-check never runs
            if await hidden() and mentions_dry_run(result):
                result = agent_safe(result)  # R8.11
        except SMTError as e:
            err = e
            if e.code == ErrorCode.LIVE_DRY_RUN and await hidden():
                # R8.11: exactly a real broker refusal (code, message, details)
                err = SMTError(AGENT_REJECT_CODE, AGENT_REJECT_MESSAGE,
                               agent_safe(e.details or {}), persisted=e.persisted)
            if commit_on_error and e.persisted:
                await session.commit()
                await dispatch_post_commit(session, runtime.redis)
            else:
                await session.rollback()
                discard_post_commit(session)
            if err is e:
                raise
            raise err from None
        except Exception:
            await session.rollback()
            discard_post_commit(session)
            raise
        await session.commit()
        await dispatch_post_commit(session, runtime.redis)
        return result


async def _audit(
    runtime: McpRuntime, agent_id: UUID | None, method: str, params: dict, outcome: str,
    latency_ms: int,
) -> None:
    safe = {k: v for k, v in params.items() if k.lower() not in _SECRET_PARAMS}
    try:
        async with runtime.sessionmaker() as session:
            await write_audit(
                session, agent_id=agent_id, method=method, params=safe, outcome=outcome,
                latency_ms=latency_ms,
            )
            await session.commit()
    except Exception:
        log.exception("audit commit failed (method=%s)", method)


def validation_error(e: pydantic.ValidationError) -> CallToolResult:
    """INVALID_ORDER for arguments that fail the tool signature's schema: `details.field`
    is the first failing argument; the rejected values are not echoed."""
    errors = e.errors(include_input=False, include_url=False)
    first = errors[0] if errors else {"loc": (), "msg": "invalid arguments"}
    field = str(first["loc"][0]) if first["loc"] else None
    message = f"{field}: {first['msg']}" if field else str(first["msg"])
    return _error(str(ErrorCode.INVALID_ORDER), message, {"field": field} if field else {})


async def reject_arguments(
    method: str, params: dict[str, Any], e: pydantic.ValidationError
) -> CallToolResult:
    """The tool-call path for arguments refused before the tool body runs (by the SDK's
    validation of the tool signature): the standard JSON error plus an audit row."""
    response = validation_error(e)
    runtime = get_runtime_or_none()
    if runtime is not None:
        await _audit(runtime, current_agent.get(), method, params, str(ErrorCode.INVALID_ORDER), 0)
    return response


async def run_tool(
    method: str,
    params: dict[str, Any],
    fn: Callable[[ToolCtx], Awaitable[Any]],
    *,
    commit_on_error: bool = False,
) -> CallToolResult:
    runtime = get_runtime()
    agent_id = current_agent.get()
    started = time.perf_counter()
    outcome = OUTCOME_OK
    try:
        if agent_id is None:
            raise SMTError(ErrorCode.FORBIDDEN, "invalid token")
        response = _text(await _execute(runtime, agent_id, fn, commit_on_error))
    except SMTError as e:
        outcome = str(e.code)
        response = _error(outcome, e.message, e.details)
    except pydantic.ValidationError as e:
        outcome = str(ErrorCode.INVALID_ORDER)
        response = validation_error(e)
    except (ValueError, InvalidOperation) as e:
        log.warning("MCP tool %s: invalid argument: %r", method, e)
        outcome = str(ErrorCode.INVALID_ORDER)
        response = _error(outcome, "invalid argument")
    except Exception:
        log.exception("MCP tool %s failed", method)
        outcome = "INTERNAL"
        response = _error("INTERNAL", "internal error")
    latency_ms = int((time.perf_counter() - started) * 1000)
    await _audit(runtime, agent_id, method, params, outcome, latency_ms)
    return response
