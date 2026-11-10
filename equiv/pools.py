#!/usr/bin/env python3
"""Pluggable overflow pools for the Upstash dispatcher.

Local tests use SimulatedPool only. Remote pools (gha, circleci, buildkite,
blacksmith, upstash_box) refuse to fire unless allow_remote=True, so a
casual import cannot burn credits or dispatch jobs.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_ORDER = ("gha", "circleci", "buildkite", "blacksmith", "upstash_box")

# Published concurrency ceilings used as default caps. Measured GHA ubuntu
# concurrency was 5; mixed labels reached 8. CircleCI Free is 30, but we
# keep the default cap modest so a first overflow burst stays under the
# auto-flag triggers noted in the sweep report.
DEFAULT_CAPS = {
    "gha": 5,
    "circleci": 10,
    "buildkite": 10,
    "blacksmith": 8,
    "upstash_box": 1,
}


@dataclass
class WorkerHandle:
    pool: str
    name: str
    proc: subprocess.Popen | None = None
    listen: str = ""
    remote_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def poll(self) -> int | None:
        if self.proc is None:
            return None
        return self.proc.poll()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class Pool(ABC):
    name: str
    cap: int
    allow_remote: bool = False

    @abstractmethod
    def spawn(
        self,
        count: int,
        *,
        controller: str,
        work_ms: int,
        name_prefix: str,
    ) -> list[WorkerHandle]:
        raise NotImplementedError

    def teardown(self, handles: list[WorkerHandle], *, timeout: float = 5.0) -> None:
        for handle in handles:
            if handle.proc is None or handle.proc.poll() is not None:
                continue
            handle.proc.terminate()
        import time

        deadline = time.time() + timeout
        for handle in handles:
            if handle.proc is None:
                continue
            remaining = max(0.05, deadline - time.time())
            try:
                handle.proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                handle.proc.kill()
                try:
                    handle.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass


def allocate(n_workers: int, pools: list[Pool]) -> dict[str, int]:
    """Fill pools in order up to each cap. Leftover demand is left unassigned."""
    assigned: dict[str, int] = {p.name: 0 for p in pools}
    remaining = max(0, int(n_workers))
    for pool in pools:
        take = min(remaining, max(0, int(pool.cap)))
        assigned[pool.name] = take
        remaining -= take
        if remaining == 0:
            break
    assigned["_unassigned"] = remaining
    return assigned


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class SimulatedPool(Pool):
    """Local worker.py processes. Used for tests and dry runs of every pool name."""

    def __init__(self, name: str, cap: int, worker_script: Path | None = None) -> None:
        self.name = name
        self.cap = cap
        self.allow_remote = False
        self.worker_script = Path(worker_script or Path(__file__).resolve().parent / "worker.py")

    def spawn(
        self,
        count: int,
        *,
        controller: str,
        work_ms: int,
        name_prefix: str,
    ) -> list[WorkerHandle]:
        if count <= 0:
            return []
        if count > self.cap:
            raise ValueError(f"{self.name} cap is {self.cap}, asked for {count}")
        handles: list[WorkerHandle] = []
        python = sys.executable
        for i in range(count):
            name = f"{name_prefix}-{self.name}-{i}"
            port = _free_udp_port()
            listen = f"127.0.0.1:{port}"
            env = os.environ.copy()
            env["WORKER_POOL"] = self.name
            # Do not copy or print secret files. The sim worker needs none.
            proc = subprocess.Popen(
                [
                    python,
                    str(self.worker_script),
                    "--listen",
                    listen,
                    "--controller",
                    controller,
                    "--name",
                    name,
                    "--work-ms",
                    str(work_ms),
                ],
                cwd=str(self.worker_script.parent),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            handles.append(WorkerHandle(pool=self.name, name=name, proc=proc, listen=listen))
        return handles


class RemotePool(Pool):
    """A real CI/box pool. spawn() is a no-op unless allow_remote is set."""

    def __init__(self, name: str, cap: int, *, allow_remote: bool = False, how: str = "") -> None:
        self.name = name
        self.cap = cap
        self.allow_remote = allow_remote
        self.how = how or f"trigger the {name} probe/bridge job"

    def spawn(
        self,
        count: int,
        *,
        controller: str,
        work_ms: int,
        name_prefix: str,
    ) -> list[WorkerHandle]:
        if count <= 0:
            return []
        if not self.allow_remote:
            raise RuntimeError(
                f"{self.name} remote spawn is disabled. "
                "Local tests must use SimulatedPool. Pass allow_remote=True only after "
                "the account, token, and (for Blacksmith) the GitHub App are in place."
            )
        raise RuntimeError(
            f"{self.name} remote spawn is not enabled in this harness yet "
            f"({self.how}). Use the matching trigger script by hand after signup."
        )


def default_remote_pools(*, allow_remote: bool = False) -> list[Pool]:
    return [
        RemotePool("gha", DEFAULT_CAPS["gha"], allow_remote=allow_remote, how="gh workflow run probe.yml or bridge.yml"),
        RemotePool("circleci", DEFAULT_CAPS["circleci"], allow_remote=allow_remote, how="python3 circleci_trigger.py --confirm"),
        RemotePool("buildkite", DEFAULT_CAPS["buildkite"], allow_remote=allow_remote, how="python3 buildkite_trigger.py --confirm"),
        RemotePool("blacksmith", DEFAULT_CAPS["blacksmith"], allow_remote=allow_remote, how="gh workflow run blacksmith-probe.yml after the app is installed"),
        RemotePool("upstash_box", DEFAULT_CAPS["upstash_box"], allow_remote=allow_remote, how="start the existing box worker through equiv/box_setup.mjs; do not enable keepAlive"),
    ]


def simulated_pools(
    caps: dict[str, int] | None = None,
    worker_script: Path | None = None,
) -> list[SimulatedPool]:
    caps = caps or DEFAULT_CAPS
    return [SimulatedPool(name, caps.get(name, 1), worker_script=worker_script) for name in DEFAULT_ORDER]
