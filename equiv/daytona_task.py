#!/usr/bin/env python3
"""In-sandbox helper for the Daytona pull pool. No network secrets required."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

# oracle.py is uploaded next to this file.
from oracle import count_primes


def work_loop(payload: bytes, work_ms: int) -> None:
    if work_ms <= 0:
        return
    deadline = time.perf_counter() + (work_ms / 1000.0)
    block = payload
    n = 0
    while time.perf_counter() < deadline:
        block = hashlib.sha256(block).digest()
        n += 1
        if n % 64 == 0 and time.perf_counter() >= deadline:
            break


def hash_bench(seconds: float = 20.0) -> dict:
    block = os.urandom(65536)
    digest = block
    n = 0
    t0 = time.perf_counter()
    while True:
        digest = hashlib.sha256(digest + block).digest()
        n += 1
        elapsed = time.perf_counter() - t0
        if elapsed >= seconds:
            break
    return {
        "hash_seconds": elapsed,
        "hashes": n,
        "hashes_per_second": n / elapsed if elapsed else 0.0,
        "algorithm": "sha256",
        "block_bytes": 65536,
    }


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def cgroup_limits() -> dict:
    out = {
        "cpu_max": _read("/sys/fs/cgroup/cpu.max"),
        "cpu_cfs_quota_us": _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"),
        "cpu_cfs_period_us": _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"),
        "memory_max": _read("/sys/fs/cgroup/memory.max"),
        "memory_limit_in_bytes": _read("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    }
    return out


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def meminfo() -> dict:
    info = {"mem_total_kb": None, "mem_available_kb": None}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                info["mem_total_kb"] = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                info["mem_available_kb"] = int(line.split()[1])
    except OSError:
        pass
    return info


def https_probe(url: str, timeout: float = 8.0) -> dict:
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "runner-probe"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            resp.read(256)
        return {
            "ok": True,
            "reachable": True,
            "status": int(status),
            "ms": (time.perf_counter() - t0) * 1000,
            "error": None,
            "url": url,
        }
    except urllib.error.HTTPError as exc:
        return {
            "ok": True,
            "reachable": True,
            "status": int(exc.code),
            "ms": (time.perf_counter() - t0) * 1000,
            "error": f"HTTPError {exc.code}",
            "url": url,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reachable": False,
            "status": None,
            "ms": (time.perf_counter() - t0) * 1000,
            "error": f"{type(exc).__name__}: {exc}",
            "url": url,
        }


def nproc() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def run_probe(hash_seconds: float, upstash_url: str) -> dict:
    return {
        "nproc": nproc(),
        "os_cpu_count": os.cpu_count(),
        "cpu_model": cpu_model(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "memory": meminfo(),
        "cgroup": cgroup_limits(),
        "hash": hash_bench(hash_seconds),
        "reachability": {
            "https_github_com": https_probe("https://github.com"),
            "https_pypi_org": https_probe("https://pypi.org"),
            "https_upstash_box": https_probe(upstash_url),
        },
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--lo", type=int)
    p.add_argument("--hi", type=int)
    p.add_argument("--work-ms", type=int, default=0)
    p.add_argument("--name", default="daytona")
    p.add_argument("--id", type=int, default=0)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--hash-seconds", type=float, default=20.0)
    p.add_argument("--upstash-url", default="https://us-east-1.box.upstash.com")
    args = p.parse_args()
    if args.probe:
        print(json.dumps(run_probe(args.hash_seconds, args.upstash_url), separators=(",", ":")))
        return
    if args.lo is None or args.hi is None:
        raise SystemExit("need --lo/--hi or --probe")
    count = count_primes(args.lo, args.hi)
    work_loop(f"{args.name}:{args.id}:{args.lo}:{args.hi}:{count}".encode(), args.work_ms)
    print(json.dumps({"count": count}, separators=(",", ":")))


if __name__ == "__main__":
    main()
