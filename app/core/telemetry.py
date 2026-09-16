"""OpenTelemetry tracing setup.

Two things here are worth more than the boilerplate:

- **The exporter is a config switch, not a code change.** `console` for local
  dev, `otlp` for a real collector, `none` for quiet test runs. Nothing else
  in the codebase knows which is active.
- **Traces and logs are joined.** `trace_id` and `span_id` are injected into
  every log record, so a slow trace in Jaeger leads straight to the log lines
  emitted while it was running, and a suspicious log line leads back to its
  trace. Without this the two systems are searched separately and by hand.
"""

import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

from app import __version__
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Modules that want a custom span import this: `from app.core.telemetry import tracer`.
tracer = trace.get_tracer("dock")


class TraceContextFilter(logging.Filter):
    """Attaches the active trace and span ids to every log record.

    A filter rather than a formatter change, because it has to run for any
    handler the deployment adds later, not only the JSON one configured here.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        context = span.get_span_context()
        if context.is_valid:
            record.trace_id = format(context.trace_id, "032x")
            record.span_id = format(context.span_id, "016x")
        return True


def _install_trace_log_correlation() -> None:
    """Attaches the filter to every root handler.

    Handlers rather than the root logger itself: a logger's filters only run
    for records logged directly to it, whereas handler filters run for every
    record that propagates up from the application's own loggers.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, TraceContextFilter) for f in handler.filters):
            handler.addFilter(TraceContextFilter())


def configure_telemetry(app: Any) -> None:
    """Sets up a TracerProvider and instruments the FastAPI app.

    Instrumentation is imported lazily and its absence is survivable: the
    service still starts and still emits its own hand-written spans, it just
    stops auto-instrumenting HTTP handling. A missing optional observability
    package is not a reason to refuse to serve traffic.
    """
    settings = get_settings()

    if settings.otel_exporter == "none":
        logger.info("telemetry disabled (OTEL_EXPORTER=none)")
        return

    resource = Resource.create(
        {
            SERVICE_NAME: settings.app_name,
            SERVICE_VERSION: __version__,
            "deployment.environment": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)

    if settings.otel_exporter == "console":
        # SimpleSpanProcessor exports synchronously: fine for reading spans in
        # a terminal, wrong for production, where BatchSpanProcessor keeps
        # export off the request path.
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif settings.otel_exporter == "otlp":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "OTEL_EXPORTER=otlp requires the 'opentelemetry-exporter-otlp' package. "
                "Install it, or set OTEL_EXPORTER=console for local dev."
            ) from exc
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
        )

    trace.set_tracer_provider(provider)
    _install_trace_log_correlation()

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:  # pragma: no cover - optional extra
        logger.warning(
            "opentelemetry-instrumentation-fastapi is not installed — "
            "custom spans still work, HTTP auto-instrumentation is off"
        )
    else:
        FastAPIInstrumentor.instrument_app(
            app,
            # Probe and scrape endpoints fire constantly and would dominate
            # the trace volume without adding anything.
            excluded_urls="healthz,readyz,metrics",
        )

    logger.info("telemetry configured", extra={"exporter": settings.otel_exporter})
