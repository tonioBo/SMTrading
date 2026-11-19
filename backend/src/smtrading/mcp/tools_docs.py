"""get_instructions_appendix: the appendix of the agent instructions (the rare cases the
instructions point to with «→ §<id>»), from «Istruzioni agenti» (slug `appendice`, its
current version: a save in the portal is visible at the next call).

Open to every agent (read only, no portfolio needed). Without `section`: the index
({sections: [{id, title}]}); with it: {id, title, text}. An unknown id is INVALID_ORDER
with `details.field = "section"` and the known ids; no appendix in the database is
NOT_FOUND."""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from smtrading.agentdocs import service
from smtrading.agentdocs.appendix import APPENDIX_SLUG, normalize_id, sections
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp.runtime import ToolCtx, run_tool


async def _get_appendix(ctx: ToolCtx, section: str | None) -> dict[str, Any]:
    try:
        doc = await service.get_doc(ctx.session, APPENDIX_SLUG)
    except SMTError as e:
        raise SMTError(ErrorCode.NOT_FOUND, "instructions appendix not found") from e
    found = sections(doc["body"])
    if section is None or not section.strip():
        return {"sections": [{"id": s.id, "title": s.title} for s in found]}
    wanted = normalize_id(section)
    for s in found:
        if s.id == wanted:
            return {"id": s.id, "title": s.title, "text": s.text}
    raise SMTError(ErrorCode.INVALID_ORDER, f"unknown section: {wanted[:20]}",
                   {"field": "section", "available": [s.id for s in found]})


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False)
    async def get_instructions_appendix(section: str | None = None) -> CallToolResult:
        """The appendix of your instructions: the rare cases your instructions point to
        with «→ §<id>» (e.g. «→ §C9»). Read the section before acting on the case; its
        rules count like the instructions'. section: the id (e.g. "C9" or "§PM3"); without
        it: the index {sections: [{id, title}]}. With it: {id, title, text}. An unknown id
        is INVALID_ORDER (details.field "section", details.available = the ids)."""
        return await run_tool(
            "get_instructions_appendix", {"section": section},
            lambda ctx: _get_appendix(ctx, section),
        )
