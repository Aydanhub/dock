"""Property-based tests.

Example-based tests check the cases somebody thought of. These check
invariants that must hold for *every* valid input, which is where the
surprises live — a float that serialises to `Infinity`, an amount of exactly
zero, an hour at the boundary.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.anomaly_service import AnomalyService

# Bounds mirror the ones declared on ScoringRequest, so the strategy generates
# exactly the space the API accepts.
events = st.fixed_dictionaries(
    {
        "amount": st.floats(min_value=0, max_value=1_000_000_000, allow_nan=False, allow_infinity=False),
        "hour_of_day": st.integers(min_value=0, max_value=23),
        "merchant_risk_score": st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
        "is_new_device": st.booleans(),
    }
)

# One service for the whole module: training per example would make this take
# minutes instead of seconds, and the model is read-only during scoring.
_service = AnomalyService()

_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@_SETTINGS
@given(event=events)
def test_any_valid_event_produces_a_finite_score(event):
    """A NaN or infinite score would serialise to invalid JSON and break every
    client — the kind of failure that only shows up on real data."""
    result = _service.score(event)

    assert result.anomaly_score == result.anomaly_score  # not NaN
    assert abs(result.anomaly_score) < 1e6
    assert isinstance(result.is_anomalous, bool)
    assert result.latency_ms >= 0


@_SETTINGS
@given(event=events)
def test_scoring_is_deterministic(event):
    """The same event scored twice must give the same decision, or the eval
    harness is measuring noise instead of drift."""
    first = _service.score(event)
    second = _service.score(event)

    assert first.anomaly_score == second.anomaly_score
    assert first.is_anomalous == second.is_anomalous


@_SETTINGS
@given(event=events)
def test_batch_and_single_agree(event):
    """Batching is an optimisation. If it ever changes an answer, it is a bug."""
    single = _service.score(event)
    batched = _service.score_many([event])[0]

    assert batched.anomaly_score == single.anomaly_score
    assert batched.is_anomalous == single.is_anomalous


@settings(max_examples=50, deadline=None)
@given(batch=st.lists(events, min_size=1, max_size=20))
def test_batch_preserves_order(batch):
    """Results are positional. A reordering would silently attach each decision
    to the wrong event, which no status code would reveal."""
    batched = _service.score_many(batch)
    individually = [_service.score(event) for event in batch]

    assert len(batched) == len(batch)
    assert [r.anomaly_score for r in batched] == [r.anomaly_score for r in individually]


@_SETTINGS
@given(event=events)
def test_every_result_carries_a_unique_id_and_the_model_version(event):
    first = _service.score(event)
    second = _service.score(event)

    assert first.request_id != second.request_id
    assert first.model_version == second.model_version == _service.model_version
