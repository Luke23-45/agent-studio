"""
P7-4 — RFC 6238 TOTP unit tests (backend/app/modules/security/totp.py).

Validates the implementation against the RFC 6238 SHA-1 test vectors and
the window/format guarantees the MFA flow depends on.
"""

import base64

from backend.app.modules.security.totp import (
    generate_secret,
    otpauth_uri,
    totp_code,
    verify_totp,
)

# RFC 6238 appendix B: SHA-1 TOTP, secret = ASCII "12345678901234567890"
# Vectors give TIME IN SECONDS (T = floor(time/30)).
_RFC_SECRET_B32 = base64.b32encode(b"12345678901234567890").decode("ascii")
_RFC_VECTORS = [  # (time seconds, expected 8-digit code)
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
]


class TestTotp:
    def test_generate_secret_is_base32_20_bytes(self):
        for _ in range(10):
            secret = generate_secret()
            assert len(secret) == 32
            assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in secret)
            assert base64.b32decode(secret + "=" * (-len(secret) % 8))

    def test_rfc6238_sha1_vectors(self):
        for time_seconds, expected in _RFC_VECTORS:
            code = totp_code(_RFC_SECRET_B32, now=float(time_seconds), digits=8)
            assert code == expected, (time_seconds, code, expected)

    def test_code_is_six_digits_by_default(self):
        code = totp_code(_RFC_SECRET_B32)
        assert len(code) == 6 and code.isdigit()

    def test_verify_accepts_within_one_step(self, monkeypatch):
        now = 1_700_000_000.0
        monkeypatch.setattr("backend.app.modules.security.totp.time.time", lambda: now)
        assert verify_totp(_RFC_SECRET_B32, totp_code(_RFC_SECRET_B32, now=now))
        assert verify_totp(
            _RFC_SECRET_B32, totp_code(_RFC_SECRET_B32, now=now, drift=1)
        )
        assert verify_totp(
            _RFC_SECRET_B32, totp_code(_RFC_SECRET_B32, now=now, drift=-1)
        )

    def test_verify_rejects_out_of_window(self, monkeypatch):
        now = 1_700_000_000.0
        monkeypatch.setattr("backend.app.modules.security.totp.time.time", lambda: now)
        assert not verify_totp(
            _RFC_SECRET_B32, totp_code(_RFC_SECRET_B32, now=now, drift=5)
        )
        assert not verify_totp(_RFC_SECRET_B32, "123456")
        assert not verify_totp(_RFC_SECRET_B32, "abcdef")
        assert not verify_totp(_RFC_SECRET_B32, "12345")
        assert not verify_totp(_RFC_SECRET_B32, "1234567")

    def test_verify_is_secret_specific(self, monkeypatch):
        other = generate_secret()
        now = 1_700_000_000.0
        monkeypatch.setattr("backend.app.modules.security.totp.time.time", lambda: now)
        assert not verify_totp(other, totp_code(_RFC_SECRET_B32, now=now))

    def test_otpauth_uri_shape(self):
        uri = otpauth_uri("ABC123", "alice (key-00000001)", "Neryva Agent Studio")
        assert uri.startswith("otpauth://totp/")
        assert "Neryva%20Agent%20Studio" in uri
        assert "alice%20%28key-00000001%29" in uri
        assert "secret=ABC123" in uri
        assert "issuer=Neryva%20Agent%20Studio" in uri
        assert "algorithm=SHA1" in uri
        assert "digits=6" in uri
        assert "period=30" in uri
