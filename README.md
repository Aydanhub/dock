# Dock

A production-ready FastAPI template for AI-powered backend services — the
infrastructure you would otherwise rebuild from scratch on every new service:
authentication, rate limiting, idempotency, structured logs, metrics, tracing,
a drift-gated eval harness, and deployment plumbing that survives a rollout.

It ships with one working example, a fraud-style anomaly-scoring endpoint
backed by scikit-learn's Isolation Forest, so every piece of the template wraps
around something real instead of a `Hello World` stub.

```bash
make install && make demo
```

`make demo` starts a real server and walks through every feature against it —
scoring, RFC 9457 errors, per-caller rate limiting, idempotent retries, the
Prometheus scrape, and a graceful shutdown. Nothing is stubbed; the recorded
run is in [demo/transcript.txt](demo/transcript.txt).

It runs straight through in about a second, which is too fast to narrate. For a
live audience, `make demo-paced` advances one section per keypress, and
`--pace 2.5` pauses a fixed interval instead.

---

## What is actually in here

| | |
| --- | --- |
| **API** | Versioned router, typed request/response models, unknown fields rejected rather than ignored |
| **Errors** | [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem documents, one shape for every failure, every body carrying a request id |
| **Auth** | API key with constant-time comparison; keys never logged, only fingerprinted |
| **Rate limiting** | Token bucket, per caller, with standard limit headers on success as well as rejection |
| **Idempotency** | `Idempotency-Key` on retries, following the IETF draft: replay, reuse detection, in-flight guard |
| **Logs** | One JSON object per line, every line carrying the request id, trace id and span id |
| **Metrics** | Prometheus at `/metrics`, labelled on the route template so cardinality stays bounded |
| **Tracing** | OpenTelemetry, with a hand-written span around inference so model time is separable from request time |
| **Probes** | `/healthz` and `/readyz` as genuinely different questions |
| **Shutdown** | Readiness fails first, then in-flight requests drain |
| **Model** | Versioned by a fingerprint of algorithm, hyperparameters and library versions; stamped on every response |
| **Batch** | One vectorised model call per batch — **94× faster** than the equivalent loop, measured |
| **Evals** | Labelled regression set gated on decisions and score drift, with latency reported |
| **Tests** | 115 tests, 96% coverage, including property-based and concurrency suites |
| **CI** | Lint, mypy strict, tests on 3.11–3.13, drift gate, dependency audit, image build plus a real smoke test |
| **Deploy** | Multi-stage non-root image, compose stack with Jaeger and Prometheus, Kubernetes manifests with probes, HPA and a PDB |

## Quickstart

```bash
make install          # venv + editable install with dev dependencies
cp .env.example .env  # optional: every value has a working default
make dev              # http://localhost:8000/docs
```

Score an event:

```bash
curl -X POST localhost:8000/api/v1/score \
  -H 'Content-Type: application/json' \
  -d '{"amount": 45, "hour_of_day": 14, "merchant_risk_score": 0.1, "is_new_device": false}'
```

```json
{
  "request_id": "786c5ee0-ecee-4322-8c66-48bab0f06a11",
  "is_anomalous": false,
  "anomaly_score": -0.2422,
  "model_version": "22acf1f28552",
  "latency_ms": 6.538
}
```

Everything else:

```bash
make check       # lint + types + tests with coverage floor + eval gate
make demo        # the guided tour, against a real server
make demo-paced  # the same tour, one section per keypress
make test        # pytest
make eval        # model decisions and drift against the baseline
make docker-run
```

`make help` lists every target.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/score` | Score one event. Honours `Idempotency-Key`. |
| `POST` | `/api/v1/score/batch` | Score many in one vectorised call. |
| `GET` | `/api/v1/health` | Human-readable: version, model version, uptime. |
| `GET` | `/healthz` | Liveness. Restart on failure. |
| `GET` | `/readyz` | Readiness. Pull from rotation on failure, do not restart. |
| `GET` | `/metrics` | Prometheus scrape. |

Request headers: `X-API-Key` when auth is on, `Idempotency-Key` to make a retry
safe, `X-Request-ID` to carry a correlation id in from a gateway.

Response headers: `X-Request-ID`, `X-Response-Time-ms`, `RateLimit-Limit`,
`RateLimit-Remaining`, `RateLimit-Reset`, and `Retry-After` on a 429.

Every error is a problem document — the full catalogue is in
[docs/errors.md](docs/errors.md).

## Project structure

```
app/
├── main.py                      # wiring and lifecycle — read this first
├── core/
│   ├── config.py                # all configuration, validated once
│   ├── context.py               # request-scoped context vars
│   ├── errors.py                # domain errors + RFC 9457 handlers
│   ├── security.py              # API key authentication
│   ├── ratelimit.py             # token bucket
│   ├── idempotency.py           # replay store
│   ├── metrics.py               # Prometheus collectors
│   ├── middleware.py            # request id, timing, access log, metric labels
│   ├── logging.py               # JSON formatter + context filter
│   └── telemetry.py             # tracing, and trace/log correlation
├── api/
│   ├── ops.py                   # /healthz /readyz /metrics — unversioned
│   ├── deps.py                  # auth + rate limit as one dependency
│   └── v1/endpoints/            # the versioned product API
├── models/schemas.py            # the public contract
├── services/anomaly_service.py  # the model; the only file importing sklearn
└── evals/                       # the drift harness

demo/       # the guided tour and its recorded transcript
docs/       # architecture, operations runbook, error catalogue, ADRs
deploy/k8s/ # manifests with probes, HPA, PDB
tests/      # 115 tests: contract, unit, property-based, concurrency
```

## Design notes

The decisions worth arguing about are written down as
[architecture decision records](docs/adr/), each with what it cost and what
would make it wrong later. The short version:

**Evals are separate from tests.** pytest answers "does the API still behave".
The eval harness answers "does the model still make the same calls". They fail
for different reasons and get fixed by different people — a test failure means
someone broke the code, an eval failure means the model changed. The harness
gates on decisions and on score drift against a recorded baseline, and the
baseline only moves on an explicit `--update-baseline` — a gate that refreshes
its own baseline always passes. Latency is measured and printed but not gated,
because a wall-clock budget does not travel from a laptop to a shared CI
runner; `--latency-budget-ms` turns it into a gate on hardware where the number
means something.

**Liveness and readiness are different questions.** Liveness checks the process
and nothing else, because a liveness probe that tests dependencies turns a
downstream outage into a restart loop across every replica at once. Readiness
fails while the model trains and again during shutdown, so traffic stops
arriving before the drain begins.

**Metrics are labelled on the route template, never the raw path.** One time
series per URL is how a Prometheus instance falls over, and a scanner walking
your 404s will find that out for you. Getting the full template out of FastAPI
is [less direct than it looks](docs/adr/0006-route-template-metric-labels.md).

**Scoring handlers are sync, not async.** Inference is CPU-bound and never
awaits, so an `async def` handler would block the event loop and every other
in-flight request with it. Sync handlers run in FastAPI's threadpool — which is
why the state behind them is thread-safe and why there is a concurrency suite
that hammers it from sixteen threads.

**Rate limiting is per API key, after authentication.** Limiting by IP lets one
client behind a NAT gateway exhaust everyone else's quota, and lets a client
rotating addresses escape its own.

**The model has a version.** A fingerprint of the algorithm, hyperparameters,
feature order, and the scikit-learn and numpy versions — stamped on every
response, exposed on `/readyz`, and attached as a metric label. Three weeks
later, "the model said so" is a reproducible answer rather than a guess.

**In-process state is documented, not hidden.** The rate limiter and the
idempotency store are exact on one instance; behind N replicas the limit
multiplies and a retry replays only on the same instance. Both are `Protocol`s
so a Redis backend drops in without touching an endpoint. That work is
deliberately left undone: it adds a dependency the template does not otherwise
need, and the single-instance behaviour is correct until you actually scale.

## Adapting this for a new service

1. Replace `AnomalyService` with your own model or logic. Keep the shape —
   built once in `lifespan`, exposing typed `score` / `score_many` — and
   nothing in the request path changes.
2. Update `app/models/schemas.py` for your real request and response.
3. Replace `app/evals/cases/scoring_cases.jsonl` with cases from your domain,
   each with a verified expected outcome, then `make eval-update` to record the
   baseline.
4. Set `REQUIRE_API_KEY=true` and `API_KEYS`, and point `OTEL_EXPORTER=otlp` at
   a real collector. The service warns at startup if you forget either in
   production.
5. Adjust `deploy/k8s/` — image, replicas, resources — and keep
   `terminationGracePeriodSeconds` above `SHUTDOWN_GRACE_SECONDS`.

## Documentation

- [Architecture](docs/architecture.md) — request path, startup and shutdown, module map
- [Operations runbook](docs/operations.md) — probes, alerts, investigating a request
- [Error catalogue](docs/errors.md) — every problem type and what to do about it
- [Decision records](docs/adr/) — why things are the way they are

## License

MIT — see [LICENSE](./LICENSE).
