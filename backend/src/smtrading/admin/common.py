"""Helpers shared by the admin routers: validation errors, the handler transaction, the
per-request quote source and the masking/merging of connector secrets."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import MemoQuoteSource
from smtrading.events import discard_post_commit, dispatch_post_commit


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def get_quotes(request: Request) -> MemoQuoteSource:
    """A fresh per-request memo over the app's quote source (the MarketDataRouter)."""
    return MemoQuoteSource(request.app.state.mcp_runtime.qs)


Quotes = Annotated[MemoQuoteSource, Depends(get_quotes)]


@asynccontextmanager
async def _transaction(session: AsyncSession, redis) -> AsyncIterator[None]:
    """Commit and dispatch the post-commit effects; roll back and drop them on error."""
    try:
        yield
    except BaseException:
        await session.rollback()
        discard_post_commit(session)
        raise
    await session.commit()
    await dispatch_post_commit(session, redis)


# --- connector secrets ------------------------------------------------------------------

MASK = "••••"
# a config key containing one of these (case-insensitive) holds a secret
SECRET_HINTS = ("key", "secret", "token", "pem", "password")


def is_secret(key: str) -> bool:
    return any(h in key.lower() for h in SECRET_HINTS)


def mask_value(value: Any) -> str:
    """'••••' + the last 4 characters ('••••' alone when the value has 4 or fewer)."""
    text = str(value)
    return MASK + text[-4:] if len(text) > 4 else MASK


def _mask(value: Any, key: str | None) -> Any:
    """Masks secret leaves at any depth; a secret key passes down into its lists."""
    if isinstance(value, dict):
        return {k: _mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, key) for v in value]
    if key is not None and is_secret(key) and value not in (None, ""):
        return mask_value(value)
    return value


def mask_config(config: dict[str, Any]) -> dict[str, Any]:
    return {k: _mask(v, k) for k, v in config.items()}


def secret_values(value: Any, key: str | None = None) -> list[str]:
    """Every secret leaf of a config, as text (longest first, for redaction)."""
    if isinstance(value, dict):
        found = [x for k, v in value.items() for x in secret_values(v, k)]
    elif isinstance(value, list):
        found = [x for v in value for x in secret_values(v, key)]
    elif key is not None and is_secret(key) and value not in (None, ""):
        found = [str(value)]
    else:
        found = []
    return sorted(set(found), key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        text = text.replace(s, MASK)
    return text


def _masked_invalid(key: str) -> SMTError:
    return _invalid("config", f"valore mascherato non valido per {key}")


def _merge(new: Any, stored: Any, key: str | None) -> Any:
    """`new` with masked secrets replaced by the stored value; omitted secret keys keep the
    stored value, an explicit null removes a secret. Recurses into dicts and lists."""
    if isinstance(new, dict):
        old = stored if isinstance(stored, dict) else {}
        out: dict[str, Any] = {}
        for k, v in old.items():
            if k not in new and is_secret(k):
                out[k] = v
        for k, v in new.items():
            if v is None and is_secret(k):
                continue
            out[k] = _merge(v, old.get(k), k)
        return out
    if isinstance(new, list):
        old = stored if isinstance(stored, list) else []
        return [_merge(v, old[i] if i < len(old) else None, key) for i, v in enumerate(new)]
    if key is not None and is_secret(key) and isinstance(new, str) and new.startswith(MASK):
        if stored in (None, "") or new != mask_value(stored):
            raise _masked_invalid(key)
        return stored
    return new


def merge_config(new: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    return _merge(new, stored, None)
