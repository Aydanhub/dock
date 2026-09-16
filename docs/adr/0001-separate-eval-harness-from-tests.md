# 0001 — The eval harness is separate from the test suite

**Status:** accepted

## Context

The service has two kinds of correctness. The API has a contract: status codes,
validation, response shape. The model has behaviour: which events it calls
anomalous, and how confidently.

Folding model checks into pytest is the obvious move and the wrong one. The two
fail for different reasons, on different schedules, and are fixed by different
people. A test failure means someone broke the code. A model failure means the
model changed — which might be an improvement, a regression, or a dependency
bump nobody expected to matter.

## Decision

`app/evals/` is a separate harness with its own CLI, its own labelled cases,
and its own recorded baseline. It checks three things:

1. **Decisions** — every labelled case still gets its label.
2. **Score drift** — scores that moved more than a threshold from the baseline,
   even when the decision has not flipped yet.
3. **Latency** — p95 against a budget.

CI runs both `pytest` and `make eval`, and fails on either.

## Consequences

- A `pytest` failure and an eval failure carry different meanings, so the
  response to each is different. That is the whole point.
- Drift detection needs a baseline, and the baseline is only updated by an
  explicit `--update-baseline`. If the harness refreshed it automatically it
  would always pass, and the gate would be theatre.
- The drift threshold is a judgement call. Too tight and every dependency bump
  is a failure; too loose and a real regression slips through. `0.05` on this
  score scale is wide enough to absorb floating-point noise across platforms
  and narrow enough to catch a retrain.

## When this stops being right

If the model is served from a registry with its own evaluation and promotion
pipeline, this harness becomes a duplicate of that pipeline's gate. At that
point it should call the registry rather than train locally.
