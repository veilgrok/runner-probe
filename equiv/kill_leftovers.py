#!/usr/bin/env python3
"""Kill leftover equivalent-workload processes by argv, never pkill.

Only matches a real script invocation such as ``python3 worker.py --listen ...``.
A shell or ``python3 -c`` whose command text merely mentions these names is left alone.
"""

from __future__ import annotations

import os
import pathlib
import signal
import sys

TARGETS = {
    "worker": ("worker.py",),
    "all": ("worker.py", "dgram_stdio.py", "echo_remote.py"),
}


def iter_cmds() -> list[tuple[int, list[str]]]:
    mine = {os.getpid(), os.getppid()}
    out: list[tuple[int, list[str]]] = []
    for p in pathlib.Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        pid = int(p.name)
        if pid in mine:
            continue
        try:
            raw = (p / "cmdline").read_bytes().split(b"\0")
            args = [a.decode(errors="replace") for a in raw if a]
        except (OSError, PermissionError):
            continue
        if args:
            out.append((pid, args))
    return out


def is_script_invocation(args: list[str], names: tuple[str, ...]) -> bool:
    if "-c" in args:
        return False
    for a in args:
        base = a.rsplit("/", 1)[-1]
        if base in names:
            return True
    return False


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "worker"
    names = TARGETS.get(mode)
    if not names:
        raise SystemExit(f"usage: kill_leftovers.py worker|all, not {mode!r}")
    killed: list[str] = []
    for pid, args in iter_cmds():
        if not is_script_invocation(args, names):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(str(pid))
        except ProcessLookupError:
            pass
    print("killed", ",".join(killed) if killed else "none")


if __name__ == "__main__":
    main()
