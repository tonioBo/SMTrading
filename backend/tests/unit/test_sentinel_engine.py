"""«Sentinella di sistema», the dedup engine (pure): one message when a problem starts, a
reminder at most every REMINDER_EVERY while it lasts, one «risolto» when it ends; a
problem that must last (`after_s`) waits; one-shot events are told once; the open
problems of a check that could not run are kept as they are."""

from datetime import UTC, datetime, timedelta

from smtrading.sentinel import engine
from smtrading.sentinel.engine import Finding, compose, fresh_state, step, weekly_summary

T0 = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def _f(key="agent:a1", kind="agent", title="Agente Risk Manager in errore", **kw):
    return Finding(key=key, kind=kind, title=title, **kw)


def _kinds(notes):
    return [(n.what, n.title) for n in notes]


def test_a_new_problem_is_told_once_then_reminded_every_two_hours_then_resolved():
    state = fresh_state()
    notes = step(state, [_f()], T0, {"agent"})
    assert _kinds(notes) == [("start", "Agente Risk Manager in errore")]
    assert step(state, [_f()], T0 + timedelta(minutes=5), {"agent"}) == []
    assert step(state, [_f()], T0 + timedelta(minutes=119), {"agent"}) == []
    notes = step(state, [_f()], T0 + timedelta(hours=2), {"agent"})
    assert _kinds(notes) == [("reminder", "Agente Risk Manager in errore")]
    assert step(state, [_f()], T0 + timedelta(hours=3), {"agent"}) == []
    notes = step(state, [], T0 + timedelta(hours=3, minutes=5), {"agent"})
    assert _kinds(notes) == [("resolved", "Agente Risk Manager in errore")]
    assert notes[0].since == T0
    assert state["open"] == {}
    # it comes back: a new start
    assert _kinds(step(state, [_f()], T0 + timedelta(hours=4), {"agent"})) == [
        ("start", "Agente Risk Manager in errore")]


def test_a_problem_that_must_last_waits_and_a_short_one_says_nothing():
    state = fresh_state()
    f = _f(after_s=600)
    assert step(state, [f], T0, {"agent"}) == []
    assert step(state, [f], T0 + timedelta(minutes=9), {"agent"}) == []
    # gone before 10 minutes: never told, never «risolto»
    assert step(state, [], T0 + timedelta(minutes=9, seconds=30), {"agent"}) == []
    assert state["pending"] == {}
    assert step(state, [f], T0 + timedelta(minutes=20), {"agent"}) == []
    notes = step(state, [f], T0 + timedelta(minutes=30), {"agent"})
    assert _kinds(notes) == [("start", "Agente Risk Manager in errore")]
    assert notes[0].since == T0 + timedelta(minutes=20)  # since it was first seen


def test_an_unchecked_kind_keeps_its_open_and_pending_problems():
    state = fresh_state()
    step(state, [_f(), _f(key="agent:a2", after_s=600)], T0, {"agent"})
    # the Paperclip read failed: «agent» not checked this pass
    assert step(state, [], T0 + timedelta(minutes=5), set()) == []
    assert set(state["open"]) == {"agent:a1"}
    assert set(state["pending"]) == {"agent:a2"}


def test_an_event_is_told_once_never_reminded_nor_resolved():
    state = fresh_state()
    ev = _f(key="run:r1", kind="run", title="Ciclo non riuscito", event=True)
    assert _kinds(step(state, [ev], T0, {"run"})) == [("event", "Ciclo non riuscito")]
    assert step(state, [ev], T0 + timedelta(hours=3), {"run"}) == []
    assert step(state, [], T0 + timedelta(hours=4), {"run"}) == []
    assert state["open"] == {}
    # forgotten after the retention
    assert step(state, [], T0 + engine.EVENT_RETENTION + timedelta(hours=1), {"run"}) == []
    assert "run:r1" not in state["events"]


def test_a_changed_signature_while_open_sends_an_update():
    state = fresh_state()
    step(state, [_f(key="health", kind="health", title="Salute", signature="attenzione")],
         T0, {"health"})
    notes = step(state, [_f(key="health", kind="health", title="Salute: problema",
                            signature="problema")], T0 + timedelta(minutes=5), {"health"})
    assert _kinds(notes) == [("update", "Salute: problema")]
    # the reminder clock restarts from the update
    assert step(state, [_f(key="health", kind="health", title="Salute: problema",
                           signature="problema")],
                T0 + timedelta(hours=1, minutes=59), {"health"}) == []


def test_history_counts_starts_and_events_for_the_week():
    state = fresh_state()
    step(state, [_f(), _f(key="run:r1", kind="run", title="x", event=True)], T0, {"agent", "run"})
    assert sorted(h["kind"] for h in state["history"]) == ["agent", "run"]
    step(state, [], T0 + engine.HISTORY_RETENTION + timedelta(hours=1), {"agent", "run"})
    assert state["history"] == []


def test_compose_one_message_per_pass_with_the_worst_level():
    state = fresh_state()
    step(state, [_f(key="old", title="Vecchio")], T0, {"agent"})
    notes = step(state, [_f(key="new", title="Nuovo", detail="errore: timeout",
                            level="critical")], T0 + timedelta(minutes=5), {"agent"})
    level, text = compose(notes, T0 + timedelta(minutes=5), "Europe/Rome")
    assert level == "critical"
    assert text.startswith("🛡 Sentinella di sistema")
    assert "🔴 Nuovo problema: Nuovo" in text
    assert "errore: timeout" in text
    assert "🟢 Risolto: Vecchio (durato 5 min)" in text


def test_compose_only_resolved_is_info():
    level, _ = compose([engine.Note("resolved", "x", "", "warning", "agent", T0)],
                       T0 + timedelta(hours=1), "Europe/Rome")
    assert level == "info"


def test_compose_keeps_the_text_within_telegram_limits():
    notes = [engine.Note("start", f"Problema {i}", "d" * 500, "warning", "agent", T0)
             for i in range(40)]
    _, text = compose(notes, T0, "Europe/Rome")
    assert len(text) <= engine.MAX_TEXT
    assert "altri" in text


def test_weekly_summary_counts_and_open_problems():
    state = fresh_state()
    step(state, [_f(), _f(key="run:r1", kind="run", title="x", event=True),
                 _f(key="run:r2", kind="run", title="y", event=True)], T0, {"agent", "run"})
    text = weekly_summary(state, T0 + timedelta(days=1), "Europe/Rome")
    assert "Riepilogo della settimana" in text
    assert "Cicli agenti non riusciti: 2" in text
    assert "Agenti Paperclip: 1" in text
    assert "Ancora aperti" in text and "Agente Risk Manager in errore" in text


def test_weekly_summary_with_nothing():
    text = weekly_summary(fresh_state(), T0, "Europe/Rome")
    assert "nessun avviso" in text


def test_weekly_due_on_sunday_from_20_once_a_week():
    state = fresh_state()
    sat = datetime(2026, 11, 28, 20, 30, tzinfo=UTC)  # Saturday
    sun_19 = datetime(2026, 11, 29, 18, 59, tzinfo=UTC)  # 19:59 in Rome
    sun_20 = datetime(2026, 11, 29, 19, 0, tzinfo=UTC)  # 20:00 in Rome
    assert not engine.weekly_due(state, sat, "Europe/Rome")
    assert not engine.weekly_due(state, sun_19, "Europe/Rome")
    assert engine.weekly_due(state, sun_20, "Europe/Rome")
    engine.mark_weekly(state, sun_20, "Europe/Rome")
    assert not engine.weekly_due(state, sun_20 + timedelta(hours=2), "Europe/Rome")


def test_state_survives_a_json_round_trip():
    import json

    state = fresh_state()
    step(state, [_f(), _f(key="p", after_s=600)], T0, {"agent"})
    loaded = engine.load(json.loads(json.dumps(state)))
    assert step(loaded, [_f(), _f(key="p", after_s=600)], T0 + timedelta(minutes=10),
                {"agent"})[0].title == "Agente Risk Manager in errore"


def test_load_of_garbage_is_a_fresh_state():
    assert engine.load(None) == fresh_state()
    assert engine.load({"open": "x", "pending": 3})["open"] == {}
