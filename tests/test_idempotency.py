"""Idempotency-Key handling, as a unit and through the API."""

import pytest

from app.core.idempotency import (
    InFlightError,
    InMemoryIdempotencyStore,
    KeyReuseError,
    NullIdempotencyStore,
    body_fingerprint,
    idempotent,
)
from tests.conftest import NORMAL_EVENT, OUTLIER_EVENT


class TestFingerprint:
    def test_field_order_does_not_change_the_fingerprint(self):
        """Two JSON objects with the same fields in a different order are the
        same request — reading that as key reuse would reject honest retries."""
        assert body_fingerprint({"a": 1, "b": 2}) == body_fingerprint({"b": 2, "a": 1})

    def test_different_values_change_the_fingerprint(self):
        assert body_fingerprint({"a": 1}) != body_fingerprint({"a": 2})


class TestStore:
    def test_first_call_claims_the_key_and_returns_nothing_to_replay(self):
        store = InMemoryIdempotencyStore(ttl_seconds=60, max_entries=10)
        assert store.begin("k", body_fingerprint({"a": 1})) is None

    def test_completed_response_is_replayed(self):
        store = InMemoryIdempotencyStore(ttl_seconds=60, max_entries=10)
        fingerprint = body_fingerprint({"a": 1})
        store.begin("k", fingerprint)
        store.complete("k", {"decision": "ok"})

        assert store.begin("k", fingerprint) == {"decision": "ok"}

    def test_same_key_different_body_is_rejected(self):
        store = InMemoryIdempotencyStore(ttl_seconds=60, max_entries=10)
        store.begin("k", body_fingerprint({"a": 1}))
        store.complete("k", {"decision": "ok"})

        with pytest.raises(KeyReuseError):
            store.begin("k", body_fingerprint({"a": 2}))

    def test_a_second_concurrent_attempt_is_rejected(self):
        store = InMemoryIdempotencyStore(ttl_seconds=60, max_entries=10)
        fingerprint = body_fingerprint({"a": 1})
        store.begin("k", fingerprint)

        with pytest.raises(InFlightError):
            store.begin("k", fingerprint)

    def test_entries_expire(self):
        clock = [0.0]
        store = InMemoryIdempotencyStore(
            ttl_seconds=10, max_entries=10, time_source=lambda: clock[0]
        )
        fingerprint = body_fingerprint({"a": 1})
        store.begin("k", fingerprint)
        store.complete("k", {"decision": "ok"})

        clock[0] = 11.0

        assert store.begin("k", fingerprint) is None

    def test_the_store_is_bounded(self):
        """An unbounded response cache is a memory leak with extra steps."""
        store = InMemoryIdempotencyStore(ttl_seconds=3600, max_entries=5)
        for index in range(20):
            key = f"k{index}"
            store.begin(key, body_fingerprint({"i": index}))
            store.complete(key, {"i": index})

        assert len(store._entries) <= 5

    def test_a_failed_request_releases_its_key(self):
        """Otherwise the first failure would lock the key until its TTL
        expired, and the retry — the whole reason the key exists — would be
        rejected as in-flight."""
        store = InMemoryIdempotencyStore(ttl_seconds=60, max_entries=10)
        fingerprint = body_fingerprint({"a": 1})

        with pytest.raises(RuntimeError), idempotent(store, "k", {"a": 1}):
            raise RuntimeError("handler blew up")

        assert store.begin("k", fingerprint) is None

    def test_null_store_never_replays(self):
        store = NullIdempotencyStore()
        store.begin("k", "fp")
        store.complete("k", {"decision": "ok"})

        assert store.begin("k", "fp") is None


class TestThroughTheApi:
    def test_retrying_with_the_same_key_replays_the_first_decision(self, make_client):
        test_client = make_client()
        headers = {"Idempotency-Key": "order-4711"}

        first = test_client.post("/api/v1/score", json=NORMAL_EVENT, headers=headers)
        second = test_client.post("/api/v1/score", json=NORMAL_EVENT, headers=headers)

        assert first.status_code == second.status_code == 200
        assert first.json()["request_id"] == second.json()["request_id"]
        assert second.headers["Idempotency-Replayed"] == "true"

    def test_without_a_key_each_call_is_a_new_decision(self, make_client):
        test_client = make_client()

        first = test_client.post("/api/v1/score", json=NORMAL_EVENT)
        second = test_client.post("/api/v1/score", json=NORMAL_EVENT)

        assert first.json()["request_id"] != second.json()["request_id"]

    def test_reusing_a_key_for_a_different_body_is_a_422(self, make_client):
        test_client = make_client()
        headers = {"Idempotency-Key": "order-4712"}
        test_client.post("/api/v1/score", json=NORMAL_EVENT, headers=headers)

        response = test_client.post("/api/v1/score", json=OUTLIER_EVENT, headers=headers)

        assert response.status_code == 422
        assert "idempotency-key-reuse" in response.json()["type"]

    def test_keys_are_scoped_per_caller(self, make_client):
        """Two clients that both call their retry "order-1" must not read each
        other's decisions."""
        test_client = make_client(
            REQUIRE_API_KEY="true", API_KEYS="client-a-key-xx,client-b-key-xx"
        )
        headers = {"Idempotency-Key": "order-1"}

        first = test_client.post(
            "/api/v1/score",
            json=NORMAL_EVENT,
            headers={**headers, "X-API-Key": "client-a-key-xx"},
        )
        second = test_client.post(
            "/api/v1/score",
            json=NORMAL_EVENT,
            headers={**headers, "X-API-Key": "client-b-key-xx"},
        )

        assert first.json()["request_id"] != second.json()["request_id"]

    def test_idempotency_can_be_switched_off(self, make_client):
        test_client = make_client(IDEMPOTENCY_ENABLED="false")
        headers = {"Idempotency-Key": "order-4713"}

        first = test_client.post("/api/v1/score", json=NORMAL_EVENT, headers=headers)
        second = test_client.post("/api/v1/score", json=NORMAL_EVENT, headers=headers)

        assert first.json()["request_id"] != second.json()["request_id"]
