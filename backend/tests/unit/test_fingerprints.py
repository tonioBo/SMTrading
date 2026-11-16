"""The words that would give the simulation away, shared by the admin name warning and
the MCP fingerprint sweep."""

import pytest

from smtrading.common.fingerprints import NAME_WARNING, has_fingerprint, name_warning


@pytest.mark.parametrize("name", [
    "Test 250 · A", "Paperclip", "Demo", "paperclip-demo", "smoke-agent", "Prova", "fake",
])
def test_fingerprint_matches_spec_words(name):
    assert name_warning(name) == NAME_WARNING == "gli agenti vedranno questo nome"


@pytest.mark.parametrize("name", ["Portafoglio Prudente", "Orion"])
def test_name_warning_none_for_neutral(name):
    assert name_warning(name) is None


@pytest.mark.parametrize("text", [
    "testo", "Testi", "testa", "simultaneo", "simultaneamente", "demolire", "democrazia",
    "attestato", "contestato", "testimone", "demografia",
])
def test_has_fingerprint_ignores_innocent_italian_words(text):
    assert not has_fingerprint(f"Un {text} qualsiasi")


@pytest.mark.parametrize("text", [
    "test", "Demo", "simulazione", "simulato", "paper trading", "dry run", "dry_run",
    "sandbox", "DemoAccount", "testnet", "testing", "demo-account", "prova", "sim",
    "Test 250 · A", "smoke-agent", "fake", "mock", "virtual",
    "testo di prova",  # one innocent word does not hide another that is not
])
def test_has_fingerprint_catches_simulation_words(text):
    assert has_fingerprint(text)
