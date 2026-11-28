"""Code pairing of a TV page (like the TV apps of the video services): for a TV that does
not open a URL sent to it, the page at `/tv` with no session asks for a code and shows it;
the user writes it in the portal («Abbina con codice»), which binds it to their own
display link; the page, polling with its own opaque id, then gets the display cookie.

Redis only (all keys expire):

    tv:pair:poll:<poll id>   {"state": "pending"} → {"state": "claimed", "user_id": …}
                             (TTL CODE_TTL_S; read once when claimed: the cookie is
                             handed out a single time)
    tv:pair:code:<code>      the poll id (TTL CODE_TTL_S; GETDEL on a claim: single use)
    tv:pair:rl:ip:<ip>       codes asked from one address (CREATE_LIMIT per window)
    tv:pair:rl:user:<id>     claims tried by one user, wrong or right (CLAIM_LIMIT)

The code is 6 digits from `secrets` and opens nothing alone: a claim needs a portal
session, and only the page holding the poll id (never shown) receives the cookie. A code
is never logged, audited or returned in an error."""

import json
import re
import secrets
import uuid

from redis.asyncio import Redis

from smtrading.admin.common import _invalid
from smtrading.common.errors import ErrorCode, SMTError

CODE_DIGITS = 6
CODE_TTL_S = 600
CREATE_LIMIT, CREATE_WINDOW_S = 10, 600
CLAIM_LIMIT, CLAIM_WINDOW_S = 10, 600
POLL_S = 2  # how often the page asks whether its code was used

INVALID = "codice non valido o scaduto"
TOO_MANY_CODES = "troppi codici richiesti da questo indirizzo: riprova tra qualche minuto"
TOO_MANY_CLAIMS = "troppi tentativi di abbinamento: riprova tra qualche minuto"

_POLL_ID = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _poll_key(poll_id: str) -> str:
    return f"tv:pair:poll:{poll_id}"


def _code_key(code: str) -> str:
    return f"tv:pair:code:{code}"


async def _count(redis: Redis, key: str, window_s: int) -> int:
    """Count one attempt (the window starts at the first one)."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_s, nx=True)
        count, _ = await pipe.execute()
    return int(count)


def _text(raw) -> str | None:
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


async def create(redis: Redis, client_ip: str | None) -> dict:
    """A new code for the TV page: {poll_id, code, expires_in_s, poll_s}."""
    if await _count(redis, f"tv:pair:rl:ip:{client_ip or '-'}", CREATE_WINDOW_S) > CREATE_LIMIT:
        raise SMTError(ErrorCode.RATE_LIMITED, TOO_MANY_CODES)
    poll_id = secrets.token_urlsafe(24)
    await redis.set(_poll_key(poll_id), json.dumps({"state": "pending"}), ex=CODE_TTL_S)
    for _ in range(20):
        code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        if await redis.set(_code_key(code), poll_id, nx=True, ex=CODE_TTL_S):
            break
    else:  # a million codes in use at once: not a real situation
        await redis.delete(_poll_key(poll_id))
        raise SMTError(ErrorCode.RATE_LIMITED, TOO_MANY_CODES)
    return {"poll_id": poll_id, "code": code, "expires_in_s": CODE_TTL_S, "poll_s": POLL_S}


def clean_code(raw) -> str | None:
    """The 6 digits of what the user wrote (spaces and dashes ignored), else None."""
    text = re.sub(r"[\s-]", "", str(raw or ""))
    return text if len(text) == CODE_DIGITS and text.isascii() and text.isdigit() else None


async def claim(redis: Redis, user_id: uuid.UUID, raw_code) -> None:
    """Bind a code to `user_id` (the caller's own link, chosen by the route). Every attempt
    counts first; a wrong, used or expired code is the same generic error."""
    if await _count(redis, f"tv:pair:rl:user:{user_id}", CLAIM_WINDOW_S) > CLAIM_LIMIT:
        raise SMTError(ErrorCode.RATE_LIMITED, TOO_MANY_CLAIMS)
    code = clean_code(raw_code)
    if code is None:
        raise _invalid("code", INVALID)
    poll_id = _text(await redis.getdel(_code_key(code)))  # single use, atomically
    if poll_id is None:
        raise _invalid("code", INVALID)
    done = await redis.set(_poll_key(poll_id),
                           json.dumps({"state": "claimed", "user_id": str(user_id)}),
                           xx=True, keepttl=True)
    if not done:
        raise _invalid("code", INVALID)


async def poll(redis: Redis, poll_id: str) -> tuple[str, uuid.UUID | None]:
    """(pending | claimed | expired, the claiming user). A claimed code is answered
    `claimed` once (GETDEL): any later poll is `expired`."""
    if not _POLL_ID.match(poll_id or ""):
        return "expired", None
    raw = _text(await redis.get(_poll_key(poll_id)))
    if raw is None:
        return "expired", None
    try:
        data = json.loads(raw)
    except ValueError:
        return "expired", None
    if data.get("state") != "claimed":
        return "pending", None
    raw = _text(await redis.getdel(_poll_key(poll_id)))
    try:
        user_id = uuid.UUID(json.loads(raw)["user_id"]) if raw else None
    except (ValueError, KeyError, TypeError):
        user_id = None
    return ("claimed", user_id) if user_id else ("expired", None)
