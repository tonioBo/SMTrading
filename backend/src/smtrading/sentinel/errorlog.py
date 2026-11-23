"""Error log rate for the sentinel: a logging handler in the api and the worker counts the
ERROR and CRITICAL records per logger and minute; a flusher writes the counts to Redis
every FLUSH_S (`errlog:<epoch minute>`: field `<process>|<logger>` → count;
`errlog:msg:<epoch minute>`: the last message, secrets redacted), kept TTL_S. The sentinel
reads the last WINDOW_S.

Known noise is not counted: the yfinance loggers and the providers' quota/rate-limit
records (a 429 or an exhausted quota is handled by the router: «Quota delle fonti»).

`emit` never raises nor logs (a logging handler must not break the caller or recurse);
the flusher swallows every error (a Redis outage loses at most the counts of the outage)."""

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from smtrading.common.logsetup import redact_log_text

WINDOW_S = 15 * 60
FLUSH_S = 30
TTL_S = WINDOW_S + 10 * 60
KEY = "errlog:{minute}"
MSG_KEY = "errlog:msg:{minute}"
MAX_MESSAGE = 200
EXCLUDED_LOGGERS = ("yfinance", "peewee")
EXCLUDED_MESSAGE = re.compile(r"(?i)\bquota\b|\b429\b|rate.?limit|too many requests")


def excluded(record: logging.LogRecord, message: str) -> bool:
    name = record.name or ""
    if any(name == n or name.startswith(n + ".") for n in EXCLUDED_LOGGERS):
        return True
    return EXCLUDED_MESSAGE.search(message) is not None


class ErrorCounter(logging.Handler):
    def __init__(self, process: str, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(level=logging.ERROR)
        self.process = process
        self._clock = clock
        self._mutex = threading.Lock()
        self._counts: dict[tuple[int, str], int] = {}
        self._last: dict[tuple[int, str], str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if excluded(record, message):
                return
            if record.exc_info and record.exc_info[1] is not None:
                message = f"{message}: {type(record.exc_info[1]).__name__}"
            key = (int(record.created // 60), record.name or "root")
            text = redact_log_text(" ".join(message.split()))[:MAX_MESSAGE]
            with self._mutex:
                self._counts[key] = self._counts.get(key, 0) + 1
                self._last[key] = text
        except Exception:  # noqa: BLE001, S110 - never from a log call
            pass

    def drain(self) -> tuple[dict[tuple[int, str], int], dict[tuple[int, str], str]]:
        with self._mutex:
            counts, last = self._counts, self._last
            self._counts, self._last = {}, {}
        return counts, last

    def put_back(self, counts: dict[tuple[int, str], int],
                 last: dict[tuple[int, str], str]) -> None:
        now_minute = int(self._clock() // 60)
        with self._mutex:
            for key, n in counts.items():
                if key[0] >= now_minute - WINDOW_S // 60:
                    self._counts[key] = self._counts.get(key, 0) + n
                    self._last.setdefault(key, last.get(key, ""))


async def flush(redis, counter: ErrorCounter) -> None:
    counts, last = counter.drain()
    if not counts:
        return
    try:
        for (minute, logger), n in counts.items():
            field = f"{counter.process}|{logger}"
            key, msg_key = KEY.format(minute=minute), MSG_KEY.format(minute=minute)
            await redis.hincrby(key, field, n)
            await redis.expire(key, TTL_S)
            if last.get((minute, logger)):
                await redis.hset(msg_key, field, last[(minute, logger)])
                await redis.expire(msg_key, TTL_S)
    except Exception:  # noqa: BLE001 - Redis down: tried again at the next flush
        counter.put_back(counts, last)


async def flusher(redis, counter: ErrorCounter, *, interval_s: float = FLUSH_S) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await flush(redis, counter)
        except Exception:  # noqa: BLE001, S110
            pass


def _text(v: Any) -> str:
    return v.decode(errors="replace") if isinstance(v, bytes) else str(v)


async def read_counts(redis, now: float, *, window_s: int = WINDOW_S
                      ) -> dict[tuple[str, str], dict[str, Any]]:
    """(process, logger) → {"count", "message"} over the last `window_s`; Redis errors
    propagate."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    last_minute = int(now // 60)
    for minute in range(last_minute - window_s // 60 + 1, last_minute + 1):
        counts = await redis.hgetall(KEY.format(minute=minute)) or {}
        messages = await redis.hgetall(MSG_KEY.format(minute=minute)) or {}
        messages = {_text(k): _text(v) for k, v in messages.items()}
        for field, n in counts.items():
            field = _text(field)
            process, _, logger = field.partition("|")
            entry = out.setdefault((process, logger), {"count": 0, "message": ""})
            try:
                entry["count"] += int(_text(n))
            except ValueError:
                continue
            if messages.get(field):
                entry["message"] = messages[field]
    return out


def install(process: str, *, extra_loggers: tuple[str, ...] = ()) -> ErrorCounter:
    """The process's counter on the root logger (and on `extra_loggers`, the
    non-propagating ones such as uvicorn's); idempotent. Call it after
    `configure_logging` (basicConfig adds its handler only to a root without one)."""
    root = logging.getLogger()
    counter = next((h for h in root.handlers if isinstance(h, ErrorCounter)), None)
    if counter is None:
        counter = ErrorCounter(process)
        root.addHandler(counter)
    for name in extra_loggers:
        logger = logging.getLogger(name)
        if counter not in logger.handlers:
            logger.addHandler(counter)
    return counter


def uninstall(counter: ErrorCounter, *, extra_loggers: tuple[str, ...] = ()) -> None:
    for logger in (logging.getLogger(), *(logging.getLogger(n) for n in extra_loggers)):
        if counter in logger.handlers:
            logger.removeHandler(counter)
