"""One bounded Windows-only diagnostic; never retries a test or a workflow."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TARGET = (
    "nanobot/channels/websocket/tests/test_websocket_channel.py"
    "::test_settings_api_returns_safe_subset_and_updates_whitelist"
)
MAX_EXECUTIONS = 20
CONCURRENCY = 6
PROBE_SECONDS = 300


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def trace_command(command: list[str], output: Path, label: str) -> None:
    result = subprocess.run(command, capture_output=True, timeout=60)
    (output / f"{label}.txt").write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"{label} failed with exit {result.returncode}; no fallback capture")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("The diagnostic requires Windows Kernel-File ETW")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw = Path(tempfile.mkdtemp(prefix="nan6395-settings-etw-"))
    session = "nan6395-settings-" + os.environ["GITHUB_RUN_ID"]
    # This is a fresh disposable hosted runner. Never run this system capture locally.
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("System-wide tracing is allowed only on the task's hosted runner")
    trace_command(["logman", "query", "providers", "Microsoft-Windows-Kernel-File"], output, "file-provider")
    trace_command(["wevtutil", "gp", "Microsoft-Windows-Kernel-File", "/ge:true", "/f:xml"], output, "file-provider-schema")
    etl = raw / "fileio.etl"
    stop_new = threading.Event()
    records: list[dict] = []
    records_lock = threading.Lock()
    deadline = time.monotonic() + PROBE_SECONDS
    write_json(output / "plan.json", {"max_executions": MAX_EXECUTIONS, "concurrency": CONCURRENCY,
        "probe_budget_seconds": PROBE_SECONDS, "target": TARGET, "raw_trace_directory": str(raw),
        "transport": "original real TCP and authenticated WebSocket", "client_read_timeout_seconds": 5})
    trace_command(["logman", "create", "trace", session, "-p", "Microsoft-Windows-Kernel-File", "0x3f0", "5",
                   "-o", str(etl), "-f", "bin", "-max", "128", "-bs", "128", "-nb", "16", "128", "-ct", "perf", "-ets"], output, "trace-start")

    def execute(index: int) -> dict:
        if stop_new.is_set() or time.monotonic() >= deadline:
            return {"execution": index, "started": False}
        trial = output / f"execution-{index:02}"
        trial.mkdir()
        temporary = raw / f"pytest-{index:02}"
        command = [sys.executable, "-B", "-m", "pytest", TARGET, "-q", "--basetemp", str(temporary),
                   "-p", "scripts.settings_ci_diagnostic", "--settings-diagnostic-dir", str(trial / "observations"),
                   "--junitxml", str(trial / "result.xml")]
        record = {"execution": index, "started": True, "command": command, "started_unix_ns": time.time_ns()}
        write_json(trial / "receipt.json", record)
        with (trial / "pytest.txt").open("wb") as stream:
            child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
            record["pid"] = child.pid
            write_json(trial / "receipt.json", record)
            try:
                code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
                record.update({"exit_code": code, "budget_terminated": False})
                if code:
                    stop_new.set()
            except subprocess.TimeoutExpired:
                stop_new.set()
                # The live Popen owns this PID. Kill only its tree, never a historical PID.
                if child.poll() is None:
                    killed = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=20)
                    (trial / "budget-cleanup.txt").write_bytes(killed.stdout + killed.stderr)
                child.wait(timeout=20)
                record.update({"exit_code": child.returncode, "budget_terminated": True})
        record["finished_unix_ns"] = time.time_ns()
        write_json(trial / "receipt.json", record)
        with records_lock:
            records.append(record)
        print(f"execution {index}: exit={record['exit_code']}", flush=True)
        return record

    try:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [pool.submit(execute, i) for i in range(1, MAX_EXECUTIONS + 1)]
            results = [future.result() for future in futures]
    finally:
        try:
            trace_command(["logman", "query", session, "-ets"], output, "trace-status")
        finally:
            try:
                trace_command(["logman", "stop", session, "-ets"], output, "trace-stop")
            finally:
                write_json(output / "executions.json", sorted(records, key=lambda x: x["execution"]))
    # Raw ETL/XML contain unrelated runner paths. Do not upload them; export only scoped DB operations.
    trace_command(["tracerpt", str(etl), "-o", str(raw / "fileio.xml"), "-of", "XML", "-summary", str(output / "trace-summary.txt"), "-y"], output, "trace-convert")
    from scripts.summarize_settings_fileio import summarize

    fileio = summarize(raw / "fileio.xml", records, output)
    write_json(output / "fileio.json", fileio)
    failed = any(r.get("exit_code") or r.get("budget_terminated") for r in results if r["started"])
    outcome = {"started": len(records), "not_started": sum(not r["started"] for r in results),
               "failed": failed, "completed_all_twenty": len(records) == MAX_EXECUTIONS,
               "scoped_file_operations": len(fileio["operations"]), "raw_trace_uploaded": False}
    write_json(output / "outcome.json", outcome)
    print(json.dumps(outcome, indent=2), flush=True)
    if not fileio["operations"]:
        raise RuntimeError("No task SQLite file operations decoded; capture is incomplete")
    return int(failed or len(records) != MAX_EXECUTIONS)


if __name__ == "__main__":
    raise SystemExit(main())
