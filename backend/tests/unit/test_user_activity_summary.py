"""The request summary of the user activity log: secrets masked by key name and by the
log secret filter, sizes capped, non-JSON bodies described, never echoed."""

import json

from smtrading.useractivity.sanitize import MASK, SUMMARY_MAX_CHARS, summarize, target_of

TOKEN = "123456789:AAH" + "z" * 32


def _s(obj) -> dict:
    return summarize(json.dumps(obj).encode(), "application/json")


def test_secret_keys_are_masked_at_any_depth():
    out = _s({"email": "a@b.c", "password": "p4ssw0rd!", "totp": "123456", "code": "654321",
              "current": "old", "new": "new-one", "config": {"api_key": "K", "x": {"secret": 1},
                                                            "keys": ["a", "b"]},
              "bot_token": "t", "key_id": "AK", "pem": "-----BEGIN"})
    assert out["email"] == "a@b.c"
    for k in ("password", "totp", "code", "current", "new", "bot_token", "key_id", "pem"):
        assert out[k] == MASK, k
    assert out["config"]["api_key"] == MASK and out["config"]["x"]["secret"] == MASK
    assert out["config"]["keys"] == MASK


def test_secret_values_under_innocent_keys_are_redacted():
    out = _s({"note": f"il token {TOKEN} qui"})
    assert TOKEN not in json.dumps(out)


def test_empty_secret_is_not_hidden():
    assert _s({"password": ""})["password"] == ""
    assert _s({"totp": None})["totp"] is None


def test_sizes_are_capped():
    out = _s({"name": "n" * 5000, "items": list(range(500)), "deep": {"a": {"b": {"c": {"d": 1}}}}})
    assert len(out["name"]) <= 201
    assert len(out["items"]) <= 21
    assert out["deep"]["a"]["b"] == "…"
    big = _s({f"k{i}": "v" * 150 for i in range(100)})
    assert len(json.dumps(big, ensure_ascii=False)) <= SUMMARY_MAX_CHARS + 200


def test_non_json_and_empty_bodies():
    assert summarize(b"", None) == {}
    assert summarize(b"not json", "text/plain") == {"_corpo": "non JSON (8 byte)"}
    assert summarize(b"{bad", "application/json") == {"_corpo": "non JSON (4 byte)"}
    assert summarize(b"[1, 2]", "application/json") == {"_corpo": [1, 2]}
    assert summarize(None, None, truncated=True) == {"_corpo": "troppo grande"}


def test_target_of_path_params():
    assert target_of({"user_id": "abc", "n": 3}) == {"user_id": "abc", "n": "3"}
    assert target_of({"slug": "x" * 500})["slug"] == "x" * 100
