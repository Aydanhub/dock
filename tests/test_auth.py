"""API key authentication."""

from app.core.security import fingerprint
from tests.conftest import NORMAL_EVENT

KEY = "test-key-aaaaaaaaaaaa"
OTHER_KEY = "test-key-bbbbbbbbbbbb"


def _authed(make_client, **extra):
    return make_client(REQUIRE_API_KEY="true", API_KEYS=f"{KEY},{OTHER_KEY}", **extra)


def test_request_without_a_key_is_rejected(make_client):
    response = _authed(make_client).post("/api/v1/score", json=NORMAL_EVENT)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "X-API-Key"
    assert response.headers["content-type"].startswith("application/problem+json")


def test_request_with_a_wrong_key_is_rejected(make_client):
    test_client = _authed(make_client)
    response = test_client.post(
        "/api/v1/score", json=NORMAL_EVENT, headers={"X-API-Key": "not-a-real-key"}
    )

    assert response.status_code == 401


def test_request_with_a_valid_key_succeeds(make_client):
    test_client = _authed(make_client)
    response = test_client.post(
        "/api/v1/score", json=NORMAL_EVENT, headers={"X-API-Key": KEY}
    )

    assert response.status_code == 200


def test_probes_stay_open_when_auth_is_on(make_client):
    """A kubelet cannot present an API key. If probes required one, an
    authenticated deployment would never pass readiness."""
    test_client = _authed(make_client)

    assert test_client.get("/healthz").status_code == 200
    assert test_client.get("/readyz").status_code == 200
    assert test_client.get("/metrics").status_code == 200


def test_auth_is_off_by_default(client):
    """Local dev must work with no configuration at all."""
    assert client.post("/api/v1/score", json=NORMAL_EVENT).status_code == 200


def test_the_key_itself_never_appears_in_a_response(make_client):
    test_client = _authed(make_client)
    body = test_client.post(
        "/api/v1/score", json=NORMAL_EVENT, headers={"X-API-Key": "wrong-key"}
    ).text

    assert KEY not in body
    assert "wrong-key" not in body


def test_fingerprints_are_stable_and_not_reversible():
    assert fingerprint(KEY) == fingerprint(KEY)
    assert fingerprint(KEY) != fingerprint(OTHER_KEY)
    assert KEY not in fingerprint(KEY)
