"""Process-wide logging setup: no provider key, token or credential reaches the logs.

httpx logs every request URL at INFO and some providers (Finnhub `token=`, Twelve Data
`apikey=`) put the key in the query string, so the httpx/httpcore loggers are raised to
WARNING and every root handler gets a filter that masks secret-looking values.
"""

import logging
import re

_QUIET_LOGGERS = ("httpx", "httpcore")
_NAMES = (
    r"access_token|refresh_token|id_token|client_secret|private_key|api_key|apikey|"
    r"token|secret|password|key"
)
# name=value in queries and key=value texts
_SECRET_PARAM = re.compile(rf"(?i)(?<![A-Za-z0-9_])({_NAMES})=[^&\s'\",;]+")
# "name": "value" (JSON) and 'name': 'value' (Python reprs)
_SECRET_JSON = re.compile(rf"""(?i)(["'])({_NAMES})\1(\s*:\s*)(["'])[^"']*\4""")
# header lines: Authorization: [scheme] value, X-...-Token / X-API-Key: value
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)(?:(basic|bearer|token)\s+)?[^\s,;'\"]+"
)
_TOKEN_HEADER = re.compile(r"(?i)\b(x-[a-z0-9-]*(?:token|key|secret))(\s*:\s*)[^\s,;'\"]+")
_AUTH_SCHEME = re.compile(r"(?i)\b(Basic|Bearer)\s+(?!<redacted>)[A-Za-z0-9+/=._~-]+")
# Telegram bot API: /bot<id>:<token>/method
_TELEGRAM = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
# a bare Telegram bot token (<bot id>:<35-char secret>) anywhere else in a text
_TELEGRAM_TOKEN = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


def _auth(m: re.Match) -> str:
    scheme = f"{m.group(3)} " if m.group(3) else ""
    return f"{m.group(1)}{m.group(2)}{scheme}<redacted>"


def redact_log_text(text: str) -> str:
    text = _TELEGRAM.sub("/bot<redacted>", text)
    text = _TELEGRAM_TOKEN.sub("<redacted>", text)
    text = _SECRET_JSON.sub(r"\1\2\1\3\4<redacted>\4", text)
    text = _AUTH_HEADER.sub(_auth, text)
    text = _TOKEN_HEADER.sub(r"\1\2<redacted>", text)
    text = _AUTH_SCHEME.sub(r"\1 <redacted>", text)
    return _SECRET_PARAM.sub(r"\1=<redacted>", text)


class RedactingFilter(logging.Filter):
    """Rewrites the record's message (and traceback text) with secrets masked."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Never raises into the caller: a record that cannot be rendered is replaced by a
        safe generic line (nothing of it, secrets included, is written)."""
        try:
            message = record.getMessage()
            redacted = redact_log_text(message)
            if redacted != message:
                record.msg, record.args = redacted, None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact_log_text(record.exc_text)
        except Exception as e:  # noqa: BLE001 - logging must not break the caller
            record.msg = (f"[log redaction failed: {type(e).__name__}] "
                          f"record of {record.name} at {record.pathname}:{record.lineno}")
            record.args = None
            record.exc_info = None
            record.exc_text = None
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
