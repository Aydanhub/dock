# syntax=docker/dockerfile:1

# --- builder: resolve dependencies into a self-contained venv ---------------
FROM python:3.12-slim AS builder

WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies are installed from pyproject alone, before any application code
# is copied, so this layer is reused across code changes. A one-line edit to a
# handler then rebuilds in seconds instead of re-resolving scikit-learn.
COPY pyproject.toml README.md ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install --upgrade pip \
    && pip install .

# --- runtime: the venv and the app, no build tooling ------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="dock" \
      org.opencontainers.image.description="Production-ready FastAPI template for AI-powered backend services" \
      org.opencontainers.image.source="https://github.com/Aydanhub/dock" \
      org.opencontainers.image.licenses="MIT"

# A fixed non-root uid, so a mounted volume's ownership is predictable and the
# container cannot write where it should not.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser pyproject.toml README.md ./

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    OTEL_EXPORTER=none

USER appuser
EXPOSE 8000

# Liveness, not readiness: this decides whether to restart the container, and
# a container that is merely still training must not be restarted.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

# Exec form, so uvicorn is PID 1 and receives SIGTERM directly — a shell-form
# CMD would swallow the signal and the container would be SIGKILLed after the
# grace period, dropping in-flight requests on every deploy.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
