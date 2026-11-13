import io
import logging

import httpx
import pytest
import respx

from smtrading.common.logsetup import RedactingFilter, configure_logging, redact_log_text


@pytest.fixture
def isolated_root():
    """A root logger with one fresh stream handler, restored afterwards."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


def test_redact_log_text_masks_secret_params_and_auth_headers():
    text = ("GET https://x/api?symbol=AAPL&token=abc&apikey=k1 password=pw "
            "Authorization: Basic dXNlcjpwYXNz Authorization: Bearer smt_live_123")
    out = redact_log_text(text)
    for secret in ("abc", "k1", "pw", "dXNlcjpwYXNz", "smt_live_123"):
        assert secret not in out
    assert "symbol=AAPL" in out


def test_record_with_token_query_is_emitted_redacted(isolated_root):
    configure_logging()
    logging.getLogger("smtrading.test").warning("fetch %s", "https://h/q?symbol=A&token=abc")
    out = isolated_root.getvalue()
    assert "token=abc" not in out
    assert "token=<redacted>" in out
    assert all(isinstance(f, RedactingFilter) for f in logging.getLogger().handlers[0].filters)


async def test_provider_keys_not_logged(isolated_root):
    configure_logging()
    with respx.mock:
        respx.get("https://finnhub.io/api/v1/quote").mock(return_value=httpx.Response(200, json={}))
        async with httpx.AsyncClient() as http:
            await http.get("https://finnhub.io/api/v1/quote",
                           params={"symbol": "AAPL", "token": "SECRET"})
    logging.getLogger("httpx").warning("echo %s", "https://finnhub.io/q?token=SECRET")
    assert "SECRET" not in isolated_root.getvalue()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_configure_logging_is_idempotent(isolated_root):
    configure_logging()
    configure_logging()
    assert len(logging.getLogger().handlers[0].filters) == 1
