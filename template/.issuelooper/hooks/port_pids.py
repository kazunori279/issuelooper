#!/usr/bin/env python3
"""The PIDs listening on a TCP port, one per line, with no lsof, ss or fuser.

    python3 .issuelooper/hooks/port_pids.py <port>            # print them
    python3 .issuelooper/hooks/port_pids.py <port> --kill     # and send them SIGTERM

Reads /proc/net/tcp and /proc/net/tcp6 for the sockets listening on the port, then
/proc/<pid>/fd for the processes holding them (only this user's processes can be read).
Elsewhere it falls back to `lsof -tiTCP:<port> -sTCP:LISTEN`. Exits 1 when nothing listens.
Some machines have no lsof, and every session that stopped its server with it failed there.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

LISTEN = "0A"  # the TCP state column for LISTEN in /proc/net/tcp


def listening_inodes(
    port: int, tables: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6")
) -> set[str]:
    """The socket inodes listening on port, from the kernel's TCP tables."""
    found = set()
    for table in tables:
        try:
            with open(table) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if len(cols) < 10 or cols[3] != LISTEN:
                continue
            try:
                if int(cols[1].rsplit(":", 1)[1], 16) == port:
                    found.add(cols[9])
            except (IndexError, ValueError):
                continue
    return found


def holders(inodes: set[str], proc: str = "/proc") -> list[int]:
    """The PIDs whose open files include one of the sockets."""
    want = {f"socket:[{i}]" for i in inodes}
    pids = []
    for d in os.listdir(proc):
        if not d.isdigit():
            continue
        try:
            fds = os.listdir(f"{proc}/{d}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"{proc}/{d}/fd/{fd}") in want:
                    pids.append(int(d))
                    break
            except OSError:
                continue
    return sorted(pids)


def port_pids(port: int) -> list[int]:
    """The PIDs listening on port: from /proc on Linux, else from lsof ([] when neither works)."""
    if os.path.isdir("/proc/net"):
        return holders(listening_inodes(port))
    try:
        r = subprocess.run(
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted({int(x) for x in r.stdout.split() if x.isdigit()})


def main(argv: list[str]) -> int:
    if not argv or not argv[0].isdigit():
        print("usage: port_pids.py <port> [--kill]", file=sys.stderr)
        return 2
    pids = [p for p in port_pids(int(argv[0])) if p != os.getpid()]
    for pid in pids:
        print(pid)
        if "--kill" in argv[1:]:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    return 0 if pids else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
