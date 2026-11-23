"""«Salute del sistema», the sentinel's part: open problems, last pass, on/off."""

from datetime import UTC, datetime, timedelta

from smtrading.sentinel import engine
from smtrading.sentinel.settings import clean_sentinel, sentinel_dto, sentinel_view

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def test_ok_when_it_ran_recently_with_nothing_open():
    state = engine.fresh_state()
    state["last_pass_at"] = (NOW - timedelta(minutes=4)).isoformat()
    v = sentinel_view(True, state, NOW)
    assert (v["status"], v["summary"], v["open"]) == ("ok", "Nessun problema aperto", [])


def test_late_pass_is_attention():
    state = engine.fresh_state()
    state["last_pass_at"] = (NOW - timedelta(minutes=20)).isoformat()
    v = sentinel_view(True, state, NOW)
    assert v["status"] == "attenzione"
    assert v["reasons"] == ["ultimo giro della sentinella 20 min fa"]


def test_open_problems_listed_oldest_first_with_the_check_errors():
    state = engine.fresh_state()
    state["last_pass_at"] = NOW.isoformat()
    state["last_error"] = "paperclip agenti: timeout"
    engine.step(state, [engine.Finding("mcp:x", "mcp", "B")], NOW - timedelta(minutes=5),
                {"mcp"})
    engine.step(state, [engine.Finding("mcp:x", "mcp", "B"),
                        engine.Finding("errlog:a", "errlog", "A")], NOW, {"mcp", "errlog"})
    v = sentinel_view(True, state, NOW)
    assert [o["title"] for o in v["open"]] == ["B", "A"]
    assert v["summary"] == "2 problemi aperti"
    assert "controlli non riusciti: paperclip agenti: timeout" in v["reasons"]


def test_off():
    v = sentinel_view(False, engine.fresh_state(), NOW)
    assert (v["status"], v["on"]) == ("spento", False)


def test_setting_round_trip():
    assert sentinel_dto(None) == {"on": True}
    assert clean_sentinel({"on": False}, {"on": True}) == {"on": False}
    import pytest

    from smtrading.common.errors import SMTError
    with pytest.raises(SMTError):
        clean_sentinel({"on": "no"}, {"on": True})
