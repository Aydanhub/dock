"""API key authentication.

Keys are compared in constant time and never logged — callers are identified
in logs and metrics by a short, stable fingerprint of the key instead, which
is enough to attribute traffic and rate-limit per client without putting the
secret itself in a log aggregator.
"""

import hashlib
import hmac
import logging

from fastapi import Request
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings
from app.core.context import set_client_id
from app.core.errors import AuthenticationError

logger = logging.getLogger(__name__)

API_KEY_HEADER = "X-API-Key"

# auto_error=False so a missing key reaches our own handler and comes back as
# a problem+json body like every other error, instead of FastAPI's default.
api_key_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)

ANONYMOUS_CLIENT_ID = "anonymous"


def fingerprint(api_key: str) -> str:
    """A short, non-reversible id for a key, safe to log and label metrics with."""
    return "key_" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _match(candidate: str, settings: Settings) -> str | None:
    """Returns the fingerprint of the matching key, or None.

    Every configured key is checked with `compare_digest` and the loop is not
    short-circuited, so response time does not leak which key matched or how
    many are configured.
    """
    matched: str | None = None
    for known in settings.api_keys:
        if hmac.compare_digest(candidate, known):
            matched = fingerprint(known)
    return matched


def authenticate(request: Request) -> str:
    """Resolves the caller id for a request, raising if a required key is bad.

    Returns the client id used for logging, metrics labels and rate-limit
    bucketing. With auth switched off every caller shares a bucket keyed by
    client host, which is the right default for local dev and a deliberate
    one to override before production (see `Settings.validate_runtime`).
    """
    settings = get_settings()
    presented = request.headers.get(API_KEY_HEADER)

    if not settings.auth_active:
        client_id = request.client.host if request.client else ANONYMOUS_CLIENT_ID
        set_client_id(client_id)
        return client_id

    if not presented:
        raise AuthenticationError(
            f"Missing {API_KEY_HEADER} header.",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )

    matched = _match(presented, settings)
    if matched is None:
        logger.warning("rejected api key", extra={"key_fingerprint": fingerprint(presented)})
        raise AuthenticationError(
            "The provided API key is not valid.",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )

    set_client_id(matched)
    return matched
