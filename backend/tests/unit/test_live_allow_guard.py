"""M5 (plan 8): only the activation service links a portfolio to a real-money link: no
other module passes `_allow_live` to brokers.service.link_portfolio."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "smtrading"
ALLOWED = {"brokers/live/service.py"}
DEFINER = "brokers/service.py"  # the parameter itself


def test_only_the_live_service_passes_allow_live():
    hits = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel != DEFINER and re.search(r"_allow_live\s*=", path.read_text(encoding="utf-8")):
            hits.add(rel)
    assert hits == ALLOWED
