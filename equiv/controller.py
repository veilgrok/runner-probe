#!/usr/bin/env python3
"""UDP task dispatcher for the labeled equivalent prime-counting workload."""

from __future__ import annotations

import argparse
import json
import socket
import time
from typing import Any


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--listen", default="127.0.0.1:7700")
    p.add_argument("--mode", required=True)
    p.add_argument("--intervals", required=True, help="JSON list of [lo, hi] pairs")
    p.add_argument("--expected", required=True, help="JSON list of integer counts")
    p.add_argument("--verify-frac", type=float, default=0.2)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--ready-for", default="", help="comma-separated worker names required before starting")
    p.add_argument("--ready-timeout", type=float, default=30)
    p.add_argument("--heartbeat-timeout", type=float, default=4.0)
    p.add_argument("--status-path", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--no-bye", action="store_true", help="Do not send bye when finishing, so the worker can be reused")
    args = p.parse_args()

    host, port = args.listen.rsplit(":", 1)
    intervals: list[tuple[int, int]] = [tuple(x) for x in json.loads(args.intervals)]
    expected: list[int] = json.loads(args.expected)
    if len(expected) != len(intervals):
        raise SystemExit("expected length does not match intervals")
    required = [n for n in args.ready_for.split(",") if n]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, int(port)))
    sock.settimeout(0.2)

    workers: dict[str, dict[str, Any]] = {}
    pending: list[int] = list(range(len(intervals)))
    inflight: dict[int, dict[str, Any]] = {}
    results: dict[int, dict[str, Any]] = {}
    verify_queue: list[int] = []
    verifies_done = 0
    mismatches = 0
    retries = 0
    events: list[dict[str, Any]] = []
    started = False
    start = 0.0
    deadline = time.perf_counter() + args.timeout
    ready_deadline = time.perf_counter() + args.ready_timeout

    def log_event(kind: str, **kw: Any) -> None:
        events.append({"t": time.time(), "elapsed": (time.perf_counter() - start) if started else 0, "kind": kind, **kw})

    def send(addr: tuple[str, int], msg: dict[str, Any]) -> None:
        sock.sendto(json.dumps(msg, separators=(",", ":")).encode("utf-8"), addr)

    def write_status() -> None:
        if not args.status_path:
            return
        payload = {
            "mode": args.mode,
            "started": started,
            "workers": {
                name: {
                    "completed": w["completed"],
                    "busy": w["busy"],
                    "alive": (time.perf_counter() - w["last"]) < args.heartbeat_timeout,
                    "last_ago": time.perf_counter() - w["last"],
                }
                for name, w in workers.items()
            },
            "pending": len(pending),
            "inflight": len(inflight),
            "done": len(results),
            "retries": retries,
            "mismatches": mismatches,
        }
        with open(args.status_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def assign() -> None:
        nonlocal retries, verifies_done
        idle = [n for n, w in workers.items() if w["busy"] is None and (time.perf_counter() - w["last"]) < args.heartbeat_timeout]
        while pending and idle:
            tid = pending.pop(0)
            name = idle.pop(0)
            lo, hi = intervals[tid]
            workers[name]["busy"] = tid
            inflight[tid] = {"name": name, "sent": time.perf_counter(), "lo": lo, "hi": hi, "verify_of": None}
            send(workers[name]["addr"], {"t": "task", "id": tid, "lo": lo, "hi": hi})
        while verify_queue and idle:
            tid = verify_queue.pop(0)
            name = idle.pop(0)
            lo, hi = intervals[tid]
            vid = 1_000_000 + tid
            if any(m.get("verify_of") == tid for m in inflight.values()):
                continue
            workers[name]["busy"] = vid
            inflight[vid] = {"name": name, "sent": time.perf_counter(), "lo": lo, "hi": hi, "verify_of": tid}
            send(workers[name]["addr"], {"t": "task", "id": vid, "lo": lo, "hi": hi})
            verifies_done += 1

    def reassign_stale() -> None:
        nonlocal retries
        now = time.perf_counter()
        stale: list[int] = []
        for tid, meta in list(inflight.items()):
            name = meta["name"]
            w = workers.get(name)
            silent = w is None or (now - w["last"]) >= args.heartbeat_timeout
            overdue = (now - meta["sent"]) >= args.heartbeat_timeout + 15
            if silent or overdue:
                stale.append(tid)
        for tid in stale:
            meta = inflight.pop(tid)
            name = meta["name"]
            if name in workers and workers[name]["busy"] == tid:
                workers[name]["busy"] = None
            if meta.get("verify_of") is None and tid not in results:
                pending.append(tid)
                retries += 1
                log_event("reassign", id=tid, from_worker=name)
        if stale:
            assign()

    def maybe_verify(tid: int, from_name: str) -> None:
        if args.verify_frac <= 0:
            return
        # Deterministic 20% check: verify when id % 5 == 0, matching a 0.2 fraction without RNG.
        if (tid % 5) != 0:
            return
        if tid in verify_queue or any(m.get("verify_of") == tid for m in inflight.values()):
            return
        # Need at least one other worker in the pool; it may be busy right now.
        if not any(n != from_name for n in workers):
            return
        verify_queue.append(tid)

    while time.perf_counter() < deadline:
        if not started and required:
            present = [n for n in required if n in workers and (time.perf_counter() - workers[n]["last"]) < args.heartbeat_timeout]
            if set(present) >= set(required):
                started = True
                start = time.perf_counter()
                log_event("start", workers=present)
                assign()
            elif time.perf_counter() > ready_deadline:
                break
        elif not started and not required:
            if workers:
                started = True
                start = time.perf_counter()
                log_event("start", workers=list(workers))
                assign()
            elif time.perf_counter() > ready_deadline:
                break

        if started and not pending and not verify_queue and not inflight and len(results) == len(intervals):
            break

        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            reassign_stale()
            write_status()
            continue
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        typ = msg.get("t")
        name = str(msg.get("name") or "")
        if typ == "hello" and name:
            if name not in workers:
                workers[name] = {"addr": addr, "last": time.perf_counter(), "busy": None, "completed": 0}
                log_event("hello", name=name)
            else:
                workers[name]["addr"] = addr
                workers[name]["last"] = time.perf_counter()
            if started:
                assign()
        elif typ == "result" and name:
            tid = int(msg["id"])
            count = int(msg["count"])
            if name in workers:
                workers[name]["last"] = time.perf_counter()
                if workers[name]["busy"] == tid:
                    workers[name]["busy"] = None
                workers[name]["completed"] += 1
            meta = inflight.pop(tid, None)
            if meta and meta.get("verify_of") is not None:
                orig = meta["verify_of"]
                if orig in results and results[orig]["count"] != count:
                    mismatches += 1
                    log_event("mismatch", id=orig, first=results[orig]["count"], second=count)
            elif tid < len(intervals) and tid not in results:
                results[tid] = {"count": count, "name": name}
                maybe_verify(tid, name)
            assign()
        write_status()
        reassign_stale()

    wall = (time.perf_counter() - start) if started else None
    counts = [results[i]["count"] if i in results else None for i in range(len(intervals))]
    correct = counts == expected
    by_worker: dict[str, int] = {}
    for row in results.values():
        by_worker[row["name"]] = by_worker.get(row["name"], 0) + 1

    # Tell live workers to exit unless the caller wants to reuse them.
    if not args.no_bye:
        for w in workers.values():
            try:
                send(w["addr"], {"t": "bye"})
            except OSError:
                pass

    out = {
        "mode": args.mode,
        "started": started,
        "wall_seconds": wall,
        "timeout": time.perf_counter() >= deadline and not correct,
        "tasks": len(intervals),
        "completed": len(results),
        "correct": correct,
        "mismatches": mismatches,
        "retries": retries,
        "verifies": verifies_done,
        "by_worker": by_worker,
        "workers_seen": list(workers),
        "events": events,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(json.dumps({k: v for k, v in out.items() if k != "events"}, sort_keys=True))
    if not correct or not started:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
