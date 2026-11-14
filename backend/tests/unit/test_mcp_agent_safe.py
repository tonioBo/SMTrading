"""R8.11 (amended): under mcp_hide_mode a dry-run rejection reaches the agent exactly like
a real broker refusal (same code, same message shape); humans keep LIVE_DRY_RUN."""

import json
import uuid

import pytest

from smtrading.brokers.base import BROKER_REJECTED_MESSAGE
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.mcp import dto, runtime


class _Session:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.calls.append("commit")

    async def rollback(self):
        self.calls.append("rollback")


def _runtime(session: _Session) -> runtime.McpRuntime:
    return runtime.McpRuntime(sessionmaker=lambda: session, redis=None, qs=None, router=None)


@pytest.fixture
def hide(monkeypatch):
    state = {"on": True, "reads": 0}

    async def fake_hide(session):
        state["reads"] += 1
        return state["on"]

    async def fake_dispatch(session, redis):
        session.calls.append("dispatch")

    monkeypatch.setattr(runtime, "get_mcp_hide_mode", fake_hide)
    monkeypatch.setattr(runtime, "dispatch_post_commit", fake_dispatch)
    monkeypatch.setattr(runtime, "discard_post_commit", lambda session: None)
    return state


def _dry_error() -> SMTError:
    return SMTError(ErrorCode.LIVE_DRY_RUN, "prova a secco: ordine non inviato",
                    {"order_id": "o1", "message": "prova a secco: ordine non inviato"},
                    persisted=True)


async def test_execute_masks_a_persisted_dry_run_error_and_still_commits(hide):
    session = _Session()

    async def fn(ctx):
        raise _dry_error()

    with pytest.raises(SMTError) as info:
        await runtime._execute(_runtime(session), uuid.uuid4(), fn, commit_on_error=True)
    err = info.value
    # the code and message of a real broker refusal (BrokerSync._placed)
    assert err.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert isinstance(err.code, ErrorCode)
    assert err.message == BROKER_REJECTED_MESSAGE
    text = json.dumps(err.to_dict()).lower()
    assert not any(w in text for w in ("prova", "dry", "secco")), text
    assert err.persisted
    assert session.calls == ["commit", "dispatch"]  # the rejected order is kept


async def test_execute_keeps_the_dry_run_code_with_hide_off(hide):
    hide["on"] = False
    session = _Session()

    async def fn(ctx):
        raise _dry_error()

    with pytest.raises(SMTError) as info:
        await runtime._execute(_runtime(session), uuid.uuid4(), fn, commit_on_error=True)
    assert info.value.code == ErrorCode.LIVE_DRY_RUN
    assert session.calls == ["commit", "dispatch"]


async def test_hide_mode_is_read_first_and_once(hide, monkeypatch):
    """Hide off: the json.dumps pre-check never runs; hide mode is read once per call."""
    hide["on"] = False

    def boom(result):
        raise AssertionError("mentions_dry_run / agent_view must not run with hide off")

    monkeypatch.setattr(dto, "mentions_dry_run", boom)
    monkeypatch.setattr(runtime, "agent_view", boom)

    async def fn(ctx):
        return {"reject_code": "LIVE_DRY_RUN"}

    out = await runtime._execute(_runtime(_Session()), uuid.uuid4(), fn, False)
    assert out == {"reject_code": "LIVE_DRY_RUN"}
    assert hide["reads"] == 1


def _real_and_dry_items() -> tuple[dict, dict]:
    """The agent DTOs of a real broker refusal and of a dry-run rejection of the same
    order, as BrokerSync writes them (order row + live.order.<phase> activity)."""
    from smtrading.brokers.live.protect import order_summary

    def item(phase: str, message: str) -> dict:
        data = {"order_id": "o1", "link_id": "l1", "phase": phase, "symbol": "ENI.MI",
                "side": "sell", "qty": "1", "notional": None, "type": "market",
                "limit_price": None, "stop_price": None, "currency": "EUR",
                "source": "agent", "agent_id": "a1", "filled_qty": "0", "price": None,
                "value": None, "fees": None, "message": message, "live_flatten": False}
        if phase == "dry_run":
            data["payload"] = {"symbol": "ENI.MI", "qty": "1"}
        return {"id": 1, "ts": "t", "type": f"live.order.{phase}",
                "level": "warning" if phase == "rejected" else "info",
                "summary": order_summary(data, phase), "data": data, "already_sent": True}

    real = item("rejected", BROKER_REJECTED_MESSAGE)
    dry = item("dry_run", "prova a secco: ordine non inviato")
    return real, dry


def test_masked_dry_run_activity_equals_a_real_refusal():
    real, dry = _real_and_dry_items()
    assert dto.agent_safe(dry) == real


def test_masked_dry_run_order_and_notification_equal_a_real_refusal():
    real_order = {"id": "o1", "status": "rejected", "reject_code": "PROVIDER_UNAVAILABLE",
                  "reject_message": BROKER_REJECTED_MESSAGE}
    dry_order = {**real_order, "reject_code": "LIVE_DRY_RUN",
                 "reject_message": "prova a secco: ordine non inviato"}
    assert dto.agent_safe(dry_order) == real_order
    real_note = {"kind": "order.rejected", "payload": {
        "order_id": "o1", "symbol": "ENI.MI", "code": "PROVIDER_UNAVAILABLE",
        "message": BROKER_REJECTED_MESSAGE, "broker": True}}
    dry_note = {"kind": "order.rejected", "payload": {
        **real_note["payload"], "code": "LIVE_DRY_RUN",
        "message": "prova a secco: ordine non inviato"}}
    assert dto.agent_safe(dry_note) == real_note


# --- no simulation fingerprints (final review I1) ------------------------------------


def test_agent_view_drops_fill_provenance_and_paper_broker_names():
    sim = {"id": "f1", "qty": "1", "source": "sim"}
    real = {"id": "f1", "qty": "1", "source": "broker"}
    assert dto.agent_view(sim) == dto.agent_view(real) == {"id": "f1", "qty": "1"}
    item = {"type": "order.filled", "summary": "Ordine eseguito",
            "data": {"order_id": "o1", "source": "sim"}}
    assert dto.agent_view({"items": [item]})["items"][0]["data"] == {"order_id": "o1"}
    # an order's source (who placed it) is not a fingerprint and stays
    assert dto.agent_view({"source": "agent"}) == {"source": "agent"}
    from smtrading.brokers.alpaca_paper import PAPER_URL
    from smtrading.brokers.live.t212_live import T212LiveAdapter
    from smtrading.brokers.t212_demo import DEMO_URL

    text = dto.agent_view({"reason": f"Server error for url '{DEMO_URL}/x'",
                           "msg": f"{PAPER_URL}/v2 (alpaca_paper)"})
    assert "demo" not in json.dumps(text) and "paper" not in json.dumps(text)
    assert text["reason"].endswith(f"'{T212LiveAdapter.BASE_URL}/x'")


async def test_execute_scrubs_error_text_only_under_hide_mode(hide):
    from smtrading.brokers.live.t212_live import T212LiveAdapter
    from smtrading.brokers.t212_demo import DEMO_URL

    def boom():
        return SMTError(ErrorCode.PROVIDER_UNAVAILABLE, f"down: {DEMO_URL}",
                        {"source": "sim", "symbol": "ENI.MI"})

    async def fn(ctx):
        raise boom()

    with pytest.raises(SMTError) as info:
        await runtime._execute(_runtime(_Session()), uuid.uuid4(), fn, False)
    assert info.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert info.value.message == f"down: {T212LiveAdapter.BASE_URL}"
    assert info.value.details == {"symbol": "ENI.MI"}

    hide["on"] = False
    with pytest.raises(SMTError) as info:
        await runtime._execute(_runtime(_Session()), uuid.uuid4(), fn, False)
    assert info.value.message == f"down: {DEMO_URL}"
