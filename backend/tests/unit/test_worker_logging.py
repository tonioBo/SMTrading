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


@pytest.mark.parametrize(("text", "secret"), [
    ("GET /cb?code=1&access_token=AT123&x=1", "AT123"),
    ("refresh_token=RT456", "RT456"),
    ("client_secret=CS789&id_token=IDT0", "CS789"),
    ("client_secret=CS789&id_token=IDT0", "IDT0"),
    ('body {"access_token": "AT123", "user": "u"}', "AT123"),
    ("{'password': 'pw1', 'user': 'u'}", "pw1"),
    ('{"client_secret":"CS9"}', "CS9"),
    ("headers X-Auth-Token: TK42 next", "TK42"),
    ("X-API-Key: K99", "K99"),
    ("Authorization: Bearer smt_abc_def", "smt_abc_def"),
    ("Authorization: tok-raw-1", "tok-raw-1"),
    ("POST https://api.telegram.org/bot123456:AAE-xyz_Q/sendMessage", "AAE-xyz_Q"),
])
def test_redaction_covers_oauth_json_headers_and_telegram(text, secret):
    out = redact_log_text(text)
    assert secret not in out, out


def test_log_redacts_telegram_token():
    secret = "AAH" + "x9_-Q" * 7  # 38 characters, like a real bot token secret
    for text in (f"https://api.telegram.org/bot123456789:{secret}/sendMessage",
                 f"token 123456789:{secret} rejected",
                 f"ConnectError('https://api.telegram.org/bot123456789:{secret}/getMe')"):
        out = redact_log_text(text)
        assert secret not in out, out
        assert "123456789:" not in out, out


def test_telegram_path_keeps_the_method():
    out = redact_log_text("https://api.telegram.org/bot123456:AAE-xyz/sendMessage")
    assert out == "https://api.telegram.org/bot<redacted>/sendMessage"


class _BadStr:
    def __str__(self):
        raise RuntimeError("boom")


@pytest.mark.parametrize("make", [
    lambda: logging.LogRecord("x", logging.WARNING, __file__, 1, "%(missing)s", ({"a": 1},), None),
    lambda: logging.LogRecord("x", logging.WARNING, __file__, 1, _BadStr(), None, None),
])
def test_filter_never_raises_and_neutralises_the_record(make):
    record = make()
    assert RedactingFilter().filter(record) is True
    assert record.getMessage().startswith("[log redaction failed")
    assert record.args is None


def test_api_logging_gets_a_redacting_root_handler(isolated_root):
    """N4: under uvicorn the root has no handler: one is added, with the filter."""
    from smtrading.api.main import create_app

    logging.getLogger().handlers = []
    try:
        create_app()
        root = logging.getLogger()
        assert root.handlers
        assert any(isinstance(f, RedactingFilter) for f in root.handlers[0].filters)
    finally:
        logging.getLogger().handlers = []
