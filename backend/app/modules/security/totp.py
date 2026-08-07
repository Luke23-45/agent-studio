"""
RFC 6238 TOTP (P7-4 MFA) — dependency-free implementation.

SHA-1 TOTP with a 30s period and 6 digits (the de-facto standard used by
Google Authenticator / Authy / 1Password). ``verify_totp`` accepts codes
within +/-1 step to absorb clock skew.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time

_PERIOD_SECONDS = 30
_DIGITS = 6


def generate_secret() -> str:
    """Base32-encoded 20-byte TOTP secret (RFC 4648, no padding)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key_from_b32(secret_b32: str) -> bytes:
    secret_b32 = secret_b32.strip().upper()
    pad = "=" * (-len(secret_b32) % 8)
    return base64.b32decode(secret_b32 + pad)


def _hotp(key: bytes, counter: int, digits: int = _DIGITS) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (
        10**digits
    )
    return str(code).zfill(digits)


def totp_code(
    secret_b32: str, *, drift: int = 0, now: float | None = None, digits: int = _DIGITS
) -> str:
    """Current TOTP code; ``drift`` shifts the counter by that many steps."""
    counter = int((now if now is not None else time.time()) / _PERIOD_SECONDS) + drift
    return _hotp(_key_from_b32(secret_b32), counter, digits=digits)


def verify_totp(secret_b32: str, code: str, *, window: int = 1) -> bool:
    """Accept a code within +/-``window`` steps of the current one."""
    code = code.strip()
    if not code.isdigit() or len(code) != _DIGITS:
        return False
    for drift in range(-window, window + 1):
        if hmac.compare_digest(totp_code(secret_b32, drift=drift), code):
            return True
    return False


def otpauth_uri(secret_b32: str, account: str, issuer: str) -> str:
    """otpauth:// provisioning URI for authenticator apps."""
    from urllib.parse import quote

    issuer_q = quote(issuer, safe="")
    label = f"{issuer_q}:{quote(account, safe='')}"
    return (
        f"otpauth://totp/{label}?secret={secret_b32}"
        f"&issuer={issuer_q}&algorithm=SHA1&digits=6&period={_PERIOD_SECONDS}"
    )
