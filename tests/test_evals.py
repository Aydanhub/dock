"""The eval harness itself is code, so it gets tests too.

A drift gate that silently never fires is worse than no gate at all — it
reports success while checking nothing.
"""

import json

import pytest

from app.evals.runner import (
    DEFAULT_BASELINE_PATH,
    DEFAULT_CASES_PATH,
    load_cases,
    main,
    run_eval,
    write_baseline,
)
from app.services.anomaly_service import AnomalyService


@pytest.fixture(scope="module")
def service():
    return AnomalyService()


def test_the_checked_in_cases_all_pass(service):
    """This is the gate CI runs. If it fails here, the model's behaviour
    changed since the baseline was recorded."""
    report = run_eval(DEFAULT_CASES_PATH, DEFAULT_BASELINE_PATH, service=service)

    assert report.ok, f"flipped={report.failed_decisions} drifted={report.drifted_cases}"
    assert report.total == 10


def test_the_baseline_covers_every_case(service):
    """A case with no baseline entry is exempt from drift detection. Silently
    exempting cases is how a drift gate stops gating."""
    cases = {case["case_id"] for case in load_cases(DEFAULT_CASES_PATH)}
    baseline = json.loads(DEFAULT_BASELINE_PATH.read_text())["scores"]

    assert cases == set(baseline)


def test_drift_is_detected(tmp_path, service):
    """Feed the harness a deliberately wrong baseline; it must notice."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"scores": {case["case_id"]: 99.0 for case in load_cases(DEFAULT_CASES_PATH)}})
    )

    report = run_eval(DEFAULT_CASES_PATH, baseline, service=service)

    assert not report.ok
    assert len(report.drifted_cases) == report.total


def test_a_flipped_decision_fails_the_run(tmp_path, service):
    cases = load_cases(DEFAULT_CASES_PATH)
    flipped = tmp_path / "cases.jsonl"
    flipped.write_text(
        "\n".join(
            json.dumps({**case, "expected_anomalous": not case["expected_anomalous"]})
            for case in cases
        )
    )

    report = run_eval(flipped, tmp_path / "missing.json", service=service)

    assert not report.ok
    assert len(report.failed_decisions) == len(cases)


def test_latency_is_reported_but_not_gated_by_default(service):
    """A wall-clock budget is not portable: the same model takes single-digit
    milliseconds on a laptop and ten times that on a shared CI runner. So p95
    is always measured, and only gates the run when a budget is asked for."""
    report = run_eval(DEFAULT_CASES_PATH, DEFAULT_BASELINE_PATH, service=service)

    assert report.latency_p95_ms > 0
    assert report.latency_budget_ms is None
    assert report.within_latency_budget is True


def test_an_explicit_latency_budget_does_gate_the_run(service):
    report = run_eval(
        DEFAULT_CASES_PATH, DEFAULT_BASELINE_PATH, service=service, latency_budget_ms=0.0
    )

    assert report.within_latency_budget is False
    assert report.ok is False


def test_a_missing_baseline_is_not_an_error(tmp_path, service):
    """A first run, before any baseline exists, still checks decisions."""
    report = run_eval(DEFAULT_CASES_PATH, tmp_path / "nope.json", service=service)

    assert report.ok
    assert all(result.score_drift is None for result in report.results)


class TestCaseLoading:
    def test_malformed_json_names_the_line(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text('{"case_id": "a", "input": {}, "expected_anomalous": false}\n{oops\n')

        with pytest.raises(ValueError, match=r"bad\.jsonl:2"):
            load_cases(path)

    def test_a_case_missing_a_field_is_rejected(self, tmp_path):
        path = tmp_path / "incomplete.jsonl"
        path.write_text('{"case_id": "a", "input": {}}\n')

        with pytest.raises(ValueError, match="expected_anomalous"):
            load_cases(path)

    def test_an_empty_file_is_rejected(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("\n\n")

        with pytest.raises(ValueError, match="no cases"):
            load_cases(path)

    def test_a_missing_file_is_rejected(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_cases(tmp_path / "nope.jsonl")


def test_cli_exits_zero_when_the_model_is_unchanged(capsys):
    exit_code = main(["--no-colour"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "EVAL PASSED" in captured.out


def test_cli_exits_nonzero_on_a_latency_regression(capsys):
    exit_code = main(["--no-colour", "--latency-budget-ms", "0"])

    assert exit_code == 1
    assert "EVAL FAILED" in capsys.readouterr().out


def test_baseline_round_trips(tmp_path, service):
    report = run_eval(DEFAULT_CASES_PATH, tmp_path / "none.json", service=service)
    baseline = tmp_path / "written.json"
    write_baseline(baseline, report)

    rerun = run_eval(DEFAULT_CASES_PATH, baseline, service=service)

    assert rerun.ok
    assert all(result.score_drift == 0 for result in rerun.results)
