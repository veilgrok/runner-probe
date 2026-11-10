#!/usr/bin/env python3
"""Control-plane pull worker for an E2B sandbox.

Same UDP hello/task/result/drain protocol as worker.py and
daytona_pull_worker.py. Each task is executed inside the sandbox
through the E2B SDK, or locally when --fake is set.

Never prints API keys or other secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import time
from pathlib import Path

from oracle import count_primes


REMOTE_DIR = "/tmp/veil-equiv"
TASK_PY = f"{REMOTE_DIR}/daytona_task.py"


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


def _load_key() -> str:
    env = os.environ.get("E2B_API_KEY") or ""
    if env.strip():
        return env.strip()
    path = os.environ.get("E2B_API_KEY_FILE") or ""
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    raise RuntimeError("E2B_API_KEY or E2B_API_KEY_FILE is required")


def _connect(sandbox_id: str):
    from e2b import Sandbox

    key = _load_key()
    # Connect to an already-running sandbox; do not create a new one.
    connect = getattr(Sandbox, "connect", None)
    if callable(connect):
        try:
            return connect(sandbox_id, api_key=key)
        except TypeError:
            return connect(sandbox_id)
    # Older SDKs used Sandbox(sandbox_id=...)
    try:
        return Sandbox(sandbox_id=sandbox_id, api_key=key)
    except TypeError:
        return Sandbox(sandbox_id)


def _exec_text(result) -> tuple[int, str]:
    code = getattr(result, "exit_code", None)
    if code is None:
        code = getattr(result, "exitCode", 0)
    text = (
        getattr(result, "stdout", None)
        or getattr(result, "text", None)
        or getattr(result, "output", None)
        or ""
    )
    if not isinstance(text, str):
        text = str(text)
    return int(code or 0), text


def exec_task_remote(sandbox, lo: int, hi: int, work_ms: int, name: str, tid: int, timeout: int) -> int:
    cmd = (
        f"python3 {TASK_PY} --lo {int(lo)} --hi {int(hi)} "
        f"--work-ms {int(work_ms)} --name {json.dumps(name)} --id {int(tid)}"
    )
    if hasattr(sandbox, "commands") and hasattr(sandbox.commands, "run"):
        result = sandbox.commands.run(cmd, timeout=timeout)
    elif hasattr(sandbox, "process") and hasattr(sandbox.process, "start"):
        proc = sandbox.process.start(cmd)
        result = proc.wait(timeout=timeout)
    else:
        raise RuntimeError("unsupported E2B sandbox exec API")
    code, text = _exec_text(result)
    if code not in (0,):
        raise RuntimeError(f"sandbox exec exit {code}: {text[:400]}")
    last = text.strip().splitlines()[-1] if text.strip() else ""
    data = json.loads(last)
    return int(data["count"])


def exec_task_fake(lo: int, hi: int, work_ms: int, name: str, tid: int) -> int:
    count = count_primes(lo, hi)
    work_loop(f"{name}:{tid}:{lo}:{hi}:{count}".encode(), work_ms)
    return count


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--listen", default="127.0.0.1:27700")
    p.add_argument("--controller", default="127.0.0.1:7700")
    p.add_argument("--name", required=True)
    p.add_argument("--work-ms", type=int, default=120)
    p.add_argument("--sandbox-id", default="")
    p.add_argument("--fake", action="store_true")
    p.add_argument("--exec-timeout", type=int, default=60)
    args = p.parse_args()

    sandbox = None
    if not args.fake:
        if not args.sandbox_id:
            raise SystemExit("real mode requires --sandbox-id")
        sandbox = _connect(args.sandbox_id)

    listen_host, listen_port = args.listen.rsplit(":", 1)
    ctrl_host, ctrl_port = args.controller.rsplit(":", 1)
    dest = (ctrl_host, int(ctrl_port))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((listen_host, int(listen_port)))
    sock.settimeout(0.5)

    def send(msg: dict) -> None:
        sock.sendto(json.dumps(msg, separators=(",", ":")).encode("utf-8"), dest)

    last_hello = 0.0
    while True:
        now = time.perf_counter()
        if now - last_hello >= 0.4:
            send({"t": "hello", "name": args.name, "ts": time.time(), "pool": "e2b"})
            last_hello = now
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        typ = msg.get("t")
        if typ in ("bye", "drain"):
            send({"t": "bye", "name": args.name, "reason": typ})
            return
        if typ == "task":
            lo = int(msg["lo"])
            hi = int(msg["hi"])
            tid = int(msg["id"])
            lease_id = msg.get("lease_id")
            try:
                if args.fake:
                    count = exec_task_fake(lo, hi, args.work_ms, args.name, tid)
                else:
                    count = exec_task_remote(
                        sandbox, lo, hi, args.work_ms, args.name, tid, args.exec_timeout
                    )
            except Exception:
                # Sandbox gone or exec failed: exit so the dispatcher reassigns.
                return
            send({"t": "result", "id": tid, "name": args.name, "count": count, "lease_id": lease_id})
            last_hello = 0.0


if __name__ == "__main__":
    main()
