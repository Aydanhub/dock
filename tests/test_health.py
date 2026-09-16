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


class TestMetricsEndpointToggle:
    """`METRICS_ENDPOINT_ENABLED` controls whether the scrape route is mounted.

    It is named for exposure rather than collection because that is all it
    does: the collectors are in-process counters and keep running either way.
    """

    def test_the_scrape_endpoint_is_mounted_by_default(self, client):
        assert client.get("/metrics").status_code == 200

    def test_it_can_be_taken_off_the_ingress(self, make_client):
        test_client = make_client(METRICS_ENDPOINT_ENABLED="false")

        assert test_client.get("/metrics").status_code == 404

    def test_probes_survive_the_metrics_endpoint_being_off(self, make_client):
        """Probes are not optional: without them an orchestrator cannot tell a
        starting instance from a broken one."""
        test_client = make_client(METRICS_ENDPOINT_ENABLED="false")

        assert test_client.get("/healthz").status_code == 200
        assert test_client.get("/readyz").status_code == 200
