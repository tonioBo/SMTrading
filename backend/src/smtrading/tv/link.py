"""A user's TV display link: `tv-display.<link id>.<signature>`, the signature an
HMAC-SHA256 of `tv-display.<link id>` with the link's own random secret (stored encrypted
with the master key, `aad=b"tv-link"`, on the user's `tv_displays` row). It opens that
user's TV screens only (scope `tv-display`) and lasts until revoked: a new link (new id and
secret) or a revocation makes every older one invalid at once, because each request is
checked against the stored link."""

import base64
import hashlib
import hmac
import secrets

from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import SMTError

SCOPE = "tv-display"
AAD = b"tv-link"
TOKEN_MAX = 200


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_link() -> tuple[str, bytes, str]:
    """(link id, its secret encrypted, the secret in clear)."""
    link_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return link_id, encrypt_json({"secret": secret}, get_settings().master_key, aad=AAD), secret


def decrypt_secret(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        secret = decrypt_json(blob, get_settings().master_key, aad=AAD).get("secret")
    except (SMTError, ValueError, TypeError, AttributeError):
        return None
    return secret if isinstance(secret, str) and secret else None


def sign(scope: str, link_id: str, secret: str) -> str:
    payload = f"{scope}.{link_id}"
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(mac)}"


def token_for(link_id: str | None, blob: bytes | None) -> str | None:
    secret = decrypt_secret(blob)
    return sign(SCOPE, link_id, secret) if secret and link_id else None


def link_id_of(token: str | None) -> str | None:
    """The link id a token claims (scope `tv-display` only), not yet verified."""
    if not token or not isinstance(token, str) or len(token) > TOKEN_MAX:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != SCOPE or not parts[1]:
        return None
    return parts[1]


def verify(token: str | None, link_id: str | None, blob: bytes | None) -> bool:
    """True only for a token of scope `tv-display` signed for the stored link."""
    claimed = link_id_of(token)
    if claimed is None or not link_id:
        return False
    if not hmac.compare_digest(claimed.encode(), link_id.encode()):
        return False
    secret = decrypt_secret(blob)
    if secret is None:
        return False
    return hmac.compare_digest(token.encode(), sign(SCOPE, link_id, secret).encode())
