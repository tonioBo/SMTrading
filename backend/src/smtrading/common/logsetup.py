"""Process-wide logging setup: no provider key, token or credential reaches the logs.

httpx logs every request URL at INFO and some providers (Finnhub `token=`, Twelve Data
`apikey=`) put the key in the query string, so the httpx/httpcore loggers are raised to
WARNING and every root handler gets a filter that masks secret-looking values.
"""

import logging
import re

_QUIET_LOGGERS = ("httpx", "httpcore")
_SECRET_PARAM = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(token|apikey|api_key|key|secret|password)=[^&\s'\"]+"
)
_AUTH_SCHEME = re.compile(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9+/=._~-]+")


def redact_log_text(text: str) -> str:
    return _AUTH_SCHEME.sub(r"\1 <redacted>", _SECRET_PARAM.sub(r"\1=<redacted>", text))


class RedactingFilter(logging.Filter):
    """Rewrites the record's message (and traceback text) with secrets masked."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):  # malformed %-args: let logging report it
            return True
        redacted = redact_log_text(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_log_text(record.exc_text)
        return True


def _install_filter(handler: logging.Handler) -> None:
    if not any(isinstance(f, RedactingFilter) for f in handler.filters):
        handler.addFilter(RedactingFilter())


def configure_logging(level: int = logging.INFO, *, add_handler: bool = True,
                      extra_loggers: tuple[str, ...] = ()) -> None:
    """Idempotent. `add_handler=False` keeps a process's own handlers (uvicorn, CLI output)
    and only installs the filter on the existing ones; `extra_loggers` are non-propagating
    loggers whose handlers also get the filter (e.g. uvicorn's)."""
    root = logging.getLogger()
    if add_handler and not root.handlers:
        logging.basicConfig(level=level)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for logger in (root, *(logging.getLogger(n) for n in extra_loggers)):
        for handler in logger.handlers:
            _install_filter(handler)
