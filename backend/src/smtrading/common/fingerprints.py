"""The words that would give the simulation away to an agent (`mcp_hide_mode`).

One list for three uses: the admin is warned when a name the agents will see contains one
of them (never blocked), a broker text giving the simulation away reads to agents as a
plain refusal (`mcp.dto`), and the MCP sweep test scans every agent-facing output for
them. The sweep uses the raw `FINGERPRINT` (strict: any match is a leak); the other two
use `has_fingerprint`, which lets through the ordinary Italian words that merely start
like one («testo», «simultaneo», «demolire»: `INNOCENT_WORDS`).
"""

import re

FINGERPRINT: re.Pattern[str] = re.compile(
    r"\bsim\b|simul|paper|demo|\bdry|\bprova\b|\btest|fake|mock|sandbox|virtual|smoke",
    re.IGNORECASE,
)

# whole words (case-insensitive) that contain a FINGERPRINT match but give nothing away;
# any other word holding a match counts («testing», «DemoAccount», «simulazione»)
INNOCENT_WORDS: re.Pattern[str] = re.compile(
    r"test[oaie]|testual\w*|testimon\w*|testament\w*|testat[aeio]"
    r"|simultane\w*"
    r"|democra\w*|demograf\w*|demoli\w*|demoniac\w*|demoni\w*|demoralizz\w*"
    r"|demoscop\w*",
    re.IGNORECASE,
)

NAME_WARNING = "gli agenti vedranno questo nome"


def _word_at(text: str, start: int, end: int) -> str:
    """The whole word (letters only) around text[start:end]."""
    while start > 0 and text[start - 1].isalpha():
        start -= 1
    while end < len(text) and text[end].isalpha():
        end += 1
    return text[start:end]


def has_fingerprint(text: str) -> bool:
    """Does `text` hold a simulation word? A FINGERPRINT match inside one of the
    INNOCENT_WORDS does not count; every other match does."""
    return any(not INNOCENT_WORDS.fullmatch(_word_at(text, m.start(), m.end()))
               for m in FINGERPRINT.finditer(text))


def name_warning(name: str) -> str | None:
    """NAME_WARNING when `name` contains a simulation word, else None."""
    return NAME_WARNING if has_fingerprint(name) else None
