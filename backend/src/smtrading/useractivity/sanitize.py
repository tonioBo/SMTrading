"""The request summary of the activity log: the JSON body with every secret masked (by key
name, at any depth, and by the log secret filter on every text), sizes capped."""

import json
from typing import Any

from smtrading.admin.common import is_secret
from smtrading.common.logsetup import redact_log_text

MASK = "••••"
TRUNCATED = "…"
SUMMARY_MAX_CHARS = 2000
MAX_TEXT, MAX_ITEMS, MAX_KEYS, MAX_DEPTH = 200, 20, 30, 3
TARGET_MAX = 100
# besides the connector hints (key, secret, token, pem, password): TOTP codes and the
# password change fields
_SECRET_KEYS = frozenset({"totp", "code", "otp", "current", "new", "credentials",
                          "authorization", "cookie"})


def _secret_key(key: str) -> bool:
    return is_secret(key) or key.lower() in _SECRET_KEYS


def _text(value: str) -> str:
    value = redact_log_text(value)
    return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + TRUNCATED


def _clean(value: Any, depth: int) -> Any:
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:
            return TRUNCATED
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_KEYS:
                out[TRUNCATED] = f"+{len(value) - MAX_KEYS}"
                break
            key = _text(str(k))
            out[key] = MASK if _secret_key(key) and v not in (None, "") else _clean(v, depth + 1)
        return out
    if isinstance(value, list):
        items = [_clean(v, depth + 1) for v in value[:MAX_ITEMS]]
        return items + [f"+{len(value) - MAX_ITEMS}"] if len(value) > MAX_ITEMS else items
    if isinstance(value, str):
        return _text(value)
    return value  # numbers, booleans, null


def _capped(summary: dict[str, Any]) -> dict[str, Any]:
    """The first keys that fit SUMMARY_MAX_CHARS as JSON; the others are dropped."""
    if len(json.dumps(summary, ensure_ascii=False)) <= SUMMARY_MAX_CHARS:
        return summary
    out: dict[str, Any] = {}
    for k, v in summary.items():
        if len(json.dumps({**out, k: v}, ensure_ascii=False)) > SUMMARY_MAX_CHARS:
            break
        out[k] = v
    out[TRUNCATED] = True
    return out


def summarize(body: bytes | None, content_type: str | None, *, truncated: bool = False
              ) -> dict[str, Any]:
    """The summary of a request body (never the body itself when it is not JSON)."""
    if truncated:
        return {"_corpo": "troppo grande"}
    if not body:
        return {}
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {"_corpo": f"non JSON ({len(body)} byte)"}
    cleaned = _clean(data, 0)
    return _capped(cleaned if isinstance(cleaned, dict) else {"_corpo": cleaned})


def target_of(path_params: dict[str, Any] | None) -> dict[str, str]:
    """The ids of the request path (route parameters), as text."""
    return {str(k): _text(str(v))[:TARGET_MAX] for k, v in (path_params or {}).items()}
