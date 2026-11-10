#!/usr/bin/env python3
"""Trigger a runner-probe Buildkite build and collect job timings.

Does not print the API token. Requires --confirm to actually create a build.

Token file default: /home/box/agent-data/secrets/buildkite-veilgrok.token
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_TOKEN = Path("/home/box/agent-data/secrets/buildkite-veilgrok.token")
API = "https://api.buildkite.com/v2"
TERMINAL = {"passed", "failed", "canceled", "cancelled", "skipped", "not_run", "blocked"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except ValueError:
        return None


def seconds_between(start: str | None, end: str | None) -> float | None:
    a, b = parse_iso(start), parse_iso(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def read_token(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(
            f"Buildkite token file not found at {path}. "
            "Create an API token (read/write builds) in Buildkite Personal Settings and write it there (mode 600)."
        )
    token = path.read_text(encoding="utf-8").strip().strip("\r")
    if not token:
        raise SystemExit(f"Buildkite token file at {path} is empty.")
    return token


def api(token: str, method: str, path: str, body: dict | None = None) -> tuple[int, object, str]:
    url = path if path.startswith("http") else API + path
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed = None
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            pass
        return exc.code, parsed, raw


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument("--org", default="veilgrok", help="Buildkite organization slug")
    parser.add_argument("--pipeline", default="runner-probe", help="Buildkite pipeline slug")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--bridge", action="store_true")
    parser.add_argument("--queue", default="hosted", help="Hosted or self-hosted queue name (BUILDKITE_QUEUE)")
    parser.add_argument("--dispatch-time", default="")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--results-dir", type=Path, default=Path("/workspace/veil-lab/gh-actions/results"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    n = max(1, min(20, int(args.jobs)))
    when = args.dispatch_time.strip() or utc_now()
    env = {
        "RUN_PROBE": "0" if args.bridge else "1",
        "RUN_BRIDGE": "1" if args.bridge else "0",
        "JOBS": str(n),
        "DISPATCH_TIME": when,
        "BUILDKITE_QUEUE": args.queue,
    }
    body = {
        "commit": args.commit,
        "branch": args.branch,
        "message": "bridge" if args.bridge else f"probe jobs={n}",
        "env": env,
    }
    print(json.dumps({"org": args.org, "pipeline": args.pipeline, "payload": body}, indent=2))
    if args.dry_run:
        print("dry-run: no build triggered")
        return
    if not args.confirm:
        print("Refusing to trigger. Pass --confirm after the token file exists, or --dry-run to preview.", file=sys.stderr)
        raise SystemExit(2)

    token = read_token(args.token_file)
    path = f"/organizations/{args.org}/pipelines/{args.pipeline}/builds"
    status, payload, raw = api(token, "POST", path, body)
    if status not in (200, 201) or not isinstance(payload, dict) or not payload.get("number"):
        msg = payload.get("message") if isinstance(payload, dict) else f"HTTP {status}"
        raise SystemExit(f"build trigger failed: {msg}")

    number = payload["number"]
    build_url = payload.get("web_url")
    print(json.dumps({"number": number, "web_url": build_url, "state": payload.get("state")}))

    deadline = time.time() + args.timeout
    build: dict = payload
    while time.time() < deadline:
        time.sleep(args.poll_seconds)
        st, build, _ = api(token, "GET", f"{path}/{number}")
        if st != 200 or not isinstance(build, dict):
            print(f"poll failed HTTP {st}")
            continue
        state = build.get("state")
        jobs = build.get("jobs") or []
        print(f"state={state} jobs=" + ", ".join(f"{j.get('name') or j.get('label') or j.get('type')}={j.get('state')}" for j in jobs[:20]))
        if state in TERMINAL:
            break
    else:
        print("timed out waiting for the build to finish", file=sys.stderr)

    job_rows = []
    for job in build.get("jobs") or []:
        created = job.get("created_at") or build.get("created_at")
        started = job.get("started_at")
        finished = job.get("finished_at") or job.get("finished_at")
        job_rows.append(
            {
                "id": job.get("id"),
                "name": job.get("name") or job.get("label") or job.get("type"),
                "type": job.get("type"),
                "state": job.get("state"),
                "created_at": created,
                "started_at": started,
                "finished_at": finished,
                "queue_seconds": seconds_between(created, started),
                "run_seconds": seconds_between(started, finished),
                "agent": (job.get("agent") or {}).get("name") if isinstance(job.get("agent"), dict) else None,
            }
        )

    dest = args.results_dir / f"buildkite-{number}"
    dest.mkdir(parents=True, exist_ok=True)
    summary = {
        "org": args.org,
        "pipeline": args.pipeline,
        "number": number,
        "web_url": build_url,
        "state": build.get("state"),
        "created_at": build.get("created_at"),
        "dispatch_time": when,
        "env": env,
        "jobs": job_rows,
        "queue_seconds": {
            "min": min((j["queue_seconds"] for j in job_rows if isinstance(j.get("queue_seconds"), (int, float))), default=None),
            "max": max((j["queue_seconds"] for j in job_rows if isinstance(j.get("queue_seconds"), (int, float))), default=None),
        },
    }
    (dest / "timings.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    public = {k: v for k, v in summary.items() if k != "jobs"}
    public["job_count"] = len(job_rows)
    public["job_states"] = {j.get("name"): j.get("state") for j in job_rows}
    print(json.dumps(public, indent=2, sort_keys=True))
    print(f"Wrote timings to {dest / 'timings.json'}")
    if (build.get("state") or "") not in {"passed", "skipped"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
