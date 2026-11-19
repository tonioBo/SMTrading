"""The appendix of the agent instructions («Istruzioni agenti», slug `appendice`): the rare
cases moved out of the instructions every run loads, read on demand by the agents
(`get_instructions_appendix`) or from the exported `APPENDICE.md`.

A section starts with a line `## §<ID> · <title>` (ID: capital letters then digits, e.g.
`C9`, `PM3`) and runs until the next section heading; text before the first one is the
appendix's own introduction."""

import re
from dataclasses import dataclass

APPENDIX_SLUG = "appendice"
APPENDIX_FILE = "APPENDICE.md"

_HEADING = re.compile(r"^## §([A-Z]+[0-9]+) · (.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    text: str


def sections(body: str) -> list[Section]:
    """The sections of the appendix, in order (a repeated id keeps its first section)."""
    heads = list(_HEADING.finditer(body))
    out: dict[str, Section] = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        out.setdefault(m.group(1), Section(m.group(1), m.group(2),
                                           body[m.end():end].strip()))
    return list(out.values())


def normalize_id(raw: str) -> str:
    """«§pm3», « PM3 » -> «PM3»."""
    return raw.strip().lstrip("§").strip().upper()
