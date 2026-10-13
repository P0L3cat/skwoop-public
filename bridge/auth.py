"""Pod and web-pod access checks. Tokens stay in the environment; this module only compares them."""

from __future__ import annotations

import hmac
from collections.abc import Collection

WEBPOD_PREFIX = "web-pod-"
LOOPBACK = frozenset({"127.0.0.1", "::1"})


def device_allowed(device_id: str, peer: str | None, allowed: Collection[str]) -> bool:
    """A web pod is only the local sidecar. Any other id must be on the allowlist when one is set."""
    dev = device_id.lower()
    if dev.startswith(WEBPOD_PREFIX) and len(dev) > len(WEBPOD_PREFIX):
        return peer in LOOPBACK

    return not allowed or dev in allowed


def bearer_matches(authorization: str, token: str) -> bool:
    """True when no token is configured, or Authorization is exactly Bearer <token>."""
    if not token:
        return True

    return hmac.compare_digest(authorization, f"Bearer {token}")


def access_key_ok(got: str, want: str) -> bool:
    """True when the web pod has no access key, or the hello key matches it."""
    if not want:
        return True

    return hmac.compare_digest(got, want)
