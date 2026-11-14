"""send_telegram: a message from an agent (the Notifier) to the SMTrading Telegram group.

Needs the agent's `notify` permission (`agents.can_notify`, read at call time). The text
goes verbatim (no markup parsing) through the outbox, which the worker sends; control
characters except newline and tab are removed. At most 20 messages per agent in any
60 s (RATE_LIMITED with `details.retry_after_s`). The audit row keeps only the level and
the text length; the text itself stays in the outbox.
"""

import math
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import func, select, text

from smtrading.agents.models import Agent
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.runtime import ToolCtx, run_tool
from smtrading.telegram.config import is_ready, load_config
from smtrading.telegram.models import TelegramOutbox
from smtrading.telegram.outbox import AGENT_WINDOW_S, agent_sent_last_minute, enqueue
from smtrading.telegram.render import MAX_LEN, PREFIX, clean_text, utf16_len

LEVELS = ("info", "warning", "critical")
MAX_PER_WINDOW = 20
NOT_READY_MESSAGE = "Notifiche Telegram non configurate o disattivate"


def audit_params(arguments: dict[str, Any]) -> dict[str, Any]:
    """What the audit keeps of a send_telegram call: the level and the text length."""
    text_ = arguments.get("text")
    return {"level": arguments.get("level", "info"),
            "length": len(text_) if isinstance(text_, str) else None}


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


async def _retry_after_s(ctx: ToolCtx, source: str) -> int:
    """Seconds until the oldest message in the window leaves it (at least 1)."""
    age = await ctx.session.scalar(
        select(func.extract("epoch", func.now() - func.min(TelegramOutbox.created_at))).where(
            TelegramOutbox.source == source,
            TelegramOutbox.created_at > func.now() - text(f"interval '{AGENT_WINDOW_S} seconds'"),
        )
    )
    left = AGENT_WINDOW_S - float(age or 0)
    return max(1, min(AGENT_WINDOW_S, math.ceil(left)))


async def _send_telegram(ctx: ToolCtx, text_: Any, level: Any) -> dict:
    can_notify = await ctx.session.scalar(select(Agent.can_notify).where(Agent.id == ctx.agent_id))
    if not can_notify:
        raise SMTError(ErrorCode.FORBIDDEN, "permesso notify mancante", {"needed": "notify"})
    if level not in LEVELS:
        raise _invalid("level", "level must be one of " + ", ".join(LEVELS))
    body = clean_text(text_) if isinstance(text_, str) else ""
    if not body:
        raise _invalid("text", "text is empty")
    message = PREFIX[level] + body
    if utf16_len(message) > MAX_LEN:
        raise _invalid("text", f"text longer than {MAX_LEN} characters (level prefix included)")
    if not is_ready(await load_config(ctx.session)):
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, NOT_READY_MESSAGE)
    source = f"agent:{ctx.agent_id}"
    if await agent_sent_last_minute(ctx.session, ctx.agent_id) >= MAX_PER_WINDOW:
        raise SMTError(
            ErrorCode.RATE_LIMITED,
            f"at most {MAX_PER_WINDOW} Telegram messages per {AGENT_WINDOW_S} s",
            {"retry_after_s": await _retry_after_s(ctx, source)},
        )
    row = await enqueue(ctx.session, source=source, category="agent", level=level, text=message)
    return {"outbox_id": row.id, "status": row.status}


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def send_telegram(text: str, level: str = "info") -> CallToolResult:
        """Send a message to the SMTrading Telegram group (needs the notify permission):
        {outbox_id, status} (status "pending", or "paused" while notifications are paused;
        the worker delivers it). The text is sent verbatim, no formatting (no Markdown or
        HTML); control characters except newline and tab are removed. level: info (no
        prefix), warning (🟠) or critical (🔴). At most 4096 UTF-16 units (an emoji counts
        2) prefix included, at most 20 messages per 60 s (RATE_LIMITED,
        details.retry_after_s). Events SMTrading sends itself (already_sent in
        get_activity_feed) need no repeat."""
        params = audit_params({"text": text, "level": level})
        return await run_tool(
            "send_telegram", params, lambda ctx: _send_telegram(ctx, text, level)
        )
