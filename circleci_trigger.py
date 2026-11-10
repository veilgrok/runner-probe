#!/usr/bin/env python3
"""Trigger a runner-probe CircleCI pipeline and collect job timings.

Does not print the API token. Does nothing unless you pass --confirm
(or omit --dry-run and explicitly run it). Default is to require the
token file and --confirm so a casual invocation cannot burn credits.

Token file default: /home/box/agent-data/secrets/circleci-veilgrok.token
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_TOKEN = Path("/home/box/agent-data/secrets/circleci-veilgrok.token")
DEFAULT_PROJECT = "gh/veilgrok/runner-probe"
API = "https://circleci.com/api/v2"
TERMINAL = {"success", "failed", "error", "canceled", "cancelled", "unauthorized", "not_run"}


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


def read_token(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(
            f"CircleCI token file not found at {path}. "
            "Create a personal API token in CircleCI User Settings and write it to that path (mode 600)."
        )
    token = path.read_text(encoding="utf-8").strip().strip("\r")
    if not token:
        raise SystemExit(f"CircleCI token file at {path} is empty.")
    return token


def api(token: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | None, str]:
    url = path if path.startswith("http") else API + path
    data = None
    headers = {
        "Circle-Token": token,
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else None
            return resp.status, parsed, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        parsed = None
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            pass
        return exc.code, parsed, raw


def seconds_between(start: str | None, end: str | None) -> float | None:
    a, b = parse_iso(start), parse_iso(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def collect_jobs(token: str, pipeline_id: str) -> list[dict]:
    status, payload, raw = api(token, "GET", f"/pipeline/{pipeline_id}/workflow")
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(f"failed to list workflows (HTTP {status}): {raw[:500]}")
    jobs: list[dict] = []
    for wf in payload.get("items") or []:
        wf_id = wf.get("id")
        st, job_payload, job_raw = api(token, "GET", f"/workflow/{wf_id}/job")
        if st != 200 or not isinstance(job_payload, dict):
            jobs.append(
                {
                    "workflow": wf.get("name"),
                    "workflow_id": wf_id,
                    "workflow_status": wf.get("status"),
                    "error": f"failed to list jobs (HTTP {st})",
                }
            )
            continue
        for job in job_payload.get("items") or []:
            number = job.get("job_number")
            slug = job.get("project_slug") or DEFAULT_PROJECT
            extra = {}
            if number is not None:
                # v2 job-by-number (queued_at when present) then v1.1 fallback.
                code, detail, _ = api(token, "GET", f"/project/{slug}/job/{number}")
                if code != 200 or not isinstance(detail, dict):
                    code, detail, _ = api(
                        token,
                        "GET",
                        f"https://circleci.com/api/v1.1/project/{slug.replace('gh/', 'github/', 1)}/{number}",
                    )
                if code == 200 and isinstance(detail, dict):
                    extra = {
                        "queued_at": detail.get("queued_at") or detail.get("queued_time"),
                        "start_time": detail.get("start_time") or detail.get("started_at"),
                        "stop_time": detail.get("stop_time") or detail.get("stopped_at"),
                        "build_time_millis": detail.get("build_time_millis"),
                    }
            started = job.get("started_at") or extra.get("start_time")
            stopped = job.get("stopped_at") or extra.get("stop_time")
            queued = extra.get("queued_at") or wf.get("created_at")
            jobs.append(
                {
                    "workflow": wf.get("name"),
                    "workflow_id": wf_id,
                    "workflow_status": wf.get("status"),
                    "workflow_created_at": wf.get("created_at"),
                    "name": job.get("name"),
                    "id": job.get("id"),
                    "job_number": number,
                    "status": job.get("status"),
                    "type": job.get("type"),
                    "queued_at": queued,
                    "started_at": started,
                    "stopped_at": stopped,
                    "queue_seconds": seconds_between(queued, started),
                    "run_seconds": seconds_between(started, stopped),
                    "build_time_millis": extra.get("build_time_millis"),
                }
            )
    return jobs


def pipeline_done(jobs: list[dict], workflows: list[dict] | None = None) -> bool:
    if not jobs:
        return False
    for row in jobs:
        st = (row.get("status") or row.get("workflow_status") or "").lower()
        if st and st not in TERMINAL and st != "not_run":
            return False
        if not st:
            return False
    return True


def download_artifacts(token: str, jobs: list[dict], dest: Path) -> list[str]:
    saved: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for job in jobs:
        number = job.get("job_number")
        slug = DEFAULT_PROJECT
        if number is None:
            continue
        code, payload, _ = api(token, "GET", f"/project/{slug}/{number}/artifacts")
        items = []
        if code == 200:
            if isinstance(payload, dict):
                items = payload.get("items") or []
            elif isinstance(payload, list):
                items = payload
        for art in items:
            url = art.get("url")
            path = art.get("path") or "artifact"
            if not url:
                continue
            # Artifact URLs require the same token. Do not log the URL if it
            # could contain a query token; CircleCI artifact URLs usually do not,
            # but we still avoid printing them.
            req = urllib.request.Request(url, headers={"Circle-Token": token})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read()
            except urllib.error.HTTPError:
                continue
            target = dest / f"job-{number}" / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            saved.append(str(target.relative_to(dest)))
    return saved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="CircleCI project slug, e.g. gh/veilgrok/runner-probe")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--jobs", type=int, default=10, help="Parallel docker/medium jobs (1-20)")
    parser.add_argument("--bridge", action="store_true", help="Trigger the Upstash bridge workflow instead of the probe")
    parser.add_argument("--dispatch-time", default="", help="UTC ISO-8601 timestamp; default is now")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--results-dir", type=Path, default=Path("/workspace/veil-lab/gh-actions/results"))
    parser.add_argument("--dry-run", action="store_true", help="Print the trigger payload and exit")
    parser.add_argument("--confirm", action="store_true", help="Required to actually trigger a pipeline")
    args = parser.parse_args()

    n = max(1, min(20, int(args.jobs)))
    when = args.dispatch_time.strip() or utc_now()
    parameters = {
        "run_probe": not args.bridge,
        "run_bridge": bool(args.bridge),
        "jobs": n,
        "dispatch_time": when,
    }
    body = {"branch": args.branch, "parameters": parameters}
    print(json.dumps({"project": args.project, "payload": body}, indent=2))
    if args.dry_run:
        print("dry-run: no pipeline triggered")
        return
    if not args.confirm:
        print("Refusing to trigger. Pass --confirm after the token file exists, or --dry-run to preview.", file=sys.stderr)
        raise SystemExit(2)

    token = read_token(args.token_file)
    slug = urllib.parse.quote(args.project, safe="/")
    status, payload, raw = api(token, "POST", f"/project/{slug}/pipeline", body)
    if status not in (200, 201) or not isinstance(payload, dict) or not payload.get("id"):
        # Do not dump the whole body in case a future error includes auth material.
        msg = ""
        if isinstance(payload, dict):
            msg = payload.get("message") or payload.get("error") or f"HTTP {status}"
        else:
            msg = f"HTTP {status}"
        raise SystemExit(f"pipeline trigger failed: {msg}")

    pipeline_id = payload["id"]
    number = payload.get("number")
    created = payload.get("created_at")
    print(json.dumps({"pipeline_id": pipeline_id, "number": number, "created_at": created, "state": payload.get("state")}))

    deadline = time.time() + args.timeout
    jobs: list[dict] = []
    while time.time() < deadline:
        time.sleep(args.poll_seconds)
        jobs = collect_jobs(token, pipeline_id)
        statuses = sorted({(j.get("name") or "?") + "=" + str(j.get("status") or j.get("workflow_status")) for j in jobs})
        print("status:", ", ".join(statuses) if statuses else "(waiting for workflows)")
        if jobs and pipeline_done(jobs):
            break
    else:
        print("timed out waiting for the pipeline to finish", file=sys.stderr)

    dest = args.results_dir / f"circleci-{number or pipeline_id}"
    dest.mkdir(parents=True, exist_ok=True)
    summary = {
        "project": args.project,
        "pipeline_id": pipeline_id,
        "number": number,
        "created_at": created,
        "dispatch_time": when,
        "parameters": parameters,
        "jobs": jobs,
        "queue_seconds": {
            "min": min((j["queue_seconds"] for j in jobs if isinstance(j.get("queue_seconds"), (int, float))), default=None),
            "max": max((j["queue_seconds"] for j in jobs if isinstance(j.get("queue_seconds"), (int, float))), default=None),
        },
    }
    (dest / "timings.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    saved = download_artifacts(token, jobs, dest / "artifacts")
    summary["artifacts"] = saved
    (dest / "timings.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    public = {k: v for k, v in summary.items() if k != "jobs"}
    public["job_count"] = len(jobs)
    public["job_statuses"] = {j.get("name"): j.get("status") for j in jobs}
    print(json.dumps(public, indent=2, sort_keys=True))
    print(f"Wrote timings to {dest / 'timings.json'}")
    failed = [j for j in jobs if (j.get("status") or "").lower() in {"failed", "error", "unauthorized"}]
    if failed or not jobs:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
