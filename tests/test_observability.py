"""Request correlation, metrics, and structured logs.

These are tested like features because that is what they are: if a request id
stops propagating, or a metric silently loses its labels, nobody notices until
the incident where they were needed."""

import json
import logging

from app.core import metrics
from app.core.logging import JsonFormatter, RequestContextFilter
from tests.conftest import NORMAL_EVENT, OUTLIER_EVENT


class TestRequestId:
    def test_every_response_carries_one(self, client):
        response = client.get("/api/v1/health")

        assert response.headers["X-Request-ID"]

    def test_an_inbound_id_is_preserved(self, client):
        """So a trace started at the gateway keeps one id across services."""
        response = client.get("/api/v1/health", headers={"X-Request-ID": "gateway-abc-123"})

        assert response.headers["X-Request-ID"] == "gateway-abc-123"

    def test_a_hostile_inbound_id_is_replaced(self, client):
        """The header is attacker-controlled and ends up in log lines and
        response headers, so it is sanitised rather than trusted."""
        response = client.get(
            "/api/v1/health", headers={"X-Request-ID": "evil\tvalue with spaces"}
        )

        assert response.headers["X-Request-ID"] != "evil\tvalue with spaces"

    def test_an_overlong_inbound_id_is_replaced(self, client):
        response = client.get("/api/v1/health", headers={"X-Request-ID": "a" * 500})

        assert len(response.headers["X-Request-ID"]) < 200

    def test_ids_differ_between_requests(self, client):
        first = client.get("/api/v1/health").headers["X-Request-ID"]
        second = client.get("/api/v1/health").headers["X-Request-ID"]

        assert first != second

    def test_response_time_header_is_present(self, client):
        response = client.get("/api/v1/health")

        assert float(response.headers["X-Response-Time-ms"]) >= 0


class TestMetrics:
    def test_scrape_endpoint_serves_prometheus_text(self, client):
        response = client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")

    def test_requests_are_counted(self, client):
        client.post("/api/v1/score", json=NORMAL_EVENT)
        body = client.get("/metrics").text

        assert 'dock_http_requests_total{method="POST",route="/api/v1/score",status="200"}' in body

    def test_model_decisions_are_counted_by_outcome(self, client):
        client.post("/api/v1/score", json=OUTLIER_EVENT)
        body = client.get("/metrics").text

        assert 'dock_model_decisions_total{outcome="anomalous"}' in body

    def test_build_info_exposes_the_model_version(self, client):
        body = client.get("/metrics").text
        model_version = client.get("/readyz").json()["model_version"]

        assert f'model_version="{model_version}"' in body

    def test_metrics_label_the_route_template_not_the_raw_path(self, client):
        """Labelling on the raw path would mint a new time series per URL,
        including every 404 a scanner generates. That is how a Prometheus
        instance falls over."""
        client.get("/api/v1/nope-1")
        client.get("/api/v1/nope-2")
        body = client.get("/metrics").text

        assert "nope-1" not in body
        assert "nope-2" not in body
        assert 'route="unmatched"' in body

    def test_in_flight_gauge_returns_to_zero(self, client):
        client.post("/api/v1/score", json=NORMAL_EVENT)

        assert metrics.http_requests_in_flight._value.get() == 0


class TestStructuredLogs:
    def test_each_line_is_json_carrying_the_request_context(self, client, caplog):
        """The context is stamped onto the record when it is handled, not read
        from a ContextVar when it is formatted — so it survives being rendered
        later, on another thread, or from a queue."""
        formatter = JsonFormatter()
        context_filter = RequestContextFilter()
        caplog.handler.addFilter(context_filter)
        try:
            with caplog.at_level(logging.INFO):
                response = client.post(
                    "/api/v1/score", json=NORMAL_EVENT, headers={"X-Request-ID": "log-test-1"}
                )
        finally:
            caplog.handler.removeFilter(context_filter)

        assert response.status_code == 200
        rendered = [
            json.loads(formatter.format(record))
            for record in caplog.records
            if record.name.startswith("app.")
        ]

        assert rendered, "expected the request to produce log records"
        assert all(line["request_id"] == "log-test-1" for line in rendered)
        assert any(line["message"] == "scored event" for line in rendered)
        assert all(line["timestamp"].endswith("+00:00") for line in rendered)

    def test_the_access_log_records_route_status_and_duration(self, client, caplog):
        with caplog.at_level(logging.INFO):
            client.post("/api/v1/score", json=NORMAL_EVENT)

        access_lines = [r for r in caplog.records if r.getMessage() == "request completed"]

        assert access_lines
        record = access_lines[-1]
        assert record.route == "/api/v1/score"
        assert record.status == 200
        assert record.duration_ms >= 0

    def test_probes_do_not_fill_the_access_log(self, client, caplog):
        with caplog.at_level(logging.INFO):
            client.get("/healthz")
            client.get("/metrics")

        assert not [r for r in caplog.records if r.getMessage() == "request completed"]
