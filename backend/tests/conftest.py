"""
Shared pytest configuration.

Sets a deterministic session-token Fernet key before any backend module
instantiates settings, so token tests are independent of import order
(pydantic-settings reads environment variables at instantiation time).
"""

import base64
import os

os.environ["SESSION_TOKEN_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0" * 32
).decode("ascii")
