import hashlib
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from smtrading.common.errors import ErrorCode, SMTError

_NONCE_LEN = 12


def _key(master_key: str) -> bytes:
    return hashlib.sha256(master_key.encode()).digest()


def encrypt_json(data: dict, master_key: str, *, aad: bytes | None = None) -> bytes:
    """`aad` binds the blob to a domain (e.g. b"broker_live"): it must be passed again to
    decrypt, so a blob cannot be read in another context."""
    nonce = os.urandom(_NONCE_LEN)
    plain = json.dumps(data, sort_keys=True).encode("utf-8")
    return nonce + AESGCM(_key(master_key)).encrypt(nonce, plain, aad)


def decrypt_json(blob: bytes, master_key: str, *, aad: bytes | None = None) -> dict:
    if len(blob) <= _NONCE_LEN:
        raise SMTError(ErrorCode.FORBIDDEN, "invalid master key")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    try:
        plain = AESGCM(_key(master_key)).decrypt(nonce, ct, aad)
    except InvalidTag:
        raise SMTError(ErrorCode.FORBIDDEN, "invalid master key") from None
    return json.loads(plain.decode("utf-8"))
