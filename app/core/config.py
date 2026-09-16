import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["local", "staging", "production"]


class Settings(BaseSettings):
    """Central configuration, parsed and validated once per process.

    Every value has a safe local default so the service runs out of the box.
    Override through environment variables or a `.env` file (see
    `.env.example`). Anything that differs between environments belongs here
    and nowhere else — no module reads `os.environ` directly.

    `protected_namespaces=()` is deliberate: pydantic reserves the `model_`
    prefix for its own attributes and warns on fields that use it, but
    `model_contamination` / `model_random_seed` are the clearest names for
    what they configure, so the guard is turned off rather than the fields
    renamed.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    # --- service identity -------------------------------------------------
    app_name: str = "dock"
    environment: Environment = "local"
    log_level: str = "INFO"

    # --- authentication ---------------------------------------------------
    # `NoDecode` turns off pydantic-settings' automatic JSON decoding for this
    # field. Without it the env source tries `json.loads` on the raw value
    # before any validator runs, so the ergonomic `API_KEYS=key-a,key-b` form
    # fails at startup with a JSON error. With it, the raw string reaches
    # `_split_api_keys` below, which accepts both forms.
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    require_api_key: bool = False

    # --- rate limiting ----------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=120, ge=1)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0)

    # --- idempotency ------------------------------------------------------
    idempotency_enabled: bool = True
    idempotency_ttl_seconds: float = Field(default=300.0, gt=0)
    idempotency_max_entries: int = Field(default=10_000, ge=1)

    # --- request handling -------------------------------------------------
    max_batch_size: int = Field(default=100, ge=1)
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    shutdown_grace_seconds: float = Field(default=10.0, ge=0)

    # --- observability ----------------------------------------------------
    # Named for what it controls: whether the /metrics route is mounted.
    # Collection itself is always on, because the collectors are in-process
    # counters that cost nothing to keep. Calling the switch `metrics_enabled`
    # would promise to stop collection, which it does not do.
    metrics_endpoint_enabled: bool = True
    otel_exporter: Literal["console", "otlp", "none"] = "console"
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"

    # --- model ------------------------------------------------------------
    model_random_seed: int = 42
    model_contamination: float = Field(default=0.02, gt=0, le=0.5)
    model_training_samples: int = Field(default=2000, ge=100)
    model_n_estimators: int = Field(default=200, ge=1)

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, value: object) -> object:
        """Accepts `API_KEYS=a,b,c` as well as a JSON list.

        Comma-separated is what anyone writing a `.env` file or a Kubernetes
        secret will reach for first, so it is supported directly rather than
        documented away.
        """
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"API_KEYS looks like JSON but does not parse: {exc}") from exc
        return [part.strip() for part in stripped.split(",") if part.strip()]

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

    @property
    def is_local(self) -> bool:
        return self.environment == "local"

    @property
    def auth_active(self) -> bool:
        """Auth is enforced when it is switched on *and* keys exist.

        Splitting "configured" from "enforced" means a missing `API_KEYS` in
        production fails loudly at startup (see `validate_runtime`) instead of
        silently serving an open endpoint.
        """
        return self.require_api_key and bool(self.api_keys)

    def validate_runtime(self) -> list[str]:
        """Returns configuration problems that matter outside local dev.

        Called at startup. These are the mistakes that are cheap to make and
        expensive to discover in production, so they are named explicitly
        rather than left to a reader of the config file.
        """
        problems: list[str] = []
        if self.environment == "production":
            if not self.require_api_key:
                problems.append("REQUIRE_API_KEY is false in production — the API is unauthenticated")
            elif not self.api_keys:
                problems.append("REQUIRE_API_KEY is true but API_KEYS is empty — every request will be rejected")
            if self.otel_exporter == "console":
                problems.append("OTEL_EXPORTER=console in production — point it at a real collector")
        return problems


@lru_cache
def get_settings() -> Settings:
    """Settings are read once per process and shared through this cache.

    Tests that need different values call `get_settings.cache_clear()` after
    patching the environment (see `tests/conftest.py`).
    """
    return Settings()
