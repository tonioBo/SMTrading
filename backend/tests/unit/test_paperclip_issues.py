"""Paperclip issue writer generalized to any recipient agent."""

import httpx
import pytest

from smtrading.paperclip.client import PaperclipError
from smtrading.paperclip.config import PaperclipConfig
from smtrading.paperclip.issues import (
    WRITE_REFUSED,
    PaperclipIssues,
    find_agent_id,
    resolve_agent,
)

CFG = PaperclipConfig(base_url="http://pc.test", company_id="co1", board_key="k", enabled=True)

AGENTS = [{"id": "a1", "name": "CEO"}, {"id": "a2", "name": " Risk   Manager "},
          {"id": "a3", "name": "Trader Intraday"}]


def _client(handler):
    return PaperclipIssues(CFG, transport=httpx.MockTransport(handler))


async def test_resolve_agent_by_any_name():
    def handler(request):
        return httpx.Response(200, json=AGENTS)

    async with _client(handler) as client:
        assert await resolve_agent(client, "trader  INTRADAY") == "a3"
        assert await resolve_agent(client, "ceo") == "a1"


async def test_resolve_agent_missing_message():
    def handler(request):
        return httpx.Response(200, json=AGENTS)

    async with _client(handler) as client:
        with pytest.raises(PaperclipError) as exc:
            await resolve_agent(client, "X")
    assert str(exc.value) == "agente «X» non trovato"


async def test_open_identifiers_skips_done_and_cancelled():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=[
            {"id": "i1", "identifier": "SMT-1", "title": "a", "status": "todo"},
            {"id": "i2", "identifier": "SMT-2", "title": "b", "status": "done"},
            {"id": "i3", "title": "c", "status": "in_progress"},
            {"id": "i4", "identifier": "SMT-4", "title": "d", "status": "Cancelled"},
            {"id": "i5", "identifier": "SMT-5", "title": "e", "status": "canceled"},
        ])

    async with _client(handler) as client:
        assert await client.open_identifiers() == {"SMT-1", "i3"}
    assert calls == [("GET", "/api/companies/co1/issues")]


def test_find_agent_id_default_unchanged():
    assert find_agent_id(AGENTS) == "a2"


async def test_transport_still_refuses_other_writes():
    def handler(request):
        raise AssertionError("network reached")

    async with _client(handler) as client:
        with pytest.raises(RuntimeError, match=WRITE_REFUSED):
            await client._http.request("PATCH", "/api/companies/co1/issues/i1", json={})
