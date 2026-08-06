"""HMAC-SHA256 webhook signing (matrix 1.7).

Signature header format (Stripe-style):

    X-Neryva-Signature: t=<unix_ts>,v1=<hex hmac over "ts.body">

``t`` makes the signature timestamped so receivers can enforce a replay
window; ``v1`` is the HMAC-SHA256 of ``f"{timestamp}.{body}"`` keyed with
the subscription secret.
"""

import hashlib
import hmac
import time


def sign_payload(secret: str, body: bytes, timestamp: int | None = None) -> tuple[int, str]:
    """Return ``(timestamp, hex_digest)`` for a payload body."""
    ts = int(timestamp if timestamp is not None else time.time())
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return ts, digest


def signature_header(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Build the ``X-Neryva-Signature`` header value."""
    ts, digest = sign_payload(secret, body, timestamp)
    return f"t={ts},v1={digest}"


def verify_signature(
    secret: str,
    body: bytes,
    header: str,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> bool:
    """Verify a signature header against the payload.

    Constant-time comparison; rejects headers with a stale timestamp.
    """
    try:
        parts = dict(pair.split("=", 1) for pair in header.split(","))
        ts = int(parts["t"])
        provided = parts["v1"]
    except (ValueError, KeyError, AttributeError):
        return False

    if abs(int(now if now is not None else time.time()) - ts) > tolerance_seconds:
        return False

    _, expected = sign_payload(secret, body, timestamp=ts)
    return hmac.compare_digest(expected, provided)
