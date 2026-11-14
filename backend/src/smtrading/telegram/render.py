"""Pure rendering of Telegram messages: prefixes, activity lines, splitting.

Messages are plain text (no parse_mode); the only formatting is a `pre` entity whose offset
and length are in UTF-16 code units, as the Bot API requires.
"""

import re
import uuid
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from smtrading.activity.models import Activity

PREFIX = {"critical": "🔴 ", "warning": "🟠 ", "executed": "🟢 ", "info": ""}
MAX_LEN = 4096

TITLES = {
    "order.accepted": "Ordine accettato",
    "order.filled": "Ordine eseguito",
    "order.partially_filled": "Ordine eseguito in parte",
    "order.rejected": "Ordine rifiutato",
    "order.canceled": "Ordine annullato",
    "order.expired": "Ordine scaduto",
    "kill_switch": "Kill switch",
    "portfolio.reset": "Portafoglio azzerato",
    "portfolio.cash": "Movimento di cassa",
    "liquidation": "Liquidazione",
    "daily_loss": "Perdita giornaliera",
    "margin_warning": "Margine",
    "broker.order_unknown": "Ordine sconosciuto al broker",
    "broker.liquidation_waiting": "Liquidazione in attesa del broker",
    "broker.fill_price_missing": "Prezzo di esecuzione mancante",
    "broker.reconciliation_diff": "Riconciliazione broker",
    "order.replace_failed": "Modifica dell'ordine non riuscita",
    "price_alert": "Alert di prezzo",
}
# control characters except \n and \t, plus lone surrogates (not encodable as UTF-8/16)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")


@dataclass
class Names:
    agents: dict[uuid.UUID, str] = field(default_factory=dict)
    portfolios: dict[uuid.UUID, str] = field(default_factory=dict)


def title_for(type_: str) -> str:
    if type_ in TITLES:
        return TITLES[type_]
    if type_.startswith("decision."):
        return "Decisione"
    return type_


def render_activity(a: Activity, names: Names, tz: str) -> str:
    """`{prefix}{title} — {portfolio}\\n{summary}\\nAgente: {name} · {HH:MM}`; lines (or
    parts) without data are left out."""
    head = f"{PREFIX.get(a.level, '')}{title_for(a.type)}"
    portfolio = names.portfolios.get(a.portfolio_id) if a.portfolio_id else None
    if portfolio:
        head += f" — {portfolio}"
    lines = [head]
    if a.summary:
        lines.append(a.summary)
    tail = []
    agent = names.agents.get(a.agent_id) if a.agent_id else None
    if agent:
        tail.append(f"Agente: {agent}")
    if a.ts is not None:
        tail.append(a.ts.astimezone(ZoneInfo(tz)).strftime("%H:%M"))
    if tail:
        lines.append(" · ".join(tail))
    return "\n".join(lines)


def utf16_len(s: str) -> int:
    """Length in UTF-16 code units; a lone surrogate counts as one and never raises."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def clean_text(text: str) -> str:
    """Removes control characters except \\n and \\t and lone surrogates, then strips."""
    return _CONTROL.sub("", text).strip()


def _cut(line: str, limit: int) -> list[str]:
    """Cuts one over-long line into pieces of at most `limit` UTF-16 units, never inside a
    surrogate pair (each piece is whole code points)."""
    pieces, cur, cur_len = [], [], 0
    for ch in line:
        n = utf16_len(ch)
        if cur_len + n > limit:
            pieces.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(ch)
        cur_len += n
    pieces.append("".join(cur))
    return pieces


def _pack(lines: list[str], limit: int) -> list[str]:
    """Greedy: joins lines with \\n into parts of at most `limit` UTF-16 units."""
    parts: list[str] = []
    cur: str | None = None
    for raw in lines:
        for line in _cut(raw, limit) if utf16_len(raw) > limit else [raw]:
            if cur is None:
                cur = line
            elif utf16_len(cur) + 1 + utf16_len(line) <= limit:
                cur = f"{cur}\n{line}"
            else:
                parts.append(cur)
                cur = line
    if cur is not None:
        parts.append(cur)
    return parts


def split_message(text: str, pre_block: str | None = None
                  ) -> list[tuple[str, list[dict] | None]]:
    """Splits at line boundaries into parts of at most MAX_LEN (measured in UTF-16 units,
    which is never less than the character count); a single longer line is cut. Parts that
    are empty or only whitespace are dropped (Telegram rejects an empty text), so
    `split_message("")` is `[]`.

    `pre_block` follows the text (after a blank line); every part holding (a piece of) it
    gets a `pre` entity covering exactly that piece."""
    out: list[tuple[str, list[dict] | None]] = [
        (p, None) for p in (_pack(text.split("\n"), MAX_LEN) if text else []) if p.strip()
    ]
    if not pre_block or not pre_block.strip():
        return out
    pre_lines = pre_block.split("\n")
    # try to fit the start of the table after the last text part
    if out:
        last = out.pop()[0]
        room = MAX_LEN - utf16_len(last) - 2
        head: list[str] = []
        while pre_lines and room > 0:
            need = utf16_len(pre_lines[0]) + (1 if head else 0)
            if need > room:
                break
            head.append(pre_lines.pop(0))
            room -= need
        block = "\n".join(head)
        if block.strip():
            prefix = f"{last}\n\n"
            out.append((prefix + block, _pre(prefix, block)))
        else:
            out.append((last, None))
    for block in _pack(pre_lines, MAX_LEN) if pre_lines else []:
        if block.strip():
            out.append((block, _pre("", block)))
    return out


def _pre(before: str, block: str) -> list[dict] | None:
    """The entity list for a `pre` block after `before` (None for an empty block: Telegram
    rejects zero-length entities)."""
    length = utf16_len(block)
    if not length:
        return None
    return [{"type": "pre", "offset": utf16_len(before), "length": length}]

