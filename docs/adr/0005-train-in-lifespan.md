# 0005 — The model trains in the lifespan hook, in a worker thread

**Status:** accepted

## Context

The model has to exist before the first request. Three places it could be
built: at module import, lazily on first request, or in the lifespan hook.

## Decision

In `lifespan`, on a worker thread via `asyncio.to_thread`.

## Consequences

- **Not at import.** Importing `app.main` would train a model — so would
  `mypy`, an editor's autocomplete, a script that wanted one helper, and every
  test collection.
- **Not lazily.** The first caller after each deploy would pay the training
  cost and probably time out. Worse, it is exactly the caller least likely to
  retry politely.
- **On a worker thread**, because training is CPU-bound and releases no event
  loop. Training inline would block the loop, the liveness probe would time
  out, and the orchestrator would kill the container mid-training — forever.
- Readiness reports `model_loaded: false` until training finishes, so the
  instance is not sent traffic it cannot serve.

Swapping training for loading a pickled or registry-hosted model changes this
file and nothing else: the request path only knows `score` and `score_many`.
