import hashlib
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from smtrading.common.errors import ErrorCode, SMTError

_NONCE_LEN = 12


def _key(master_key: str) -> bytes:
    return hashlib.sha256(master_key.encode()).digest()


def encrypt_json(data: dict, master_key: str) -> bytes:
    nonce = os.urandom(_NONCE_LEN)
    plain = json.dumps(data, sort_keys=True).encode("utf-8")
    return nonce + AESGCM(_key(master_key)).encrypt(nonce, plain, None)


def decrypt_json(blob: bytes, master_key: str) -> dict:
    if len(blob) <= _NONCE_LEN:
        raise SMTError(ErrorCode.FORBIDDEN, "invalid master key")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    try:
        plain = AESGCM(_key(master_key)).decrypt(nonce, ct, None)
    except InvalidTag:
        raise SMTError(ErrorCode.FORBIDDEN, "invalid master key") from None
    return json.loads(plain.decode("utf-8"))
