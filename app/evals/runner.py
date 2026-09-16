"""Offline evaluation harness for the anomaly-scoring model.

This is not a second test suite. pytest answers "does the API still behave" —
status codes, validation, response shape. This answers a different question:
"does the model still make the same calls it used to."

They fail for different reasons and get fixed by different people. A test
failure is a bug in the code. An eval failure is a change in the model's
behaviour, which may be an improvement, a regression, or a dependency bump
nobody expected to matter. Both gate CI; only one of them means "revert".

What it checks
--------------
1. **Decisions** — every labelled case must still get the label it had.
2. **Score drift** — even when a decision is unchanged, a score that has moved
   more than `--max-score-drift` from the recorded baseline is reported. This
   catches the retrain that has not flipped anything *yet* but is on its way.
3. **Latency** — a p95 over the budget fails the run, so a model that got
   slower is caught before it is deployed rather than after.

Usage
-----
    python -m app.evals.runner
    python -m app.evals.runner --report eval_reports/latest.json
    python -m app.evals.runner --update-baseline    # after an intended change
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.services.anomaly_service import AnomalyService

CASES_DIR = Path(__file__).parent / "cases"
DEFAULT_CASES_PATH = CASES_DIR / "scoring_cases.jsonl"
DEFAULT_BASELINE_PATH = CASES_DIR / "baseline.json"

DEFAULT_MAX_SCORE_DRIFT = 0.05
DEFAULT_P95_BUDGET_MS = 25.0

_GREEN, _RED, _YELLOW, _DIM, _RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class CaseResult:
    case_id: str
    expected_anomalous: bool
    actual_anomalous: bool
    anomaly_score: float
    latency_ms: float
    baseline_score: float | None = None
    score_drift: float | None = None
    drifted: bool = False

    @property
    def decision_correct(self) -> bool:
        return self.expected_anomalous == self.actual_anomalous

    @property
    def passed(self) -> bool:
        return self.decision_correct and not self.drifted


@dataclass
class EvalReport:
    model_version: str
    total: int
    passed: int
    failed_decisions: list[str] = field(default_factory=list)
    drifted_cases: list[str] = field(default_factory=list)
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_budget_ms: float = DEFAULT_P95_BUDGET_MS
    within_latency_budget: bool = True
    duration_seconds: float = 0.0
    results: list[CaseResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.failed_decisions
            and not self.drifted_cases
            and self.within_latency_budget
        )


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Reads a JSONL file, naming the offending line when one does not parse."""
    if not path.exists():
        raise FileNotFoundError(f"cases file not found: {path}")

    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON — {exc}") from exc
            for required in ("case_id", "input", "expected_anomalous"):
                if required not in case:
                    raise ValueError(f"{path}:{line_no}: case is missing {required!r}")
            cases.append(case)

    if not cases:
        raise ValueError(f"{path} contains no cases")
    return cases


def load_baseline(path: Path) -> dict[str, float]:
    """Recorded scores from the last accepted run. Absent on a first run."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): float(v) for k, v in data.get("scores", {}).items()}


def run_eval(
    cases_path: Path,
    baseline_path: Path,
    *,
    service: AnomalyService | None = None,
    max_score_drift: float = DEFAULT_MAX_SCORE_DRIFT,
    latency_budget_ms: float = DEFAULT_P95_BUDGET_MS,
) -> EvalReport:
    service = service or AnomalyService()
    cases = load_cases(cases_path)
    baseline = load_baseline(baseline_path)

    started = time.perf_counter()
    results: list[CaseResult] = []

    for case in cases:
        outcome = service.score(case["input"])
        baseline_score = baseline.get(case["case_id"])
        drift = (
            abs(outcome.anomaly_score - baseline_score) if baseline_score is not None else None
        )
        results.append(
            CaseResult(
                case_id=case["case_id"],
                expected_anomalous=case["expected_anomalous"],
                actual_anomalous=outcome.is_anomalous,
                anomaly_score=outcome.anomaly_score,
                latency_ms=outcome.latency_ms,
                baseline_score=baseline_score,
                score_drift=round(drift, 6) if drift is not None else None,
                drifted=drift is not None and drift > max_score_drift,
            )
        )

    latencies = sorted(r.latency_ms for r in results)
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]

    return EvalReport(
        model_version=service.model_version,
        total=len(results),
        passed=sum(r.passed for r in results),
        failed_decisions=[r.case_id for r in results if not r.decision_correct],
        drifted_cases=[r.case_id for r in results if r.drifted],
        latency_p50_ms=round(statistics.median(latencies), 3),
        latency_p95_ms=round(p95, 3),
        latency_budget_ms=latency_budget_ms,
        within_latency_budget=p95 <= latency_budget_ms,
        duration_seconds=round(time.perf_counter() - started, 3),
        results=results,
    )


def print_report(report: EvalReport, *, colour: bool = True) -> None:
    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if colour else text

    width = max((len(r.case_id) for r in report.results), default=8)
    print(f"model {report.model_version}  ·  {report.total} cases\n")
    print(f"{'CASE':<{width}}  {'EXPECTED':<9}  {'ACTUAL':<9}  {'SCORE':>8}  {'DRIFT':>8}  RESULT")

    for result in report.results:
        if not result.decision_correct:
            mark, code = "FAIL", _RED
        elif result.drifted:
            mark, code = "DRIFT", _YELLOW
        else:
            mark, code = "PASS", _GREEN
        drift = f"{result.score_drift:>8.4f}" if result.score_drift is not None else f"{'—':>8}"
        print(
            f"{result.case_id:<{width}}  "
            f"{result.expected_anomalous!s:<9}  "
            f"{result.actual_anomalous!s:<9}  "
            f"{result.anomaly_score:>8.4f}  {drift}  {paint(mark, code)}"
        )

    print()
    print(f"decisions   {report.passed}/{report.total} correct")
    if report.failed_decisions:
        print(paint(f"  flipped:  {', '.join(report.failed_decisions)}", _RED))
    if report.drifted_cases:
        print(paint(f"  drifted:  {', '.join(report.drifted_cases)}", _YELLOW))
    budget = "within" if report.within_latency_budget else "OVER"
    print(
        f"latency     p50 {report.latency_p50_ms:.3f}ms  ·  p95 {report.latency_p95_ms:.3f}ms  "
        f"({budget} the {report.latency_budget_ms:.0f}ms budget)"
    )
    print(f"{_DIM if colour else ''}finished in {report.duration_seconds:.2f}s{_RESET if colour else ''}")
    print()
    print(paint("EVAL PASSED", _GREEN) if report.ok else paint("EVAL FAILED", _RED))


def write_baseline(path: Path, report: EvalReport) -> None:
    """Records the current scores as the new reference.

    Deliberately a separate, explicit command. If the harness rewrote the
    baseline on every run it would always pass, and drift detection would be
    theatre.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "model_version": report.model_version,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "scores": {r.case_id: r.anomaly_score for r in report.results},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evals.runner",
        description="Check the model's decisions against a labelled regression set.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_PATH)
    parser.add_argument("--report", type=Path, default=None, help="Write a JSON report here")
    parser.add_argument("--max-score-drift", type=float, default=DEFAULT_MAX_SCORE_DRIFT)
    parser.add_argument("--latency-budget-ms", type=float, default=DEFAULT_P95_BUDGET_MS)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Record the current scores as the new baseline and exit 0",
    )
    parser.add_argument("--no-colour", action="store_true")
    args = parser.parse_args(argv)

    report = run_eval(
        args.cases,
        args.baseline,
        max_score_drift=args.max_score_drift,
        latency_budget_ms=args.latency_budget_ms,
    )
    print_report(report, colour=not args.no_colour and sys.stdout.isatty())

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(report)
        payload["ok"] = report.ok
        args.report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"report written to {args.report}")

    if args.update_baseline:
        write_baseline(args.baseline, report)
        print(f"baseline updated at {args.baseline}")
        return 0

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
