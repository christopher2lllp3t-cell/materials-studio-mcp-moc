"""P2 test-only fake process helper; it must never be treated as CASTEP."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time


def _append_pid(path: Path, role: str) -> None:
    with path.open("a", encoding="ascii", newline="\n") as handle:
        handle.write(f"{role}:{__import__('os').getpid()}\n")
        handle.flush()


def _spawn(role: str, pid_file: Path) -> None:
    subprocess.Popen(
        [sys.executable, __file__, "--role", role, "--pid-file", str(pid_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("normal", "write_then_sleep", "sleep", "tree", "nonzero", "missing_output", "truncated"))
    parser.add_argument("--seed")
    parser.add_argument("--pid-file", required=True)
    parser.add_argument("--role", choices=("child", "grandchild"))
    args = parser.parse_args()
    pid_file = Path(args.pid_file)
    if args.role == "grandchild":
        _append_pid(pid_file, "grandchild")
        time.sleep(60)
        return 0
    if args.role == "child":
        _append_pid(pid_file, "child")
        _spawn("grandchild", pid_file)
        time.sleep(60)
        return 0

    _append_pid(pid_file, "parent")
    if args.scenario == "tree":
        _spawn("child", pid_file)
        time.sleep(60)
        return 0
    if args.scenario == "sleep":
        time.sleep(60)
        return 0
    if args.scenario == "nonzero":
        return 7
    if args.scenario == "missing_output":
        return 0
    target = Path.cwd() / f"{args.seed}.castep"
    if args.scenario == "truncated":
        target.write_text("SYNTHETIC P2 fixture; not CASTEP execution.\nFinal energy = -1.0 eV\n", encoding="utf-8", newline="\n")
        return 0
    target.write_text(
        "SYNTHETIC P2 fixture; not CASTEP execution.\n"
        "Final energy = -1.2345 eV\n"
        "Total time = 0.01 s\n",
        encoding="utf-8",
        newline="\n",
    )
    if args.scenario == "write_then_sleep":
        time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
