import uuid
from datetime import UTC, datetime

from smtrading.activity.models import Activity
from smtrading.telegram.render import (
    MAX_LEN,
    PREFIX,
    Names,
    clean_text,
    render_activity,
    split_message,
    utf16_len,
)

TZ = "Europe/Rome"


def _act(**kw):
    base = {"type": "order.filled", "level": "executed", "summary": "Ordine eseguito: buy 1 AAPL",
            "ts": datetime(2026, 11, 14, 9, 5, tzinfo=UTC), "agent_id": None,
            "portfolio_id": None}
    base.update(kw)
    return Activity(**base)


def test_prefixes():
    assert PREFIX == {"critical": "🔴 ", "warning": "🟠 ", "executed": "🟢 ", "info": ""}


def test_render_filled_has_green_prefix_and_names():
    aid, pid = uuid.uuid4(), uuid.uuid4()
    text = render_activity(_act(agent_id=aid, portfolio_id=pid),
                           Names(agents={aid: "Trader"}, portfolios={pid: "Test 250"}), TZ)
    assert text == ("🟢 Ordine eseguito — Test 250\n"
                    "Ordine eseguito: buy 1 AAPL\n"
                    "Agente: Trader · 10:05")


def test_render_omits_missing_data():
    text = render_activity(_act(type="kill_switch", level="critical", summary="Kill switch attivato"),
                           Names(), TZ)
    assert text == "🔴 Kill switch\nKill switch attivato\n10:05"
    info = render_activity(_act(type="price_alert", level="info", summary="x"), Names(), TZ)
    assert info.startswith("Alert di prezzo\n")


def test_utf16_len_counts_surrogates():
    assert utf16_len("🔴") == 2
    assert utf16_len("abc") == 3
    assert utf16_len("è") == 1


def test_clean_text_strips_controls_keeps_newlines():
    assert clean_text("  a\x00b\x07\nc\td\x1b[0m\x7f\u0085 ") == "ab\nc\td[0m"
    assert clean_text("\x00\x01 \n") == ""
    assert clean_text("<b>*_[x](y)@all") == "<b>*_[x](y)@all"


def test_lone_surrogates_never_raise():
    bad = "a\ud800b\udfffc"
    assert clean_text(bad) == "abc"
    assert utf16_len(bad) == 5
    parts = split_message(bad * 1000)
    assert all(utf16_len(p) <= MAX_LEN for p, _ in parts)


def test_split_message_drops_empty_parts():
    assert split_message("") == []
    assert split_message("\n\n  \n") == []
    long_blank = "a" * 4000 + "\n" + " " * 4000 + "\n" + "b"
    assert [p.strip() for p, _ in split_message(long_blank)] == ["a" * 4000, "b"]
    assert split_message("", "  \n ") == []
    [(part, entities)] = split_message("", "x  1")
    assert part == "x  1" and entities == [{"type": "pre", "offset": 0, "length": 4}]
    assert split_message("ciao", "\n") == [("ciao", None)]


def test_split_message_short_is_one_part():
    assert split_message("ciao\nmondo") == [("ciao\nmondo", None)]


def test_split_message_lines_and_limit():
    a, b = "a" * 3000, "b" * 3000
    parts = split_message(f"{a}\n{b}")
    assert [p for p, _ in parts] == [a, b]
    assert all(len(p) <= MAX_LEN and e is None for p, e in parts)


def test_split_message_cuts_an_overlong_line_without_breaking_emoji():
    line = "🔴" * 3000  # 6000 UTF-16 units
    parts = split_message(line)
    assert len(parts) == 2
    assert "".join(p for p, _ in parts) == line
    assert all(utf16_len(p) <= MAX_LEN for p, _ in parts)


def test_split_pre_entity_utf16_offsets():
    text = "🔴 Riepilogo\nriga"
    table = "Portafoglio  Rend. %\nTest 250     +1,23"
    [(part, entities)] = split_message(text, table)
    assert part == f"{text}\n\n{table}"
    prefix = f"{text}\n\n"
    assert entities == [{"type": "pre", "offset": utf16_len(prefix), "length": utf16_len(table)}]
    assert utf16_len(prefix) == len(prefix) + 1  # the emoji counts twice
    # the entity covers exactly the table, measured in UTF-16
    units = part.encode("utf-16-le")
    e = entities[0]
    assert units[2 * e["offset"]: 2 * (e["offset"] + e["length"])].decode("utf-16-le") == table


def test_split_long_pre_block_gets_an_entity_in_every_part():
    text = "🟢 Riepilogo giornaliero\n" + "\n".join(f"🔴 evento {i}" for i in range(50))
    table = "\n".join(f"Portafoglio {i:03d}      +1,23      -0,50   12" for i in range(300))
    parts = split_message(text, table)
    assert len(parts) > 2
    covered = []
    for part, entities in parts:
        assert utf16_len(part) <= MAX_LEN
        if entities is None:
            continue
        [e] = entities
        units = part.encode("utf-16-le")
        block = units[2 * e["offset"]: 2 * (e["offset"] + e["length"])].decode("utf-16-le")
        assert e["offset"] + e["length"] == utf16_len(part)  # the block ends the part
        covered.append(block)
    assert "\n".join(covered) == table
    assert parts[0][0].startswith("🟢 Riepilogo giornaliero")


# -- «Decisioni di oggi» lines (telegram/summaries.py) ----------------------------------


def _decision(**kw):
    from smtrading.activity.models import Decision

    base = {"kind": "strategy", "summary": "Decisione", "details": {}, "outcome": None,
            "reason": None, "subject": None}
    base.update(kw)
    return Decision(**base)


def test_decision_line_full():
    from smtrading.telegram.summaries import decision_line

    d = _decision(outcome="approvata", details={"ref": "STR-001"},
                  subject="incrocio medie mobili 10/30 su SPY, QQQ e DIA",
                  reason="trend confermato")
    assert decision_line(d, "CEO", "Prudente") == (
        "✅ STR-001 · incrocio medie mobili 10/30 su SPY, QQQ e DIA — Prudente — "
        "trend confermato (CEO)")


def test_decision_line_fallback_summary_no_ids():
    from smtrading.telegram.summaries import decision_line

    uid = "3f2a9c1e-1b2c-4d5e-8f90-123456789abc"
    summary = f"Approvo la proposta #42 ({uid}) " + "x" * 200
    line = decision_line(_decision(kind="proposal", summary=summary[:200]), "Risk", None)
    assert uid not in line and "#42" not in line
    assert line.startswith("• ") and line.endswith(" — generale (Risk)")
    subject = line[len("• "):-len(" — generale (Risk)")]
    assert len(subject) == 120 and subject.endswith("…")
    upper = decision_line(_decision(summary=f"Vedi {uid.upper()}"), "Risk", None)
    assert upper == "• Vedi — generale (Risk)"


def test_decision_line_icons_and_ref():
    from smtrading.telegram.summaries import OUTCOME_ICON, decision_line

    assert OUTCOME_ICON == {"approvata": "✅", "respinta": "❌", "sospesa": "⏸",
                            "modificata": "✏️", "informativa": "ℹ️"}
    assert decision_line(_decision(kind="approval"), "A", None).startswith("✅ Decisione")
    assert decision_line(_decision(kind="veto"), "A", None).startswith("❌ Decisione")
    assert decision_line(_decision(kind="veto", outcome="sospesa"), "A", None
                         ).startswith("⏸ Decisione")
    # a ref longer than 20 characters, or not a text, is left out
    assert decision_line(_decision(details={"ref": "R" * 21}), "A", "P") == (
        "• Decisione — P (A)")
    assert decision_line(_decision(details={"ref": 7}), "A", "P") == "• Decisione — P (A)"


def test_decision_groups():
    from smtrading.telegram.summaries import DECISION_GROUPS

    assert DECISION_GROUPS == (
        (("strategy",), "Strategie"), (("risk_rule",), "Rischio"),
        (("allocation",), "Allocazioni"), (("veto", "approval", "proposal"), "Veti e approvazioni"),
        (("other",), "Altro"))


def test_render_fill_details_after_summary():
    text = render_activity(_act(), Names(), TZ,
                           ["Strategia: STR-103", "Rischio allo stop: 7,75 €"])
    assert text == ("🟢 Ordine eseguito\nOrdine eseguito: buy 1 AAPL\n"
                    "Strategia: STR-103\nRischio allo stop: 7,75 €\n10:05")
