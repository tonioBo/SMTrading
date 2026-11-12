import pytest

from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import ErrorCode, SMTError


def test_encrypt_roundtrip():
    assert decrypt_json(encrypt_json({"k": "v", "n": [1, 2]}, "m"), "m") == {"k": "v", "n": [1, 2]}


def test_wrong_key():
    blob = encrypt_json({"k": "v"}, "m")
    with pytest.raises(SMTError) as ei:
        decrypt_json(blob, "other")
    assert ei.value.code == ErrorCode.FORBIDDEN
    assert ei.value.message == "invalid master key"


def test_truncated_blob():
    with pytest.raises(SMTError) as ei:
        decrypt_json(b"short", "m")
    assert ei.value.code == ErrorCode.FORBIDDEN


def test_nonce_random():
    a, b = encrypt_json({"k": "v"}, "m"), encrypt_json({"k": "v"}, "m")
    assert a != b
    assert a[:12] != b[:12]
