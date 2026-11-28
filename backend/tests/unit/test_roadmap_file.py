"""The «Roadmap» file (`smtrading/roadmap/roadmap.json`): valid, complete (every spec and
plan of docs/superpowers referenced), truthful about the drills, with every acronym in the
glossary and no secret; the validator, the stale-status check and the pure live helpers."""

import copy
import re
import warnings
from datetime import date
from importlib import resources
from pathlib import Path

import pytest

from smtrading.roadmap.service import (
    OWNERS,
    STATUSES,
    all_refs,
    campaign_day,
    drills_count,
    load_roadmap,
    stale_warnings,
    validate_roadmap,
)

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs" / "superpowers"

# stock and coin symbols and plain geography: not acronyms to explain
NOT_ACRONYMS = {"MSFT", "AAPL", "JPM", "PEP", "BTC", "SPY", "QQQ", "VWCE", "USA", "UE",
                "EUR", "AGG", "PRU", "MOD", "A", "B", "C", "I", "II"}


def _fresh() -> dict:
    return copy.deepcopy(load_roadmap())


def _texts(data: dict) -> list[str]:
    out = [data["summary"]]
    for s in data["stages"]:
        out += [s["title"], s["what"], s["why"]]
        for i in s["items"]:
            out += [i["title"], i["detail"]]
    return out


def test_the_file_is_package_data_and_valid():
    assert (resources.files("smtrading.roadmap") / "roadmap.json").is_file()
    data = load_roadmap()
    assert validate_roadmap(data) == []
    assert set(data) >= {"updated", "summary", "campaign", "go_no_go", "stages", "glossary"}
    assert {s["status"] for s in data["stages"]} <= set(STATUSES)
    assert {s["owner"] for s in data["stages"]} <= set(OWNERS)


def test_campaign_and_decision_dates():
    data = load_roadmap()
    c = data["campaign"]
    assert (c["start"], c["end"]) == ("2026-11-23", "2026-12-23")
    assert data["go_no_go"] == "2026-12-23"
    assert [p["code"] for p in c["portfolios"]] == ["AGG", "PRU", "MOD"]
    assert c["goal"]["label"] == "Mensile +5%"


def _doc_files() -> list[str]:
    return sorted(p.relative_to(REPO).as_posix()
                  for folder in ("specs", "plans") for p in (DOCS / folder).glob("*.md"))


def _unreferenced(data: dict) -> list[str]:
    refs = all_refs(data)
    return [f for f in _doc_files() if f not in refs]


def test_every_spec_and_plan_is_referenced():
    """A new spec or plan without a roadmap item fails here, with its file name."""
    if not DOCS.is_dir():
        pytest.skip("docs/superpowers not present (container image)")
    assert len(_doc_files()) >= 30
    missing = _unreferenced(load_roadmap())
    assert not missing, f"non citati nella roadmap: {missing}"


def test_every_ref_exists_in_the_repo():
    if not DOCS.is_dir():
        pytest.skip("docs/superpowers not present (container image)")
    absent = sorted(r for r in all_refs(load_roadmap()) if not (REPO / r).is_file())
    assert not absent, f"riferimenti inesistenti: {absent}"


def test_the_guard_names_a_missing_spec():
    if not DOCS.is_dir():
        pytest.skip("docs/superpowers not present (container image)")
    data = _fresh()
    for s in data["stages"]:
        for i in s["items"]:
            i["refs"] = [r for r in i["refs"] if "fiume-flussi-design" not in r]
    assert _unreferenced(data) == [
        "docs/superpowers/specs/2026-11-28-smtrading-fiume-flussi-design.md"]


def test_drills_with_their_real_status():
    data = load_roadmap()
    drills = {i["date"]: i for s in data["stages"] for i in s["items"]
              if i.get("kind") == "esercitazione"}
    assert drills["2026-11-21"]["status"] == "fatto"
    assert drills["2026-11-27"]["status"] == "fatto"
    for day in ("2026-11-29", "2026-12-02", "2026-12-05", "2026-12-09", "2026-12-12"):
        assert drills[day]["status"] == "prossimo", day
    done, planned = drills_count(data)
    assert planned == sum(1 for s in data["stages"] for i in s["items"]
                          if i.get("kind") == "esercitazione")
    assert 0 < done < planned


def test_validator_catches_bad_content():
    good = _fresh()
    assert validate_roadmap(good) == []

    bad = _fresh()
    bad["stages"][0]["status"] = "finito"
    bad["stages"][1]["owner"] = "luca"
    bad["stages"][2]["items"][0]["status"] = "boh"
    bad["stages"][3]["start"] = "28/11/2026"
    bad["stages"][4]["end"] = "2026-01-01"
    bad["stages"][5]["id"] = bad["stages"][0]["id"]
    del bad["stages"][6]["why"]
    bad["stages"][0]["items"][0]["refs"] = "docs/x.md"
    bad["stages"][0]["items"][1]["kind"] = "gara"
    bad["glossary"].append({"term": "", "meaning": "x"})
    errors = "\n".join(validate_roadmap(bad))
    for needle in ("finito", "luca", "boh", "28/11/2026", "fine prima dell'inizio",
                   "id ripetuto", "why", "refs", "gara", "glossario"):
        assert needle in errors, needle

    assert validate_roadmap({"stages": []})  # missing top-level fields


def test_stale_status_is_reported():
    data = _fresh()
    stage = next(s for s in data["stages"] if any(i["status"] == "prossimo" for i in s["items"]))
    item = next(i for i in stage["items"] if i["status"] == "prossimo")
    item["date"] = "2026-11-01"
    out = stale_warnings(data, date(2026, 11, 28))
    assert any(item["title"] in w for w in out), out
    # a stage still «prossimo» after its start
    data = _fresh()
    future = next(s for s in data["stages"] if s["status"] == "prossimo")
    future["start"] = "2026-11-01"
    assert any(future["title"] in w for w in stale_warnings(data, date(2026, 11, 28)))


def test_the_real_file_is_not_stale_on_its_update_day():
    data = load_roadmap()
    assert stale_warnings(data, date.fromisoformat(data["updated"])) == []
    late = stale_warnings(data, date.today())
    if late:  # a reminder, not a date bomb: the file needs an update
        warnings.warn("roadmap da aggiornare: " + "; ".join(late), stacklevel=1)


def test_every_acronym_is_in_the_glossary():
    data = load_roadmap()
    known: set[str] = set()
    for g in data["glossary"]:
        known.update(re.findall(r"[A-Za-z0-9]+", g["term"]))
    acronyms = {a for t in _texts(data) for a in re.findall(r"\b[A-Z][A-Z0-9]{1,}\b", t)}
    unexplained = sorted(acronyms - known - NOT_ACRONYMS)
    assert not unexplained, f"sigle senza spiegazione nel glossario: {unexplained}"


def test_glossary_has_the_spec_terms():
    terms = " ".join(g["term"].casefold() for g in load_roadmap()["glossary"])
    for word in ("agent", "paperclip", "mcp", "broker", "conto di prova", "demo", "reale",
                 "backtest", "forchetta di rischio", "scalping", "intraday", "campagna",
                 "go/no-go", "neurograph"):
        assert word in terms, word


def test_no_secrets_ips_or_emails():
    raw = (resources.files("smtrading.roadmap") / "roadmap.json").read_text("utf-8")
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", raw), "indirizzo IP"
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", raw), "indirizzo email"
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", raw), "id interno"
    for word in ("Bearer", "SMT_", "token", "password"):
        assert word not in raw, word


def test_campaign_day():
    start, end = date(2026, 11, 23), date(2026, 12, 23)
    assert campaign_day(start, end, date(2026, 11, 28)) == (6, 30, "in_corso")
    assert campaign_day(start, end, date(2026, 11, 23)) == (1, 30, "in_corso")
    assert campaign_day(start, end, date(2026, 12, 23)) == (30, 30, "in_corso")
    assert campaign_day(start, end, date(2026, 11, 20)) == (0, 30, "prima")
    assert campaign_day(start, end, date(2026, 12, 30)) == (30, 30, "conclusa")
