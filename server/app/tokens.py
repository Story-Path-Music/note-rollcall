"""Handoff token verification (CONTRACT section 3)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from . import clock

ISSUER = "storypathmusic.com"


class TokenError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in s):
        raise ValueError("bad base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(secret: str, payload: dict) -> str:
    """Mint a token (used by tests and dev tooling)."""
    p = b64url_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(secret.encode("utf-8"), p.encode("ascii"), hashlib.sha256).digest()
    return p + "." + b64url_encode(mac)


def verify(secret: str, token: str) -> dict:
    """Verify signature, expiry, claims. Nonce replay is checked by the caller (store)."""
    if not secret:
        raise TokenError("not_configured")
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 4096:
        raise TokenError("malformed")
    p, s = token.split(".")
    try:
        sig = b64url_decode(s)
    except ValueError:
        raise TokenError("malformed")
    expect = hmac.new(secret.encode("utf-8"), p.encode("ascii", "ignore"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        raise TokenError("bad_signature")
    try:
        claims = json.loads(b64url_decode(p).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise TokenError("malformed")
    if not isinstance(claims, dict) or claims.get("iss") != ISSUER:
        raise TokenError("bad_issuer")
    now = clock.now().timestamp()
    try:
        exp = float(claims["exp"])
        iat = float(claims.get("iat", now))
        sub = int(claims["sub"])
        quota = int(claims.get("quota", 5))
    except (KeyError, TypeError, ValueError):
        raise TokenError("bad_claims")
    if exp <= now:
        raise TokenError("expired")
    if iat > now + 120:
        raise TokenError("bad_claims")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not (8 <= len(nonce) <= 128):
        raise TokenError("bad_claims")
    if sub <= 0:
        raise TokenError("bad_claims")
    return {
        "sub": sub,
        "name": str(claims.get("name") or f"user{sub}")[:100],
        "quota": max(0, min(1000, quota)),
        "nonce": nonce,
        "exp": exp,
    }
