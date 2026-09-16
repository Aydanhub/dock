"""Configuration parsing and the startup checks that run against it."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


class TestApiKeyParsing:
    def test_comma_separated_keys(self):
        assert Settings(api_keys="a, b ,c").api_keys == ["a", "b", "c"]

    def test_json_list_of_keys(self):
        assert Settings(api_keys='["a", "b"]').api_keys == ["a", "b"]

    def test_empty_string_means_no_keys(self):
        assert Settings(api_keys="  ").api_keys == []

    def test_malformed_json_is_a_clear_error(self):
        with pytest.raises(ValidationError, match="does not parse"):
            Settings(api_keys='["a",')


class TestLogLevel:
    def test_case_is_normalised(self):
        assert Settings(log_level="debug").log_level == "DEBUG"

    def test_an_unknown_level_is_rejected(self):
        with pytest.raises(ValidationError, match="log_level must be one of"):
            Settings(log_level="chatty")


class TestAuthActive:
    def test_off_when_not_required(self):
        assert Settings(require_api_key=False, api_keys=["k"]).auth_active is False

    def test_off_when_required_but_no_keys_configured(self):
        assert Settings(require_api_key=True, api_keys=[]).auth_active is False

    def test_on_when_required_and_configured(self):
        assert Settings(require_api_key=True, api_keys=["k"]).auth_active is True


class TestRuntimeChecks:
    def test_local_defaults_raise_nothing(self):
        assert Settings(environment="local").validate_runtime() == []

    def test_production_without_auth_is_flagged(self):
        problems = Settings(environment="production").validate_runtime()

        assert any("unauthenticated" in p for p in problems)

    def test_production_requiring_keys_without_any_is_flagged(self):
        """This is the configuration that rejects 100% of traffic, so it is
        worth naming explicitly rather than debugging at 3am."""
        problems = Settings(
            environment="production", require_api_key=True, api_keys=[]
        ).validate_runtime()

        assert any("every request will be rejected" in p for p in problems)

    def test_production_with_console_tracing_is_flagged(self):
        problems = Settings(
            environment="production", require_api_key=True, api_keys=["k"]
        ).validate_runtime()

        assert any("console" in p for p in problems)

    def test_a_correct_production_config_is_clean(self):
        problems = Settings(
            environment="production",
            require_api_key=True,
            api_keys=["k"],
            otel_exporter="otlp",
        ).validate_runtime()

        assert problems == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_contamination", 0.0),
        ("model_contamination", 0.9),
        ("rate_limit_requests", 0),
        ("rate_limit_window_seconds", 0),
        ("max_batch_size", 0),
        ("request_timeout_seconds", 0),
    ],
)
def test_out_of_range_settings_fail_at_startup_not_at_request_time(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})
