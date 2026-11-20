"""Master key rotation: the generated key and the strength check (no database)."""

import pytest

from smtrading.devtools import rotate_master_key as rot


def test_generated_keys_are_strong_and_unique():
    keys = {rot.generate_key() for _ in range(50)}
    assert len(keys) == 50
    for key in keys:
        assert len(key) >= 64
        assert rot.key_problem(key) is None


def test_generate_prints_only_the_new_key(monkeypatch, capsys):
    monkeypatch.setenv("SMT_MASTER_KEY", "the-old-key-must-never-be-printed")
    monkeypatch.setenv(rot.NEW_KEY_ENV, "nor-this-one-0123456789-ABCDEFGHIJ")
    assert rot.main(["--generate"]) == 0
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert len(lines) == 1 and out.err == ""
    assert rot.key_problem(lines[0]) is None
    assert "old-key" not in out.out and "nor-this-one" not in out.out


@pytest.mark.parametrize("key, fragment", [
    (None, "mancante"),
    ("", "mancante"),
    ("Ab1" * 10, "troppo corta"),
    ("Ab1-" * 8 + " x", "spazi"),
    ("ab" * 20, "caratteri diversi"),
    ("abcdefghijklmnopqrstuvwxyzabcdefgh", "due tipi"),
])
def test_weak_keys_are_refused(key, fragment):
    problem = rot.key_problem(key)
    assert problem is not None and fragment in problem
    if key:
        assert key not in problem  # never quoted


def test_new_key_equal_to_old_is_refused():
    key = rot.generate_key()
    assert "uguale" in rot.key_problem(key, key)


def test_hex_and_urlsafe_keys_are_accepted():
    import secrets
    assert rot.key_problem(secrets.token_hex(32)) is None
    assert rot.key_problem(secrets.token_urlsafe(32)) is None


def test_generate_and_apply_are_exclusive():
    with pytest.raises(SystemExit):
        rot.main(["--generate", "--apply"])
