"""Prometheus instrumentation.

Three rules shape what is recorded here:

1. **Label on the route template, never the raw path.** `/api/v1/score` is one
   time series; a raw path containing an id would be one series per id, which
   is the classic way to take down a Prometheus instance.
2. **Histograms, not averages.** An average latency hides the tail, and the
   tail is what users feel. The buckets below are chosen around this service's
   actual profile (single-digit milliseconds) rather than the library default,
   which starts at 5ms and would put almost every request in one bucket.
3. **Model behaviour is a first-class signal.** Request rate and latency say
   the service is up; the score distribution and anomaly rate say the model is
   still doing what it did last week. Silent model drift is invisible to
   ordinary HTTP metrics.
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)

# Buckets in seconds, tuned for an in-process model call: most requests land
# between 0.5ms and 10ms, so the resolution lives there.
_LATENCY_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)

http_requests_total = Counter(
    "dock_http_requests_total",
    "HTTP requests by route template, method and status class.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "dock_http_request_duration_seconds",
    "End-to-end request handling time, including serialisation.",
    labelnames=("method", "route"),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

http_requests_in_flight = Gauge(
    "dock_http_requests_in_flight",
    "Requests currently being handled. Also drives graceful shutdown draining.",
    registry=REGISTRY,
)

model_inference_duration_seconds = Histogram(
    "dock_model_inference_duration_seconds",
    "Time inside the model call only, excluding request parsing and serialisation.",
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

model_score = Histogram(
    "dock_model_anomaly_score",
    "Distribution of returned anomaly scores. A shift here is model drift.",
    buckets=(-0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5),
    registry=REGISTRY,
)

model_decisions_total = Counter(
    "dock_model_decisions_total",
    "Scoring decisions by outcome.",
    labelnames=("outcome",),
    registry=REGISTRY,
)

request_timeouts_total = Counter(
    "dock_request_timeouts_total",
    "Requests abandoned at the server deadline. A rising rate means threadpool "
    "slots are being held by work nobody is waiting for any more.",
    labelnames=("route",),
    registry=REGISTRY,
)

rate_limit_rejections_total = Counter(
    "dock_rate_limit_rejections_total",
    "Requests rejected by the rate limiter.",
    registry=REGISTRY,
)

idempotent_replays_total = Counter(
    "dock_idempotent_replays_total",
    "Responses served from the idempotency store instead of being recomputed.",
    registry=REGISTRY,
)

build_info = Gauge(
    "dock_build_info",
    "Build and model identity. Always 1; the labels carry the information.",
    labelnames=("version", "model_version", "environment"),
    registry=REGISTRY,
)


def record_inference(duration_seconds: float, score: float, is_anomalous: bool) -> None:
    model_inference_duration_seconds.observe(duration_seconds)
    model_score.observe(score)
    model_decisions_total.labels(outcome="anomalous" if is_anomalous else "normal").inc()


def render() -> tuple[bytes, str]:
    """The `/metrics` payload and its content type.

    `generate_latest` emits the Prometheus text exposition format, so the
    matching `CONTENT_TYPE_LATEST` is the one from the top-level package —
    pairing it with the OpenMetrics content type would advertise a format
    the body does not actually use (OpenMetrics requires an `# EOF`
    terminator that this payload has no reason to carry).
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
