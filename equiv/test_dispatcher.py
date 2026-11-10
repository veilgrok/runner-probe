#!/usr/bin/env python3
"""Local tests for the overflow dispatcher. Simulated pools only. No remote APIs."""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from dispatcher import Dispatcher, spawn_allocated, teardown_all
from oracle import count_primes
from pools import (
    DEFAULT_ORDER,
    DaytonaPool,
    E2BPool,
    RemotePool,
    SimulatedPool,
    allocate,
    default_remote_pools,
    simulated_pools,
)


FAILED = 0


def check(cond: bool, name: str, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"ok  {name}")
        return
    FAILED += 1
    print(f"FAIL  {name}" + (f": {detail}" if detail else ""), file=sys.stderr)


def test_allocate() -> None:
    pools = simulated_pools(caps={"gha": 2, "circleci": 2, "buildkite": 1, "blacksmith": 1, "daytona": 1, "e2b": 1, "upstash_box": 1})
    plan = allocate(5, pools)
    check(plan["gha"] == 2, "allocate fills gha first")
    check(plan["circleci"] == 2, "allocate overflows to circleci")
    check(plan["buildkite"] == 1, "allocate overflows to buildkite")
    check(plan["blacksmith"] == 0, "allocate stops once demand is met")
    check(plan["daytona"] == 0, "allocate leaves daytona at 0 once demand is met")
    check(plan["e2b"] == 0, "allocate leaves e2b at 0 once demand is met")
    check(plan["upstash_box"] == 0, "allocate leaves later pools at 0")
    check(plan["_unassigned"] == 0, "allocate has no leftover for 5 of 9")
    leftover = allocate(20, pools)
    check(leftover["_unassigned"] == 11, "allocate reports unassigned demand", str(leftover))
    check(sum(leftover[n] for n in DEFAULT_ORDER) == 9, "allocate never exceeds total cap")


def test_remote_pools_refuse() -> None:
    for pool in default_remote_pools(allow_remote=False):
        raised = False
        try:
            pool.spawn(1, controller="127.0.0.1:9", work_ms=10, name_prefix="x")
        except RuntimeError as exc:
            raised = "disabled" in str(exc).lower() or "not enabled" in str(exc).lower()
            check(raised, f"{pool.name} refuses spawn without allow_remote", str(exc))
        else:
            check(False, f"{pool.name} refuses spawn without allow_remote", "spawn() returned")
    allowed = RemotePool("circleci", 1, allow_remote=True, how="manual trigger")
    try:
        allowed.spawn(1, controller="127.0.0.1:9", work_ms=10, name_prefix="x")
        check(False, "allow_remote still has no automatic trigger")
    except RuntimeError as exc:
        check("not enabled in this harness" in str(exc), "allow_remote still has no automatic trigger", str(exc))


def _small_workload(n: int = 12) -> tuple[list[tuple[int, int]], list[int]]:
    intervals = [(i * 200, (i + 1) * 200) for i in range(n)]
    expected = [count_primes(lo, hi) for lo, hi in intervals]
    return intervals, expected


def test_overflow_and_drain() -> None:
    intervals, expected = _small_workload(12)
    caps = {"gha": 2, "circleci": 2, "buildkite": 1, "blacksmith": 0, "daytona": 0, "e2b": 0, "upstash_box": 1}
    pools = simulated_pools(caps=caps)
    disp = Dispatcher(
        "127.0.0.1:0",
        intervals,
        expected,
        heartbeat_timeout=2.0,
        lease_seconds=4.0,
        verify_frac=0.0,
        timeout=30.0,
        ready_timeout=8.0,
    )
    handles = []
    try:
        plan, handles = spawn_allocated(pools, 6, controller=disp.bound, work_ms=40, name_prefix="ov")
        check(plan["gha"] == 2 and plan["circleci"] == 2 and plan["buildkite"] == 1 and plan["upstash_box"] == 1, "spawn plan uses overflow pools", json.dumps(plan))
        check(len(handles) == 6, "spawned 6 simulated workers", str(len(handles)))
        out = disp.run()
        check(out["started"], "dispatcher started")
        check(out["correct"], "results match the oracle", json.dumps({k: out[k] for k in ("completed", "correct", "by_pool")}))
        check(out["completed"] == 12, "all 12 tasks completed")
        check(out["retries"] == 0, "no unexpected reassigns on a clean run", str(out["retries"]))
        used = set(out.get("by_pool") or {})
        check("gha" in used and "circleci" in used, "more than one pool did work", str(out.get("by_pool")))
        # Give drain a moment to land, then assert the processes actually exited.
        deadline = time.time() + 5
        while time.time() < deadline and any(h.alive() for h in handles):
            time.sleep(0.05)
        still = [h.name for h in handles if h.alive()]
        check(not still, "workers self-exited when the queue was empty", f"still running: {still}")
        check(len(out["workers_exited"]) == len(handles), "dispatcher recorded a drain/bye for every worker", str(out["workers_exited"]))
    finally:
        disp.close()
        teardown_all(pools, handles, timeout=2.0)


def test_reassign_on_drop() -> None:
    intervals, expected = _small_workload(10)
    # Two workers in one pool plus one overflow worker, so a kill can be reassigned.
    pools = [
        SimulatedPool("gha", 2),
        SimulatedPool("circleci", 1),
    ]
    disp = Dispatcher(
        "127.0.0.1:0",
        intervals,
        expected,
        heartbeat_timeout=1.2,
        lease_seconds=2.0,
        verify_frac=0.0,
        timeout=30.0,
        ready_timeout=8.0,
        ready_for=["drop-gha-0", "drop-gha-1", "drop-circleci-0"],
    )
    handles = []
    try:
        plan, handles = spawn_allocated(pools, 3, controller=disp.bound, work_ms=250, name_prefix="drop")
        check(len(handles) == 3, "reassign test spawned 3 workers")
        # Pump until each required worker has finished at least one task, then SIGKILL one.
        ready_deadline = time.perf_counter() + 8
        run_deadline = time.perf_counter() + 30
        killed = None
        while disp.pump(ready_deadline=ready_deadline, run_deadline=run_deadline):
            done = {n: w["completed"] for n, w in disp.workers.items()}
            if killed is None and len(done) >= 3 and all(v >= 1 for v in done.values()):
                victim = next(h for h in handles if h.pool == "gha")
                if victim.proc is not None:
                    victim.proc.kill()
                    victim.proc.wait(timeout=2)
                    killed = victim.name
                    disp.log_event("injected_kill", name=killed)
            if disp._finished():
                break
        out = disp.summary()
        reassigns = [e for e in out["events"] if e.get("kind") == "reassign"]
        check(killed is not None, "killed a gha worker mid-run")
        check(bool(reassigns), "dispatcher reassigned the dropped lease", json.dumps(out["events"][-8:]))
        check(out["correct"], "results still match after a mid-run kill", json.dumps({k: out[k] for k in ("completed", "correct", "retries", "by_worker")}))
        check(out["retries"] >= 1, "retry count increased after the drop", str(out["retries"]))
        deadline = time.time() + 5
        # The killed proc is already dead; the others should drain.
        while time.time() < deadline and any(h.alive() for h in handles if h.name != killed):
            disp.drain_idle()
            time.sleep(0.05)
        leftover = [h.name for h in handles if h.alive()]
        check(not leftover, "surviving workers exited after drain", f"still running: {leftover}")
    finally:
        disp.close()
        teardown_all(pools, handles, timeout=2.0)



def test_daytona_and_e2b_refuse_without_allow_remote() -> None:
    for pool in (DaytonaPool(cap=1, allow_remote=False), E2BPool(cap=1, allow_remote=False)):
        try:
            pool.spawn(1, controller="127.0.0.1:9", work_ms=10, name_prefix="x")
            check(False, f"{pool.name} refuses spawn without allow_remote", "spawn() returned")
        except RuntimeError as exc:
            check("disabled" in str(exc).lower(), f"{pool.name} refuses spawn without allow_remote", str(exc))


def _run_fake_pull(pool, name: str) -> None:
    intervals, expected = _small_workload(8)
    disp = Dispatcher(
        "127.0.0.1:0",
        intervals,
        expected,
        heartbeat_timeout=2.0,
        lease_seconds=4.0,
        verify_frac=0.0,
        timeout=30.0,
        ready_timeout=8.0,
    )
    handles = []
    try:
        handles = pool.spawn(2, controller=disp.bound, work_ms=40, name_prefix=name)
        check(len(handles) == 2, f"{pool.name} fake spawn created 2 workers", str(len(handles)))
        out = disp.run()
        check(out["started"], f"{pool.name} fake pull dispatcher started")
        check(out["correct"], f"{pool.name} fake pull matches the oracle", str({k: out[k] for k in ("completed", "correct", "by_pool")}))
        check(out["completed"] == 8, f"{pool.name} fake pull completed 8 tasks")
        check((out.get("by_pool") or {}).get(pool.name) == 8, f"{pool.name} pool tag is set on results", str(out.get("by_pool")))
        deadline = time.time() + 5
        while time.time() < deadline and any(h.alive() for h in handles):
            time.sleep(0.05)
        still = [h.name for h in handles if h.alive()]
        check(not still, f"{pool.name} fake workers drained", f"still running: {still}")
    finally:
        disp.close()
        pool.teardown(handles, timeout=2.0)


def test_daytona_fake_pull() -> None:
    _run_fake_pull(DaytonaPool(cap=2, allow_remote=True, fake=True), "dt")


def test_e2b_fake_pull() -> None:
    _run_fake_pull(E2BPool(cap=2, allow_remote=True, fake=True), "eb")


def main() -> None:
    tests = [
        test_allocate,
        test_remote_pools_refuse,
        test_overflow_and_drain,
        test_reassign_on_drop,
        test_daytona_and_e2b_refuse_without_allow_remote,
        test_daytona_fake_pull,
        test_e2b_fake_pull,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:
            global FAILED
            FAILED += 1
            print(f"FAIL  {fn.__name__} raised:", file=sys.stderr)
            traceback.print_exc()
    if FAILED:
        print(f"{FAILED} failure(s)")
        raise SystemExit(1)
    print("all dispatcher tests passed")


if __name__ == "__main__":
    main()
