"""The words that would give the simulation away, shared by the admin name warning and
the MCP fingerprint sweep."""

import pytest

from smtrading.common.fingerprints import NAME_WARNING, name_warning


@pytest.mark.parametrize("name", [
    "Test 250 · A", "Paperclip", "Demo", "paperclip-demo", "smoke-agent", "Prova", "fake",
])
def test_fingerprint_matches_spec_words(name):
    assert name_warning(name) == NAME_WARNING == "gli agenti vedranno questo nome"


@pytest.mark.parametrize("name", ["Portafoglio Prudente", "Orion"])
def test_name_warning_none_for_neutral(name):
    assert name_warning(name) is None
