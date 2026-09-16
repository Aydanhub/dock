"""Idempotency-Key support for non-GET endpoints.

A client that times out on a scoring call and retries should not get a second,
differently-numbered decision for the same event. With an `Idempotency-Key`
header the first response is stored and replayed for later retries of the same
key, following the semantics of the IETF `Idempotency-Key` draft:

- same key, same body  -> the stored response is replayed
- same key, different body -> 422, because the key has been reused for a
  different request and silently returning the old answer would be wrong
- same key, still in flight -> 409, so two concurrent retries cannot both run

The store is in-process and bounded. As with rate limiting, the Protocol is
the extension point: a Redis-backed store makes replay work across replicas.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

_PENDING = object()


def body_fingerprint(payload: Any) -> str:
    """A stable hash of a request body, used to detect key reuse.

    `sort_keys` matters: two JSON objects with the same fields in a different
    order are the same request, and must not be read as key reuse.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class _Entry:
    fingerprint: str
    expires_at: float
    response: Any = _PENDING

    @property
    def is_pending(self) -> bool:
        return self.response is _PENDING


class IdempotencyStore(Protocol):
    def begin(self, key: str, fingerprint: str) -> Any | None: ...
    def complete(self, key: str, response: Any) -> None: ...
    def abandon(self, key: str) -> None: ...


class KeyReuseError(Exception):
    """The key was seen before with a different request body."""


class InFlightError(Exception):
    """A request with this key is still being processed."""


class InMemoryIdempotencyStore:
    """Bounded, TTL'd, thread-safe store of completed responses."""

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int,
        *,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._now = time_source or time.monotonic

    def begin(self, key: str, fingerprint: str) -> Any | None:
        """Claims the key. Returns a stored response to replay, or None to proceed.

        Raises `KeyReuseError` if the key was used for a different body, and
        `InFlightError` if another request holds the key right now.
        """
        now = self._now()
        with self._lock:
            self._evict_expired(now)
            entry = self._entries.get(key)

            if entry is not None:
                if entry.fingerprint != fingerprint:
                    raise KeyReuseError(key)
                if entry.is_pending:
                    raise InFlightError(key)
                self._entries.move_to_end(key)
                return entry.response

            self._entries[key] = _Entry(fingerprint=fingerprint, expires_at=now + self._ttl)
            self._entries.move_to_end(key)
            self._enforce_capacity()
            return None

    def complete(self, key: str, response: Any) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.response = response
                entry.expires_at = self._now() + self._ttl

    def abandon(self, key: str) -> None:
        """Releases a claimed key whose request failed, so a retry can run."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.is_pending:
                del self._entries[key]

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, e in self._entries.items() if e.expires_at <= now]
        for key in expired:
            del self._entries[key]

    def _enforce_capacity(self) -> None:
        # OrderedDict in least-recently-used order, so the front is the coldest.
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


class NullIdempotencyStore:
    """No-op store used when the feature is switched off."""

    def begin(self, key: str, fingerprint: str) -> Any | None:
        return None

    def complete(self, key: str, response: Any) -> None:
        return None

    def abandon(self, key: str) -> None:
        return None


@dataclass
class _Slot:
    """Handle for one idempotent section: replay what was stored, or save a new result."""

    replay: Any | None
    _store: "IdempotencyStore | None" = None
    _key: str | None = None

    @property
    def is_replay(self) -> bool:
        return self.replay is not None

    def save(self, response: Any) -> None:
        if self._store is not None and self._key is not None:
            self._store.complete(self._key, response)


@contextmanager
def idempotent(store: IdempotencyStore, key: str | None, payload: Any) -> Iterator[_Slot]:
    """Scopes an idempotent operation.

    With no key the section runs normally, which keeps `Idempotency-Key`
    optional. With a key, a stored response is handed back for replay, and a
    failure inside the block releases the claim so a retry is not locked out
    by a request that never produced an answer.
    """
    if key is None:
        yield _Slot(replay=None)
        return

    stored = store.begin(key, body_fingerprint(payload))
    if stored is not None:
        yield _Slot(replay=stored)
        return

    try:
        yield _Slot(replay=None, _store=store, _key=key)
    except BaseException:
        store.abandon(key)
        raise
