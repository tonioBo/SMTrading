"""The appendix of the agent instructions (`10-appendice.md`, slug `appendice`): its
sections parse, every «→ §<id>» of the instructions exists, it holds no simulation word,
and the instructions every run loads stay within their budget."""

import re
from pathlib import Path

import pytest

from smtrading.agentdocs.appendix import normalize_id, sections
from smtrading.agentdocs.export import full_instructions
from smtrading.agentdocs.service import doc_identity
from smtrading.common.fingerprints import has_fingerprint

DOCS = Path(__file__).resolve().parents[3] / "docs" / "paperclip-agents"
APPENDIX = (DOCS / "10-appendice.md").read_text("utf-8")
ROLES = sorted(DOCS.glob("0[1-9]-*.md"))
# characters of each agent's AGENTS.md (common + role) before the appendix existed
BEFORE = {"ceo": 71504, "portfolio-manager": 76612, "risk-manager": 69019,
          "execution-supervisor": 59907, "market-analyst": 53509, "news-analyst": 53513,
          "quant-researcher": 57829, "performance-auditor": 54621, "notifier": 54907}


def test_appendix_is_its_own_doc():
    assert doc_identity("10-appendice.md", APPENDIX) == (
        "appendice", "Appendice delle istruzioni (casi rari)")


def test_sections_parse_with_unique_ids():
    found = sections(APPENDIX)
    ids = [s.id for s in found]
    assert len(ids) == len(set(ids)) == len(re.findall(r"(?m)^## §", APPENDIX))
    assert {"C1", "C9", "C17", "C18", "CEO2", "PM5", "RM2", "ES1", "NOT1"} <= set(ids)
    assert all(s.title and s.text for s in found)
    c9 = next(s for s in found if s.id == "C9")
    assert c9.text.startswith("Il trading halt") and "## §" not in c9.text


def test_sections_split_on_headings_only():
    body = "intro\n\n## §A1 · Uno\n\ntesto uno\n### sotto\n\n## §B12 · Due\ntesto due\n"
    assert [(s.id, s.title, s.text) for s in sections(body)] == [
        ("A1", "Uno", "testo uno\n### sotto"), ("B12", "Due", "testo due")]
    assert sections("nessuna sezione") == []


@pytest.mark.parametrize("raw", ["pm3", "§PM3", " §pm3 ", "PM3"])
def test_normalize_id(raw):
    assert normalize_id(raw) == "PM3"


def test_every_reference_of_the_instructions_exists():
    ids = {s.id for s in sections(APPENDIX)}
    used = set()
    for p in [DOCS / "00-regole-comuni.md", *ROLES]:
        used |= set(re.findall(r"§([A-Z]+[0-9]+)", p.read_text("utf-8")))
    assert used <= ids, used - ids
    assert ids <= used, ids - used  # no orphan section either


def test_appendix_holds_no_simulation_word():
    assert not [line for line in APPENDIX.splitlines() if has_fingerprint(line)]


def test_instructions_loaded_every_run_are_at_most_45_percent_of_before():
    common = (DOCS / "00-regole-comuni.md").read_text("utf-8")
    for p in ROLES:
        slug = p.stem[3:]
        size = len(full_instructions(common, p.read_text("utf-8")))
        assert size <= 0.45 * BEFORE[slug], (slug, size)
