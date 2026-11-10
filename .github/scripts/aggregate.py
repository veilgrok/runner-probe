#!/usr/bin/env python3
"""Collect per-job probe.json files into summary.json and compute max concurrency."""

from __future__ import annotations

import json
import statistics
from pathlib import Path


def load_jobs(root: Path) -> list[dict]:
    jobs = []
    for path in sorted(root.rglob("probe.json")):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            jobs.append({"error": f"invalid JSON in {path}", "path": str(path)})
    return jobs


def max_concurrent(jobs: list[dict]) -> dict:
    events: list[tuple[str, int, int]] = []
    usable = []
    for i, job in enumerate(jobs):
        start = job.get("job_start_time")
        end = job.get("job_end_time")
        if not start or not end:
            continue
        usable.append(job)
        events.append((start, 1, i))
        events.append((end, -1, i))
    # At a shared timestamp, process ends before starts so a handoff is not counted as overlap.
    events.sort(key=lambda x: (x[0], x[1], x[2]))
    cur = 0
    peak = 0
    peak_at = None
    for ts, delta, _ in events:
        cur += delta
        if cur > peak:
            peak = cur
            peak_at = ts
    return {"max_concurrent": peak, "peak_at": peak_at, "jobs_with_times": len(usable)}


def median(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.median(vals))


def main() -> None:
    root = Path("artifacts")
    if not root.exists():
        root = Path(".")
    jobs = load_jobs(root)
    jobs_ok = [j for j in jobs if "job_start_time" in j]
    queues = [j["queue_seconds"] for j in jobs_ok if isinstance(j.get("queue_seconds"), (int, float))]
    hps = [j["hashes_per_second"] for j in jobs_ok if isinstance(j.get("hashes_per_second"), (int, float))]
    conc = max_concurrent(jobs_ok)

    def uniq(key):
        vals = []
        for j in jobs_ok:
            v = j.get(key)
            if v is not None and v not in vals:
                vals.append(v)
        return vals

    summary = {
        "job_count": len(jobs_ok),
        "artifact_files": len(jobs),
        "max_concurrent": conc["max_concurrent"],
        "peak_at": conc["peak_at"],
        "queue_seconds": {
            "min": min(queues) if queues else None,
            "median": median(queues),
            "max": max(queues) if queues else None,
            "all": queues,
        },
        "nproc_values": uniq("nproc"),
        "cpu_models": uniq("cpu_model"),
        "hashes_per_second": {
            "min": min(hps) if hps else None,
            "median": median(hps),
            "max": max(hps) if hps else None,
        },
        "reachability": {
            "https_api_github_com_ok": sum(1 for j in jobs_ok if ((j.get("reachability") or {}).get("https_api_github_com") or {}).get("ok")),
            "https_upstash_box_ok": sum(1 for j in jobs_ok if ((j.get("reachability") or {}).get("https_upstash_box") or {}).get("ok")),
            "dns_ok": sum(1 for j in jobs_ok if ((j.get("reachability") or {}).get("dns") or {}).get("ok")),
            "udp_stun_ok": sum(1 for j in jobs_ok if ((j.get("reachability") or {}).get("udp") or {}).get("stun", {}).get("ok")),
            "udp_dns_ok": sum(1 for j in jobs_ok if ((j.get("reachability") or {}).get("udp") or {}).get("dns_udp", {}).get("ok")),
            "total": len(jobs_ok),
        },
        "memory_raw": [((j.get("memory") or {}).get("raw")) for j in jobs_ok],
        "disk_raw": [((j.get("disk_root") or {}).get("raw")) for j in jobs_ok],
        "jobs": jobs_ok,
    }
    Path("summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    public = {k: v for k, v in summary.items() if k not in ("jobs", "memory_raw", "disk_raw", "queue_seconds")}
    public["queue_seconds"] = {k: summary["queue_seconds"][k] for k in ("min", "median", "max")}
    print(json.dumps(public, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
