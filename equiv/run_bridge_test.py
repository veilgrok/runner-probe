#!/usr/bin/env python3
"""Orchestrate the runner + Upstash aggregation test. Never prints secrets."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import oracle

ROOT = Path(__file__).resolve().parent.parent
EQUIV = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("BRIDGE_OUT_DIR") or "bridge-out")
WORK_MS = int(os.environ.get("WORK_MS") or "120")
DROP_WORK_MS = int(os.environ.get("DROP_WORK_MS") or "200")
PYTHON = sys.executable


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print("missing required env:", ", ".join(missing), file=sys.stderr)
        raise SystemExit(2)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("check", True)
    return subprocess.run(cmd, **kw)


def node(script: str, *args: str, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    return run(["node", str(EQUIV / script), *args], check=check, timeout=timeout)


def start_logged(cmd: list[str], log_path: Path, cwd: Path | None = None, env: dict | None = None) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = log_path.open("w", encoding="utf-8")
    use_env = os.environ.copy()
    if env:
        use_env.update(env)
    use_env["PYTHONPATH"] = str(EQUIV) + (os.pathsep + use_env["PYTHONPATH"] if use_env.get("PYTHONPATH") else "")
    return subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(cwd or ROOT), env=use_env)


def wait_log(path: Path, needle: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if needle in text:
            return
        if proc.poll() is not None:
            raise RuntimeError(f"{cmd_name(proc)} exited before {needle!r}: {text[-2000:]}")
        time.sleep(0.1)
    raise TimeoutError(f"timeout waiting for {needle!r} in {path}")


def cmd_name(proc: subprocess.Popen) -> str:
    return " ".join(map(str, proc.args[:3]))


def stop_proc(proc: subprocess.Popen | None, sig: int = signal.SIGTERM) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def start_controller(mode: str, intervals, expected, required: list[str], timeout: float, out: Path, status: Path, no_bye: bool = False) -> subprocess.Popen:
    cmd = [
        PYTHON,
        str(EQUIV / "controller.py"),
        "--listen",
        "127.0.0.1:7700",
        "--mode",
        mode,
        "--intervals",
        json.dumps(intervals),
        "--expected",
        json.dumps(expected),
        "--verify-frac",
        "0.2",
        "--timeout",
        str(timeout),
        "--ready-for",
        ",".join(required),
        "--ready-timeout",
        "40",
        "--heartbeat-timeout",
        "4.0",
        "--status-path",
        str(status),
        "--out",
        str(out),
    ]
    if no_bye:
        cmd.append("--no-bye")
    return start_logged(cmd, OUT_DIR / f"{mode}-controller.log")


def start_local_worker(name: str, port: int, work_ms: int) -> subprocess.Popen:
    cmd = [
        PYTHON,
        str(EQUIV / "worker.py"),
        "--listen",
        f"127.0.0.1:{port}",
        "--controller",
        "127.0.0.1:7700",
        "--name",
        name,
        "--work-ms",
        str(work_ms),
    ]
    return start_logged(cmd, OUT_DIR / f"{name}.log")


def start_bridge() -> subprocess.Popen:
    env = os.environ.copy()
    proc = start_logged(["node", str(EQUIV / "upstash_bridge.mjs")], OUT_DIR / "bridge.log")
    # start_logged doesn't pass env override; parent env already has secrets.
    _ = env
    return proc


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_status(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def wait_controller(proc: subprocess.Popen, out: Path, timeout: float) -> dict:
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_proc(proc, signal.SIGKILL)
        raise TimeoutError(f"controller timed out; out={out}")
    if not out.exists():
        log = (OUT_DIR / f"{out.stem.replace('-result','')}-controller.log")
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        raise RuntimeError(f"controller exit {rc} with no result file: {text[-2000:]}")
    data = read_json(out)
    data["exit"] = rc
    return data


def main() -> None:
    require_env("UPSTASH_BOX_API_KEY", "UPSTASH_BOX_ID")
    oracle.sanity_check()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    compare_intervals = oracle.COMPARE_INTERVALS
    compare_expected = oracle.expected_counts(compare_intervals)
    drop_intervals = oracle.DROP_INTERVALS
    drop_expected = oracle.expected_counts(drop_intervals)

    report: dict = {
        "workload": "equivalent",
        "workload_note": (
            "Labeled equivalent of the lab's 40-interval prime-counting job "
            "(0..10,000,000 by 250,000) plus the RELIABILITY.md mid-run 80-task set. "
            "Uses the same outbound WebSocket datagram bridge. Not the private Veil binary or Wasm task."
        ),
        "work_ms": WORK_MS,
        "drop_work_ms": DROP_WORK_MS,
        "started_unix": time.time(),
    }

    print("PHASE upload", flush=True)
    node("box_setup.mjs", "upload", timeout=60)

    print("PHASE rtt", flush=True)
    rtt_out = OUT_DIR / "rtt.json"
    os.environ["RTT_OUT"] = str(rtt_out)
    rtt_proc = run(["node", str(EQUIV / "rtt.mjs")], check=False, timeout=90)
    report["rtt_exit"] = rtt_proc.returncode
    report["rtt"] = read_json(rtt_out) if rtt_out.exists() else None

    # --- runner-only ---
    print("PHASE runner-only", flush=True)
    local = start_local_worker("runner", 27700, WORK_MS)
    out = OUT_DIR / "runner-only.json"
    status = OUT_DIR / "runner-only-status.json"
    ctrl = start_controller("runner-only", compare_intervals, compare_expected, ["runner"], 90, out, status)
    try:
        report["runner_only"] = wait_controller(ctrl, out, 100)
    finally:
        stop_proc(ctrl)
        stop_proc(local)

    # Box worker + bridge stay up across box-only, combined, and mid-run.
    print("PHASE start box worker + bridge", flush=True)
    os.environ["WORK_MS"] = str(WORK_MS)
    node("box_setup.mjs", "start-worker", timeout=30)
    bridge = start_bridge()
    try:
        wait_log(OUT_DIR / "bridge.log", "BRIDGE_READY", bridge, 40)

        print("PHASE box-only", flush=True)
        out = OUT_DIR / "box-only.json"
        status = OUT_DIR / "box-only-status.json"
        ctrl = start_controller("box-only", compare_intervals, compare_expected, ["upstash-box"], 90, out, status, no_bye=True)
        try:
            report["box_only"] = wait_controller(ctrl, out, 100)
        finally:
            stop_proc(ctrl)

        print("PHASE combined", flush=True)
        # Restart the box worker in case a previous phase stopped it.
        node("box_setup.mjs", "stop-worker", timeout=30)
        node("box_setup.mjs", "start-worker", timeout=30)
        time.sleep(0.4)
        local = start_local_worker("runner", 27700, WORK_MS)
        out = OUT_DIR / "combined.json"
        status = OUT_DIR / "combined-status.json"
        ctrl = start_controller("combined", compare_intervals, compare_expected, ["runner", "upstash-box"], 90, out, status, no_bye=True)
        try:
            report["combined"] = wait_controller(ctrl, out, 100)
        finally:
            stop_proc(ctrl)
            stop_proc(local)

        # Mid-run: start the larger job, kill the runner worker after both have completed a task.
        print("PHASE mid-run kill runner worker", flush=True)
        os.environ["WORK_MS"] = str(DROP_WORK_MS)
        # Restart box worker with heavier per-task work so the job lasts long enough to observe the kill.
        node("box_setup.mjs", "stop-worker", timeout=30)
        node("box_setup.mjs", "start-worker", timeout=30)
        time.sleep(0.5)
        local = start_local_worker("runner", 27700, DROP_WORK_MS)
        out = OUT_DIR / "midrun.json"
        status = OUT_DIR / "midrun-status.json"
        ctrl = start_controller("midrun", drop_intervals, drop_expected, ["runner", "upstash-box"], 150, out, status, no_bye=True)
        killed = False
        kill_unix = None
        status_at_kill = {}
        deadline = time.perf_counter() + 40
        while time.perf_counter() < deadline and ctrl.poll() is None:
            st = read_status(status)
            workers = st.get("workers") or {}
            r = (workers.get("runner") or {}).get("completed", 0)
            b = (workers.get("upstash-box") or {}).get("completed", 0)
            if r >= 1 and b >= 1:
                kill_unix = time.time()
                status_at_kill = st
                stop_proc(local, signal.SIGKILL)
                local = None
                killed = True
                print("KILLED_RUNNER_WORKER", "completed", r, b, flush=True)
                break
            time.sleep(0.15)
        mid = wait_controller(ctrl, out, 160)
        mid["injected"] = killed
        mid["status_at_kill"] = status_at_kill
        ev = mid.get("events") or []
        start_ev = next((e for e in ev if e.get("kind") == "start"), None)
        reassigns = [e for e in ev if e.get("kind") == "reassign"]
        mid["start_event"] = start_ev
        mid["first_reassign_event"] = reassigns[0] if reassigns else None
        mid["kill_unix"] = kill_unix
        mid["kill_elapsed_seconds"] = None
        mid["post_kill_seconds"] = None
        if killed and start_ev and kill_unix is not None:
            mid["kill_elapsed_seconds"] = kill_unix - start_ev["t"]
            if mid.get("wall_seconds") is not None:
                mid["post_kill_seconds"] = mid["wall_seconds"] - mid["kill_elapsed_seconds"]
        elif killed and reassigns and mid.get("wall_seconds") is not None:
            mid["kill_elapsed_seconds"] = reassigns[0]["elapsed"]
            mid["post_kill_seconds"] = mid["wall_seconds"] - reassigns[0]["elapsed"]
        report["midrun"] = mid
        stop_proc(ctrl)
        stop_proc(local)

        # Simulate the Actions job ending: close the outbound WebSocket / bridge,
        # inspect how the box worker behaves, then recover the runner side.
        print("PHASE simulate runner job end + recovery", flush=True)
        inspect_before = OUT_DIR / "inspect-before-close.txt"
        os.environ["INSPECT_OUT"] = str(inspect_before)
        node("box_setup.mjs", "inspect", timeout=30)
        close_t = time.perf_counter()
        stop_proc(bridge, signal.SIGTERM)
        bridge = None
        time.sleep(1.0)
        inspect_after = OUT_DIR / "inspect-after-close.txt"
        os.environ["INSPECT_OUT"] = str(inspect_after)
        node("box_setup.mjs", "inspect", timeout=30)
        after_text = inspect_after.read_text(encoding="utf-8", errors="replace") if inspect_after.exists() else ""
        box_alive_after_close = "ALIVE" in after_text and "DEAD" not in after_text.split("--- proc ---")[-1][:80]

        # Recovery: restart bridge (and worker if it died), start a local worker, wait until both hellos arrive.
        recover_start = time.perf_counter()
        if not box_alive_after_close:
            node("box_setup.mjs", "start-worker", timeout=30)
        bridge = start_bridge()
        wait_log(OUT_DIR / "bridge.log", "BRIDGE_READY", bridge, 40)
        local = start_local_worker("runner", 27700, WORK_MS)
        # Use a tiny 2-task job as a readiness/recovery probe (same oracle path).
        tiny = compare_intervals[:2]
        tiny_exp = compare_expected[:2]
        out = OUT_DIR / "recovery.json"
        status = OUT_DIR / "recovery-status.json"
        ctrl = start_controller("recovery", tiny, tiny_exp, ["runner", "upstash-box"], 60, out, status)
        rec = wait_controller(ctrl, out, 70)
        recover_seconds = time.perf_counter() - recover_start
        rec["recover_seconds_from_restart"] = recover_seconds
        rec["seconds_from_session_close"] = time.perf_counter() - close_t
        rec["box_worker_alive_after_session_close"] = box_alive_after_close
        rec["inspect_after_close"] = after_text[-4000:]
        rec["inspect_before_close"] = inspect_before.read_text(encoding="utf-8", errors="replace")[-2000:] if inspect_before.exists() else ""
        report["recovery"] = rec
        stop_proc(ctrl)
        stop_proc(local)
        stop_proc(bridge)
        bridge = None
    finally:
        stop_proc(bridge)
        try:
            node("box_setup.mjs", "stop-worker", timeout=30)
        except Exception as exc:
            print("stop-worker error", type(exc).__name__, flush=True)

    report["finished_unix"] = time.time()
    report["elapsed_seconds"] = report["finished_unix"] - report["started_unix"]
    # High-level correctness flag across comparison jobs and mid-run.
    report["correct"] = all(
        (report.get(k) or {}).get("correct") is True
        for k in ("runner_only", "box_only", "combined", "midrun")
    )
    summary = {
        "workload": report["workload"],
        "correct": report["correct"],
        "rtt": [
            {k: row[k] for k in row if k != "samples"}
            for row in ((report.get("rtt") or {}).get("rows") or [])
        ],
        "runner_only_s": (report.get("runner_only") or {}).get("wall_seconds"),
        "box_only_s": (report.get("box_only") or {}).get("wall_seconds"),
        "combined_s": (report.get("combined") or {}).get("wall_seconds"),
        "midrun_correct": (report.get("midrun") or {}).get("correct"),
        "midrun_injected": (report.get("midrun") or {}).get("injected"),
        "midrun_wall_s": (report.get("midrun") or {}).get("wall_seconds"),
        "midrun_post_kill_s": (report.get("midrun") or {}).get("post_kill_seconds"),
        "recovery_s": (report.get("recovery") or {}).get("recover_seconds_from_restart"),
        "box_alive_after_close": (report.get("recovery") or {}).get("box_worker_alive_after_session_close"),
        "elapsed_seconds": report["elapsed_seconds"],
    }
    (OUT_DIR / "bridge-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (OUT_DIR / "bridge-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("BRIDGE_SUMMARY", json.dumps(summary, sort_keys=True), flush=True)
    if not report["correct"] or report.get("rtt_exit") not in (0, None):
        raise SystemExit(1)


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
