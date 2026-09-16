# 0006 — Metrics are labelled with the route template

**Status:** accepted

## Context

HTTP metrics need a label saying which endpoint they describe. The obvious
choice, `request.url.path`, is a well-known way to take down a monitoring
system: every distinct URL becomes its own time series. Add a path parameter,
or let a vulnerability scanner walk the service, and the cardinality is
unbounded.

## Decision

Label on the route *template* — `/api/v1/score`, never a concrete path — and
use the literal `unmatched` for requests that matched no route, so scanner 404s
collapse into one series.

Getting the full template is less direct than it looks. `scope["route"]` holds
the route as its own router declared it, which for an included router is the
un-prefixed path: `/score`, not `/api/v1/score`. Labelling with that would
merge two genuinely different endpoints that happen to share a suffix across
API versions.

So the route tree is walked once at startup, building a map from route object
identity to its full template. Both router shapes are handled: FastAPI versions
that nest included routers, and older ones that flatten them at include time.

## Consequences

- Cardinality is bounded by the number of routes the service declares.
- Lookup is a dict hit per request; the walk happens once, at startup.
- The map is rebuilt on a miss, so routes added after startup are still
  labelled correctly.
- It depends on FastAPI internals (`include_context`), which is why the
  fallbacks exist and why a test asserts the full template rather than trusting
  it.
