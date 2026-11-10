#!/usr/bin/env python3
"""Upload Buildkite probe/bridge/size steps. Only runs when RUN_PROBE, RUN_SIZES, or RUN_BRIDGE is 1."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


QUEUE_CLASS = {
    "hosted": "linux-amd64-small",
    "linux-small": "linux-amd64-small",
    "linux-medium": "linux-amd64-medium",
    "linux-large": "linux-amd64-large",
    "linux-xlarge": "linux-amd64-xlarge",
    "linux-arm64-small": "linux-arm64-small",
    "linux-arm64-medium": "linux-arm64-medium",
    "linux-arm64-large": "linux-arm64-large",
}


def yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "") + '"'


def probe_command(meta_key: str, indent: int = 8) -> list[str]:
    # Publish probe.json to build meta-data so we can read it with the
    # existing token (read_builds). The token cannot read artifacts or logs.
    pad = " " * indent
    body = " " * (indent + 2)
    return [
        f"{pad}command: |",
        f"{body}set +x",
        body + 'export RUNNER_NAME="buildkite-$${RESOURCE_CLASS}-$${BUILDKITE_BUILD_NUMBER:-0}-$${MATRIX_INDEX}"',
        body + 'export RUNNER_OS="$${RUNNER_OS:-Linux}"',
        body + 'export RUNNER_ARCH="$${RUNNER_ARCH:-$$(uname -m)}"',
        f"{body}python3 .github/scripts/probe_job.py",
        f"{body}mkdir -p artifacts/{meta_key}",
        f"{body}cp probe.json artifacts/{meta_key}/probe.json",
        f"{body}buildkite-agent meta-data set {yaml_quote(meta_key)} " + '"$$(cat probe.json)" || true',
        f"{body}buildkite-agent annotate --style info --context {yaml_quote(meta_key)} < probe.json",
    ]


def probe_jobs_on_queue(n: int, dispatch: str, queue: str, key_prefix: str = "probe") -> list[str]:
    resource = QUEUE_CLASS.get(queue, queue)
    lines: list[str] = [
        f'  - group: ":microscope: probe {queue}"',
        f"    key: {key_prefix}-group",
        "    steps:",
    ]
    for i in range(n):
        meta_key = f"probe-{resource}-{i}"
        lines += [
            f'      - label: ":microscope: {queue} {i}"',
            f"        key: {key_prefix}-{i}",
            f'        agents:',
            f'          queue: {yaml_quote(queue)}',
            "        timeout_in_minutes: 3",
            "        env:",
            '          PROBE_POOL: "buildkite"',
            f"          RESOURCE_CLASS: {yaml_quote(resource)}",
            f'          MATRIX_INDEX: "{i}"',
            f"          DISPATCH_TIME: {yaml_quote(dispatch)}",
            *probe_command(meta_key, indent=8),
            "        artifact_paths:",
            f"          - artifacts/{meta_key}/probe.json",
        ]
    dep_keys = [f"{key_prefix}-{i}" for i in range(n)]
    lines += [
        f'  - label: ":bar_chart: aggregate {queue}"',
        f"    key: {key_prefix}-aggregate",
        "    depends_on:",
        *[f"      - {k}" for k in dep_keys],
        "    timeout_in_minutes: 3",
        "    command: |",
        "      set +x",
        "      mkdir -p artifacts",
        "      buildkite-agent artifact download 'artifacts/probe-*/probe.json' artifacts/ || true",
        "      python3 .github/scripts/aggregate.py || true",
        "      if [ -f summary.json ]; then",
        f"        buildkite-agent meta-data set {yaml_quote(key_prefix + '-summary')} " + '"$$(cat summary.json)" || true',
        "      fi",
        "    artifact_paths:",
        "      - summary.json",
    ]
    return lines


def size_steps(queues: list[str], dispatch: str) -> list[str]:
    lines: list[str] = []
    keys: list[str] = []
    for i, queue in enumerate(queues):
        resource = QUEUE_CLASS.get(queue, queue)
        meta_key = f"size-{resource}"
        key = f"size-{i}"
        keys.append(key)
        lines += [
            f'  - label: ":microscope: size {queue}"',
            f"    key: {key}",
            "    agents:",
            f"      queue: {yaml_quote(queue)}",
            "    timeout_in_minutes: 3",
            "    env:",
            '      PROBE_POOL: "buildkite"',
            f"      RESOURCE_CLASS: {yaml_quote(resource)}",
            '      MATRIX_INDEX: "0"',
            f"      DISPATCH_TIME: {yaml_quote(dispatch)}",
            *probe_command(meta_key, indent=4),
            "    artifact_paths:",
            f"      - artifacts/{meta_key}/probe.json",
        ]
    return lines


def bridge_steps(dispatch: str) -> list[str]:
    return [
        '  - label: ":bridge_at_night: upstash bridge"',
        "    key: bridge",
        "    agents:",
        '      queue: "hosted"',
        "    timeout_in_minutes: 5",
        "    env:",
        f"      DISPATCH_TIME: {yaml_quote(dispatch)}",
        '      BRIDGE_OUT_DIR: "bridge-out"',
        '      WORK_MS: "120"',
        '      DROP_WORK_MS: "200"',
        "    command: bash .github/scripts/buildkite_bridge.sh",
        "    artifact_paths:",
        "      - bridge-out/**",
    ]


def main() -> None:
    run_probe = os.environ.get("RUN_PROBE") == "1"
    run_bridge = os.environ.get("RUN_BRIDGE") == "1"
    run_sizes = os.environ.get("RUN_SIZES") == "1"
    if not run_probe and not run_bridge and not run_sizes:
        print("RUN_PROBE, RUN_SIZES, and RUN_BRIDGE are unset; uploading no steps")
        return

    try:
        n = max(1, min(20, int(os.environ.get("JOBS") or "10")))
    except ValueError as exc:
        raise SystemExit(f"JOBS must be an integer: {exc}") from exc
    dispatch = os.environ.get("DISPATCH_TIME") or ""
    queue = os.environ.get("BUILDKITE_QUEUE") or "hosted"
    size_queues = [q.strip() for q in (os.environ.get("SIZE_QUEUES") or "hosted,linux-large,linux-arm64-small").split(",") if q.strip()]

    lines = [
        "# Generated by .github/scripts/buildkite_upload.py; not hand-edited.",
        "steps:",
    ]
    if run_probe:
        lines += probe_jobs_on_queue(n, dispatch, queue, key_prefix="probe")
    if run_sizes:
        lines += size_steps(size_queues, dispatch)
    if run_bridge:
        lines += bridge_steps(dispatch)

    uploaded = Path("pipeline.upload.yml")
    uploaded.write_text("\n".join(lines) + "\n", encoding="utf-8")
    kind = []
    if run_probe:
        kind.append(f"{n} probe jobs on {queue}")
    if run_sizes:
        kind.append("sizes " + ",".join(size_queues))
    if run_bridge:
        kind.append("bridge")
    print("uploading", uploaded, "(" + ", ".join(kind) + ")")
    subprocess.run(["buildkite-agent", "pipeline", "upload", str(uploaded)], check=True)


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(f"buildkite-agent not on PATH: {exc}", file=sys.stderr)
        raise SystemExit(1)
