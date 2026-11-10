#!/usr/bin/env python3
"""One GitHub Actions runner probe. Writes probe.json. No secrets involved."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(ts: str) -> datetime:
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def nproc() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def meminfo() -> dict:
    raw = ""
    try:
        raw = subprocess_text(["free", "-h"])
    except Exception as exc:
        raw = f"free failed: {type(exc).__name__}"
    info = {"raw": raw, "mem_total_kb": None, "mem_available_kb": None}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                info["mem_total_kb"] = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                info["mem_available_kb"] = int(line.split()[1])
    except OSError:
        pass
    return info


def disk_root() -> dict:
    raw = ""
    try:
        raw = subprocess_text(["df", "-h", "/"])
    except Exception as exc:
        raw = f"df failed: {type(exc).__name__}"
    st = os.statvfs("/")
    return {
        "raw": raw,
        "size_bytes": st.f_frsize * st.f_blocks,
        "free_bytes": st.f_frsize * st.f_bavail,
    }


def subprocess_text(cmd: list[str]) -> str:
    import subprocess

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return (r.stdout or r.stderr or "").rstrip()


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


def https_probe(url: str, timeout: float = 8.0) -> dict:
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "runner-probe"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            resp.read(256)
        return {"ok": True, "reachable": True, "status": int(status), "ms": (time.perf_counter() - t0) * 1000, "error": None, "url": url}
    except urllib.error.HTTPError as exc:
        # TLS and HTTP connected; a 401/404 still counts as reachable.
        return {"ok": True, "reachable": True, "status": int(exc.code), "ms": (time.perf_counter() - t0) * 1000, "error": f"HTTPError {exc.code}", "url": url}
    except Exception as exc:
        return {"ok": False, "reachable": False, "status": None, "ms": (time.perf_counter() - t0) * 1000, "error": f"{type(exc).__name__}: {exc}", "url": url}


def dns_probe(host: str = "api.github.com") -> dict:
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addrs = sorted({item[4][0] for item in infos})
        return {"ok": True, "host": host, "addresses": addrs, "ms": (time.perf_counter() - t0) * 1000, "error": None}
    except Exception as exc:
        return {"ok": False, "host": host, "addresses": [], "ms": (time.perf_counter() - t0) * 1000, "error": f"{type(exc).__name__}: {exc}"}


def udp_probe() -> dict:
    """Outbound UDP notes only. Inbound UDP is not tested; this is not a bypass."""
    note = (
        "Outbound UDP datagrams were sent from this runner. A reply means outbound "
        "UDP and a response path worked for that destination. No reply means only "
        "that this destination did not answer; it does not prove every UDP path is blocked, "
        "and this probe does not attempt to bypass any network policy."
    )
    results = {"note": note, "stun": None, "dns_udp": None}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    try:
        # STUN binding request (RFC 5389).
        txid = os.urandom(12)
        req = (1).to_bytes(2, "big") + (0).to_bytes(2, "big") + (0x2112A442).to_bytes(4, "big") + txid
        sock.sendto(req, ("stun.l.google.com", 19302))
        data, addr = sock.recvfrom(2048)
        results["stun"] = {"ok": True, "reply_from": list(addr), "bytes": len(data), "error": None}
    except Exception as exc:
        results["stun"] = {"ok": False, "reply_from": None, "bytes": 0, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        sock.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    try:
        # Minimal DNS query for example.com A via 1.1.1.1
        q = (0x1234).to_bytes(2, "big") + (0x0100).to_bytes(2, "big") + (1).to_bytes(2, "big") + (0).to_bytes(6, "big")
        q += b"\x07example\x03com\x00" + (1).to_bytes(2, "big") + (1).to_bytes(2, "big")
        sock.sendto(q, ("1.1.1.1", 53))
        data, addr = sock.recvfrom(2048)
        results["dns_udp"] = {"ok": True, "reply_from": list(addr), "bytes": len(data), "error": None}
    except Exception as exc:
        results["dns_udp"] = {"ok": False, "reply_from": None, "bytes": 0, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        sock.close()
    return results


def main() -> None:
    dispatch = os.environ.get("DISPATCH_TIME") or ""
    start = utc_now()
    queue_seconds = None
    if dispatch:
        try:
            queue_seconds = (parse_iso(start) - parse_iso(dispatch)).total_seconds()
        except ValueError:
            queue_seconds = None

    result = {
        "matrix_index": int(os.environ.get("MATRIX_INDEX") or "0"),
        "pool": os.environ.get("PROBE_POOL") or "",
        "resource_class": os.environ.get("RESOURCE_CLASS") or "",
        "runner_name": os.environ.get("RUNNER_NAME") or "",
        "runner_os": os.environ.get("RUNNER_OS") or platform.system(),
        "runner_arch": os.environ.get("RUNNER_ARCH") or platform.machine(),
        "dispatch_time": dispatch or None,
        "job_start_time": start,
        "queue_seconds": queue_seconds,
        "nproc": nproc(),
        "cpu_model": cpu_model(),
        "memory": meminfo(),
        "disk_root": disk_root(),
        "reachability": {
            "https_api_github_com": https_probe("https://api.github.com"),
            "https_upstash_box": https_probe("https://us-east-1.box.upstash.com"),
            "dns": dns_probe("api.github.com"),
            "udp": udp_probe(),
        },
    }
    result.update(hash_bench(20.0))
    result["job_end_time"] = utc_now()
    Path("probe.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    # Brief log line without large address lists.
    summary = {
        "matrix_index": result["matrix_index"],
        "runner_name": result["runner_name"],
        "queue_seconds": result["queue_seconds"],
        "nproc": result["nproc"],
        "cpu_model": result["cpu_model"],
        "hashes_per_second": result["hashes_per_second"],
        "https_github": result["reachability"]["https_api_github_com"]["ok"],
        "https_upstash": result["reachability"]["https_upstash_box"]["ok"],
    }
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
