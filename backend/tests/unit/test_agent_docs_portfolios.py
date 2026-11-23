"""The agent instructions name no portfolio: the agents take every portfolio, its code
(`code`), its character (`risk_profile.description`) and its limits from SMTrading at each
cycle (`list_portfolios`, `get_risk_limits`), so a portfolio the admins add or archive needs
no change to the instructions. The README (for humans) may show the current setup as an
example; the instructions the agents read (00-10, HANDOFFS) may not."""

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[3] / "docs" / "paperclip-agents"
AGENT_FACING = [*sorted(DOCS.glob("[01][0-9]-*.md")), DOCS / "HANDOFFS.md"]

# the portfolios that are not archived (and the former names): keep up to date when the
# admins add or rename one, so the instructions never hardcode it
PORTFOLIO_NAMES = ["Portafoglio eToro", "Portafoglio Alpaca", "Portafoglio Moderato",
                   "Portafoglio Prudente", "Portafoglio Aggressivo"]
PORTFOLIO_CODES = ["PRU", "AGG", "MOD"]
# a count of portfolios («3 portafogli», «i due portafogli»); «tra due portafogli attivi»
# (any two of them) is a generic rule and allowed
COUNT = re.compile(r"(?i)\b(\d+|tre|quattro|cinque)\s+portafogli\b"
                   r"|\b(i|entrambi i|tutti e)\s+(\d+|due|tre)\s+portafogli\b"
                   r"|\bentrambi i portafogli\b")


def test_the_agent_facing_docs_are_all_checked():
    names = {p.name for p in AGENT_FACING}
    assert {"00-regole-comuni.md", "02-portfolio-manager.md", "10-appendice.md",
            "HANDOFFS.md"} <= names
    assert len(names) == 12


@pytest.mark.parametrize("path", AGENT_FACING, ids=lambda p: p.name)
def test_no_portfolio_name_code_or_count(path):
    text = path.read_text("utf-8")
    lowered = text.lower()
    found = [n for n in PORTFOLIO_NAMES if n.lower() in lowered]
    found += [c for c in PORTFOLIO_CODES if re.search(rf"\b{c}\b", text)]
    found += [m.group(0) for m in COUNT.finditer(text)]
    assert not found, (path.name, found)


def test_common_rules_take_the_portfolios_from_smtrading():
    common = (DOCS / "00-regole-comuni.md").read_text("utf-8")
    section = common.split("## 2.")[1].split("## 3.")[0]
    for needle in ("A ogni ciclo `list_portfolios`", "`code`", "`risk_profile.description`",
                   "`get_risk_limits`", "uno nuovo", "non compare più"):
        assert needle in section, needle
    assert "max 2 strumenti in comune tra due portafogli attivi" in common


def test_the_pattern_catches_counts_and_codes():
    assert COUNT.search("SMTrading gestisce 3 portafogli")
    assert COUNT.search("confronta i due portafogli")
    assert COUNT.search("trade su entrambi i portafogli")
    assert not COUNT.search("max 2 strumenti in comune tra due portafogli attivi")
    assert re.search(r"\bAGG\b", "CHIUDI · AGG · NVDA")
    assert not re.search(r"\bMOD\b", "MODIFICA STOP")
