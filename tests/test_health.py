"""Probes. These are what an orchestrator reads, so their contract is as real
as the product API's."""


def test_liveness_is_cheap_and_unconditional(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_reports_the_loaded_model(client):
    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"model_loaded": True, "accepting_traffic": True}
    assert len(body["model_version"]) == 12


def test_readiness_turns_503_once_shutdown_starts(client):
    """A draining instance must fail readiness so the load balancer stops
    sending it work — while still answering liveness, so it is not killed
    before in-flight requests finish."""
    client.app.state.shutting_down = True
    try:
        response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert client.get("/healthz").status_code == 200
    finally:
        client.app.state.shutting_down = False


def test_health_reports_version_and_uptime(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_name"] == "dock"
    assert body["version"]
    assert body["uptime_seconds"] >= 0


def test_probes_are_not_in_the_public_schema(client):
    """Operational endpoints are not part of the versioned product API."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/healthz" not in paths
    assert "/metrics" not in paths
    assert "/api/v1/score" in paths
