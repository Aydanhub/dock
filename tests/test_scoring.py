from tests.conftest import NORMAL_EVENT, OUTLIER_EVENT


def test_normal_transaction_is_not_anomalous(client):
    response = client.post("/api/v1/score", json=NORMAL_EVENT)

    assert response.status_code == 200
    body = response.json()
    assert body["is_anomalous"] is False
    assert body["latency_ms"] > 0
    assert body["request_id"]
    assert body["model_version"]


def test_extreme_outlier_is_anomalous(client):
    response = client.post("/api/v1/score", json=OUTLIER_EVENT)

    assert response.status_code == 200
    assert response.json()["is_anomalous"] is True


def test_outlier_scores_higher_than_normal(client):
    """The API only promises a boolean, but the score behind it should still
    rank an extreme case above a routine one."""
    normal = client.post("/api/v1/score", json=NORMAL_EVENT).json()["anomaly_score"]
    outlier = client.post("/api/v1/score", json=OUTLIER_EVENT).json()["anomaly_score"]

    assert outlier > normal


def test_every_response_is_stamped_with_the_model_version(client):
    """Without this, a decision cannot be traced back to the model that made
    it — which is the difference between a reproducible answer and a guess."""
    single = client.post("/api/v1/score", json=NORMAL_EVENT).json()
    ready = client.get("/readyz").json()

    assert single["model_version"] == ready["model_version"]


class TestValidation:
    def test_negative_amount_is_rejected(self, client):
        response = client.post("/api/v1/score", json={**NORMAL_EVENT, "amount": -5})
        assert response.status_code == 422

    def test_hour_out_of_range_is_rejected(self, client):
        response = client.post("/api/v1/score", json={**NORMAL_EVENT, "hour_of_day": 24})
        assert response.status_code == 422

    def test_missing_field_is_rejected(self, client):
        payload = {k: v for k, v in NORMAL_EVENT.items() if k != "merchant_risk_score"}
        assert client.post("/api/v1/score", json=payload).status_code == 422

    def test_unknown_field_is_rejected(self, client):
        """A typo'd field name must fail loudly rather than being ignored —
        silently dropping `merchant_risk` instead of `merchant_risk_score`
        would score the event against a default and look successful."""
        response = client.post("/api/v1/score", json={**NORMAL_EVENT, "merchant_risk": 0.9})

        assert response.status_code == 422
        assert any("merchant_risk" in e["field"] for e in response.json()["errors"])


class TestBatch:
    def test_batch_scores_every_event(self, client):
        events = [NORMAL_EVENT, OUTLIER_EVENT, NORMAL_EVENT]
        response = client.post("/api/v1/score/batch", json={"events": events})

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 3
        assert body["anomalous_count"] == 1
        assert len(body["results"]) == 3

    def test_batch_agrees_with_single_scoring(self, client):
        """Batching is an optimisation, not a different model. If the two
        paths ever disagree, one of them is wrong."""
        single = client.post("/api/v1/score", json=OUTLIER_EVENT).json()
        batched = client.post("/api/v1/score/batch", json={"events": [OUTLIER_EVENT]}).json()

        assert batched["results"][0]["anomaly_score"] == single["anomaly_score"]
        assert batched["results"][0]["is_anomalous"] == single["is_anomalous"]

    def test_empty_batch_is_rejected(self, client):
        assert client.post("/api/v1/score/batch", json={"events": []}).status_code == 422

    def test_oversized_batch_is_rejected(self, make_client):
        test_client = make_client(MAX_BATCH_SIZE=2)
        response = test_client.post("/api/v1/score/batch", json={"events": [NORMAL_EVENT] * 3})

        assert response.status_code == 413
        body = response.json()
        assert body["max_batch_size"] == 2
        assert body["received"] == 3
