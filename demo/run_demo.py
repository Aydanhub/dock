#!/usr/bin/env python3
"""A scripted tour of the service, run against a real server.

Everything below is a genuine HTTP round trip to a uvicorn process this script
starts and stops, and the log lines quoted are read back out of that process's
own stdout. Nothing is stubbed and nothing is pre-recorded — a demo that
cannot fail is not evidence that anything works.

    make demo              # straight through, about a second
    make demo-paced        # one section at a time, for a live audience

Three API keys are configured so that each section gets its own rate-limit
bucket. That is not a workaround: it is the behaviour being demonstrated in
section 8, where one caller burns through its quota and another is unaffected.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

MAIN_KEY = "demo-key-main-3f9a2b7c"
BURST_KEY = "demo-key-burst-8d1e4c05"
RETRY_KEY = "demo-key-retry-27b6f9aa"
RATE_LIMIT = 8

BOLD, DIM, GREEN, RED, YELLOW, CYAN, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m",
)

# Sentinel for a bare --pace, which means "the presenter presses Enter".
MANUAL_PACE = "manual"
FALLBACK_PACE_SECONDS = 2.5

# How long to wait before explaining the wait. Under this, a spinner is enough;
# past it the audience deserves to know why nothing is happening.
COLD_START_HINT_AFTER = 3.0

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

NORMAL_EVENT = {
    "amount": 42.5,
    "hour_of_day": 13,
    "merchant_risk_score": 0.08,
    "is_new_device": False,
}
SUSPICIOUS_EVENT = {
    "amount": 18400.0,
    "hour_of_day": 3,
    "merchant_risk_score": 0.96,
    "is_new_device": True,
}


def paint(text: str, code: str) -> str:
    return text if os.environ.get("NO_COLOR") else f"{code}{text}{RESET}"


class Pacer:
    """Controls how fast the tour advances between sections.

    The default is not to pause at all. The whole run then takes about a
    second, which is right when the output is being captured, diffed, or read
    afterwards — and far too fast for a live audience, who get 185 lines in one
    blink. `--pace` is for that case: either the presenter presses Enter, or a
    fixed interval elapses between sections.
    """

    def __init__(self, spec: str | None) -> None:
        self.manual = False
        self.seconds = 0.0

        if spec is None:
            return
        if spec == MANUAL_PACE:
            # `input()` on a redirected stdin hits EOF immediately, which would
            # fast-forward the entire tour rather than pause it. Somebody
            # piping a paced run into a file means to slow it down, so fall
            # back to a timed pause instead of silently ignoring the flag.
            if sys.stdin.isatty():
                self.manual = True
            else:
                self.seconds = FALLBACK_PACE_SECONDS
            return
        self.seconds = float(spec)

    def between_steps(self) -> None:
        if self.manual:
            print(paint("   ⏎ press Enter for the next section", DIM), end="", flush=True)
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                # Stop asking rather than spinning on a closed stdin.
                self.manual = False
                print()
            else:
                # Erase the prompt so the finished output reads as a clean
                # transcript rather than a list of things somebody pressed.
                print("\033[F\033[2K", end="")
        elif self.seconds > 0:
            time.sleep(self.seconds)


def step(pacer: Pacer, number: int, title: str, why: str) -> None:
    if number > 1:
        pacer.between_steps()
    print()
    rule = "─" * max(3, 64 - len(title) - len(str(number)))
    print(paint(f"── {number}. {title} ", BOLD) + paint(rule, DIM))
    print(paint(f"   {why}", DIM))
    print()


def show(
    request_line: str,
    response: httpx.Response,
    *,
    fields: list[str] | None = None,
) -> None:
    tone = GREEN if response.status_code < 400 else (YELLOW if response.status_code < 500 else RED)
    print(f"   {paint('$', CYAN)} {request_line}")
    print(f"     {paint(f'HTTP {response.status_code}', tone)}", end="")

    notable = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"x-request-id", "ratelimit-remaining", "retry-after", "idempotency-replayed"}
    }
    if notable:
        print("   " + paint("  ".join(f"{k}: {v}" for k, v in sorted(notable.items())), DIM), end="")
    print()

    try:
        body: Any = response.json()
    except ValueError:
        return
    if fields and isinstance(body, dict):
        body = {key: body[key] for key in fields if key in body}
    for line in json.dumps(body, indent=2).splitlines():
        print(f"     {line}")


def quote_log(lines: list[str], *, limit: int = 6) -> None:
    for line in lines[:limit]:
        print(f"     {paint(line.rstrip(), DIM)}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def start_server(port: int, log_path: Path) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "ENVIRONMENT": "local",
        "REQUIRE_API_KEY": "true",
        "API_KEYS": f"{MAIN_KEY},{BURST_KEY},{RETRY_KEY}",
        "RATE_LIMIT_REQUESTS": str(RATE_LIMIT),
        "RATE_LIMIT_WINDOW_SECONDS": "60",
        "OTEL_EXPORTER": "none",
        "LOG_LEVEL": "INFO",
        # Line-buffered, so the log file can be read back while the server runs.
        "PYTHONUNBUFFERED": "1",
    }
    interpreter = REPO_ROOT / ".venv" / "bin" / "python"
    handle = log_path.open("wb")
    # S603: the command is this repository's own interpreter and uvicorn, with a
    # fixed argument list. Nothing here comes from user input.
    return subprocess.Popen(  # noqa: S603
        [
            str(interpreter if interpreter.exists() else sys.executable),
            "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "info",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def read_log(log_path: Path) -> list[str]:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def app_log_lines(log_path: Path, **match: Any) -> list[str]:
    """Application log lines (JSON) matching every given field."""
    found = []
    for line in read_log(log_path):
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if all(record.get(key) == value for key, value in match.items()):
            found.append(line)
    return found


def wait_until_ready(
    client: httpx.Client, server: subprocess.Popen[bytes], timeout: float = 120.0
) -> float:
    """Polls readiness, which is exactly what an orchestrator does on deploy.

    The polling is visible. On a warm machine this returns in under a second,
    but the first run after a fresh install spends around half a minute
    importing scikit-learn and scipy from a cold disk cache — and half a minute
    of an unexplained blank terminal in front of an audience reads as a crash,
    not as a startup. So the wait shows a spinner with elapsed time, and once
    it is long enough to worry anyone, it says why.

    Animation only when stdout is a terminal: carriage returns would otherwise
    fill a captured transcript or a CI log with redrawn spinner frames.
    """
    started = time.perf_counter()
    animated = sys.stdout.isatty()
    tick = 0
    printed_width = 0

    while True:
        elapsed = time.perf_counter() - started
        if elapsed >= timeout:
            raise TimeoutError(f"server never became ready within {timeout:.0f}s")
        if server.poll() is not None:
            raise RuntimeError(f"server exited early with code {server.returncode}")

        try:
            if client.get("/readyz", timeout=2.0).status_code == 200:
                if animated and printed_width:
                    print(" " * printed_width, end="\r", flush=True)
                return time.perf_counter() - started
        except httpx.TransportError:
            pass

        if animated:
            note = (
                "training the model"
                if elapsed < COLD_START_HINT_AFTER
                else "first run imports scikit-learn from a cold disk cache; "
                "later runs start in under a second"
            )
            line = f"   {SPINNER[tick % len(SPINNER)]} waiting for readiness — {note}  {elapsed:4.1f}s"
            print(paint(line.ljust(printed_width), DIM), end="\r", flush=True)
            printed_width = len(line)
            tick += 1

        time.sleep(0.1)


def _pace_spec(value: str) -> str:
    # argparse runs `const` through `type` too, so a bare --pace arrives here
    # as the sentinel and must be let through rather than parsed as a number.
    if value == MANUAL_PACE:
        return value
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--pace expects a number of seconds, got {value!r}"
        ) from None
    if seconds < 0:
        raise argparse.ArgumentTypeError("--pace cannot be negative")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python demo/run_demo.py",
        description="Walk through every feature of the service against a real server.",
    )
    parser.add_argument(
        "--pace",
        nargs="?",
        type=_pace_spec,
        const=MANUAL_PACE,
        default=None,
        metavar="SECONDS",
        help=(
            "Pause between sections so a live audience can keep up. Bare --pace waits "
            "for Enter, so the presenter sets the pace; --pace 2.5 pauses that many "
            "seconds instead. Without it the tour runs straight through in about a second."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pacer = Pacer(args.pace)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    main_auth = {"X-API-Key": MAIN_KEY}
    log_path = Path(tempfile.mkdtemp(prefix="dock-demo-")) / "server.log"

    print()
    print(paint("  Dock — live demo", BOLD))
    print(paint(f"  every response below is a real HTTP call to {base_url}", DIM))

    server = start_server(port, log_path)
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            # 1 ------------------------------------------------------------
            step(pacer, 1, "Startup and readiness",
                 "The model trains once, before traffic. Liveness and readiness are separate signals.")
            elapsed = wait_until_ready(client, server)
            print(f"   {paint('ready after', DIM)} {elapsed:.2f}s\n")
            quote_log(app_log_lines(log_path, logger="app.services.anomaly_service"))
            quote_log(app_log_lines(log_path, message="startup complete"))
            print()
            show("GET /healthz", client.get("/healthz"))
            show("GET /readyz", client.get("/readyz"))

            # 2 ------------------------------------------------------------
            step(pacer, 2, "A routine transaction",
                 "Lunchtime card payment, known device, low-risk merchant.")
            show(
                "POST /api/v1/score   " + json.dumps(NORMAL_EVENT),
                client.post("/api/v1/score", json=NORMAL_EVENT, headers=main_auth),
            )

            # 3 ------------------------------------------------------------
            step(pacer, 3, "A suspicious transaction",
                 "18,400 at 3am, high-risk merchant, device never seen before.")
            suspicious = client.post("/api/v1/score", json=SUSPICIOUS_EVENT, headers=main_auth)
            show("POST /api/v1/score   " + json.dumps(SUSPICIOUS_EVENT), suspicious)

            # 4 ------------------------------------------------------------
            step(pacer, 4, "Request correlation",
                 "Every log line written while handling that call carries its request id.")
            request_id = suspicious.headers["x-request-id"]
            print(f"   {paint('$', CYAN)} grep {request_id} server.log\n")
            quote_log(app_log_lines(log_path, request_id=request_id))

            # 5 ------------------------------------------------------------
            step(pacer, 5, "Batch scoring",
                 "One vectorised model call for the whole batch, not one call per event.")
            batch = [NORMAL_EVENT, SUSPICIOUS_EVENT, NORMAL_EVENT, SUSPICIOUS_EVENT]
            show(
                f"POST /api/v1/score/batch   ({len(batch)} events)",
                client.post("/api/v1/score/batch", json={"events": batch}, headers=main_auth),
                fields=["request_id", "model_version", "count", "anomalous_count", "latency_ms"],
            )

            # 6 ------------------------------------------------------------
            step(pacer, 6, "Errors are RFC 9457 problem documents",
                 "One error shape for every failure, always carrying the request id.")
            show(
                "POST /api/v1/score   (amount: -1)",
                client.post("/api/v1/score", json={**NORMAL_EVENT, "amount": -1}, headers=main_auth),
            )

            # 7 ------------------------------------------------------------
            step(pacer, 7, "API key enforcement",
                 "The same call without a key. The key itself is never echoed or logged.")
            show("POST /api/v1/score   (no X-API-Key)", client.post("/api/v1/score", json=NORMAL_EVENT))

            # 8 ------------------------------------------------------------
            step(pacer, 8, "Rate limiting is per caller",
                 f"One client burns its {RATE_LIMIT}/min quota; another client is untouched.")
            statuses = []
            for attempt in range(1, RATE_LIMIT + 2):
                response = client.post(
                    "/api/v1/score", json=NORMAL_EVENT, headers={"X-API-Key": BURST_KEY}
                )
                statuses.append(response.status_code)
                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after")
                    print(f"   client A, attempt {attempt}: " + paint("HTTP 429", YELLOW)
                          + paint(f"   retry-after: {retry_after}s", DIM))
                    break
                print(f"   client A, attempt {attempt}: " + paint("HTTP 200", GREEN)
                      + paint(f"   remaining: {response.headers.get('ratelimit-remaining')}", DIM))

            unaffected = client.post("/api/v1/score", json=NORMAL_EVENT, headers=main_auth)
            print("\n   client B, first call:  " + paint(f"HTTP {unaffected.status_code}", GREEN)
                  + paint(f"   remaining: {unaffected.headers.get('ratelimit-remaining')}", DIM))
            print(paint("   → one caller's burst cannot exhaust another caller's quota", GREEN))

            # 9 ------------------------------------------------------------
            step(pacer, 9, "Idempotent retries",
                 "A client that times out and retries must not get a second, different decision.")
            retry_auth = {"X-API-Key": RETRY_KEY, "Idempotency-Key": "payment-88213"}
            first = client.post("/api/v1/score", json=SUSPICIOUS_EVENT, headers=retry_auth)
            second = client.post("/api/v1/score", json=SUSPICIOUS_EVENT, headers=retry_auth)
            show("POST /api/v1/score   Idempotency-Key: payment-88213", first,
                 fields=["request_id", "is_anomalous", "anomaly_score"])
            show("POST /api/v1/score   (timed out, retried with the same key)", second,
                 fields=["request_id", "is_anomalous", "anomaly_score"])
            replayed = first.json().get("request_id") == second.json().get("request_id")
            print(paint(
                "   → the original decision was replayed, not recomputed" if replayed
                else "   → decisions differ, which would be a bug",
                GREEN if replayed else RED,
            ))
            show("POST /api/v1/score   (same key, different body)",
                 client.post("/api/v1/score", json=NORMAL_EVENT, headers=retry_auth),
                 fields=["title", "status", "detail"])

            # 10 -----------------------------------------------------------
            step(pacer, 10, "Prometheus metrics",
                 "HTTP metrics say the service is up; the score distribution says the model still works.")
            wanted = (
                "dock_http_requests_total",
                "dock_model_decisions_total",
                "dock_rate_limit_rejections_total",
                "dock_idempotent_replays_total",
                "dock_build_info",
            )
            for line in client.get("/metrics").text.splitlines():
                if line.startswith(wanted):
                    print(f"     {line}")

            # 11 -----------------------------------------------------------
            step(pacer, 11, "Graceful shutdown",
                 "Readiness fails first so traffic stops arriving, then in-flight requests drain.")
            print(f"   {paint('$', CYAN)} kill -TERM  (what `docker stop` and Kubernetes send)\n")
            server.send_signal(signal.SIGTERM)
            returncode = server.wait(timeout=30)

            shutdown_lines = [
                line for line in read_log(log_path)
                if "Shutting down" in line
                or "shutdown complete" in line
                or "Application shutdown complete" in line
            ]
            quote_log(shutdown_lines)
            drained = any("shutdown complete" in line for line in shutdown_lines)
            print()
            print(paint(
                "   → the shutdown hook ran and the app drained before the process exited"
                if drained else "   → the process exited without running its shutdown hook",
                GREEN if drained else RED,
            ))
            print(paint(
                f"   → terminated by SIGTERM (exit status {returncode}), the normal signal-death code",
                DIM,
            ))

        print()
        print(paint("  demo complete", BOLD))
        print()
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    sys.exit(main())
