# Operations runbook

## Endpoints an operator cares about

| Path | Purpose |
| --- | --- |
| `GET /healthz` | Liveness. Checks nothing but the process. Restart on failure. |
| `GET /readyz` | Readiness. 503 while training or draining. Pull from rotation, do not restart. |
| `GET /metrics` | Prometheus scrape. |
| `GET /api/v1/health` | Human-readable summary: version, model version, uptime. |

Never point a liveness probe at `/readyz`. A readiness failure means "not
now"; a liveness failure means "kill me". Conflating them turns a slow start
into a restart loop.

## Deploying

```bash
kubectl apply -f deploy/k8s/
```

The manifests set both probes, resource requests and limits, a
`PodDisruptionBudget`, and `terminationGracePeriodSeconds` above the app's own
`SHUTDOWN_GRACE_SECONDS` so the drain finishes before the kubelet escalates to
SIGKILL.

## Configuration that matters in production

| Variable | Why |
| --- | --- |
| `ENVIRONMENT=production` | Turns off `/docs` and `/openapi.json`, and enables the startup config check. |
| `REQUIRE_API_KEY=true` + `API_KEYS` | Otherwise the API is open. The service warns at startup. |
| `OTEL_EXPORTER=otlp` | `console` prints every span to stdout, which is a log-volume incident. |
| `SHUTDOWN_GRACE_SECONDS` | Must be below the orchestrator's termination grace period. |

The service logs a warning for each of these at startup rather than refusing
to boot: a misconfigured canary should be visible, not an outage.

## Alerts worth having

| Signal | Query sketch | Means |
| --- | --- | --- |
| Error rate | `rate(dock_http_requests_total{status=~"5.."}[5m])` | Something is broken. |
| Saturation | `rate(dock_rate_limit_rejections_total[5m])` | A client is over quota, or the limit is too low. |
| Latency | `histogram_quantile(0.95, rate(dock_http_request_duration_seconds_bucket[5m]))` | Tail latency. |
| Model drift | `histogram_quantile(0.5, rate(dock_model_anomaly_score_bucket[1h]))` | The score distribution moved. |
| Anomaly rate | `rate(dock_model_decisions_total{outcome="anomalous"}[1h])` | A jump means either an attack or a broken model. |
| Version skew | `count(count by (model_version) (dock_build_info))` | More than 1 during a deploy is fine; sustained is not. |

The last three are the ones ordinary HTTP monitoring misses. A model that has
quietly started calling everything anomalous has perfect uptime.

## Investigating one request

Every response carries `X-Request-ID`, and every error body repeats it as
`request_id`. That id is on every log line written while handling the call:

```bash
kubectl logs deploy/dock | grep '"request_id":"<id>"'
```

With tracing on, the same lines carry `trace_id` and `span_id`, so a slow trace
leads to its logs and a suspicious log line leads to its trace.

## Common situations

**Every request returns 401.** `REQUIRE_API_KEY=true` with an empty or
mismatched `API_KEYS`. The startup log names this case explicitly.

**Readiness never passes.** The model is still training, or training failed.
Check for a `model trained` log line; its absence means the exception above it
is the real story.

**429s after a deploy.** The limiter is in-process, so a scale-down multiplies
each surviving replica's share of traffic without changing its bucket size.

**Scores moved but no code changed.** Check `model_version` on `/readyz`
against the last release. It is a fingerprint of the algorithm, the seed, the
hyperparameters *and* the scikit-learn and numpy versions — so a base-image
bump changes it, and `make eval` will say which cases moved.
