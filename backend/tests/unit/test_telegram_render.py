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
