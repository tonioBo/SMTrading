"""eToro real money. Built only on a LiveClient: every request goes through the live gate.
The URL is the shared eToro host (ETORO_URL, the demo's too): what tells real from demo
is the path, so this file is the only one of the repository (outside `docs/`) that
writes the real execution and info prefixes (a repository scan test checks it). Every
request is checked by `_EtoroProtocol._guard_path` first: a `/demo/` path is refused.

Placement is not idempotent (no resend): an unknown outcome is looked up by
`referenceId`, never sent again.

Docs: https://api-portal.etoro.com/llms.txt (consulted 2026-11-15).

To verify with the user's real key before arming (assumed shapes, parsed fail-safe by
`_EtoroProtocol`: an unknown shape never invents a price or units, the order waits):
1. the eligibility response (`POST {INFO_V2}/eligibility`);
2. the order lookup wrapper (`GET {INFO_V2}/orders:lookup`);
3. the close-order fields `statusID`, `positions[].units`/`rate` (checked on demo 21/11/2026)
   (`GET {INFO_V1}/close-orders/{id}`).
"""

from typing import ClassVar, Literal

from smtrading.brokers.etoro_core import ETORO_URL, MARKET_DATA_PREFIX, _EtoroProtocol
from smtrading.brokers.live import require_live_construction
from smtrading.brokers.registry import register


@register
class EtoroLiveAdapter(_EtoroProtocol):
    """Config: `api_key` (x-api-key), `user_key` (x-user-key created for the Real
    environment, Write permission)."""

    kind: ClassVar[str] = "etoro_live"
    live: ClassVar[bool] = True
    idempotent_place: ClassVar[bool] = False
    BASE_URL: ClassVar[str] = ETORO_URL
    ENV: ClassVar[Literal["demo", "real"]] = "real"
    EXEC_V1: ClassVar[str] = "/api/v1/trading/execution"
    EXEC_V2: ClassVar[str] = "/api/v2/trading/execution"
    INFO_V1: ClassVar[str] = "/api/v1/trading/info/real"
    INFO_V2: ClassVar[str] = "/api/v2/trading/info"
    ALLOWED_PREFIXES: ClassVar[tuple[str, ...]] = (
        f"{EXEC_V1}/",
        f"{EXEC_V2}/",
        f"{INFO_V1}/",
        f"{INFO_V2}/",
        MARKET_DATA_PREFIX,
    )
    # POSTs that only read (no order): the live gate passes them like a GET
    READ_ONLY_POSTS: ClassVar[frozenset[str]] = frozenset({f"{INFO_V2}/eligibility"})

    def __init__(self, config: dict, http) -> None:
        require_live_construction(type(self), config, http)
        super().__init__(config, http)
