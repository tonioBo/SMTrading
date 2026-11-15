"""The words that would give the simulation away to an agent (`mcp_hide_mode`).

One list for two uses: the admin is warned when a name the agents will see contains one
of them (never blocked), and the MCP sweep test scans every agent-facing output for them.
"""

import re

FINGERPRINT: re.Pattern[str] = re.compile(
    r"\bsim\b|simul|paper|demo|\bdry|\bprova\b|\btest|fake|mock|sandbox|virtual|smoke",
    re.IGNORECASE,
)

NAME_WARNING = "gli agenti vedranno questo nome"


def name_warning(name: str) -> str | None:
    """NAME_WARNING when `name` contains a simulation word, else None."""
    return NAME_WARNING if FINGERPRINT.search(name) else None
