#!/usr/bin/env python3
"""The owner's side of the monitor: talk to its session through broker.py.

    monitorctl.py start                       start the broker and the monitor session
    monitorctl.py say TEXT [--no-wait]        send the monitor a message and wait for its turn
    monitorctl.py wait [--turn N]             keep waiting for a turn that is still running
    monitorctl.py status
    monitorctl.py tail [-n 20]                the current (or last) turn's steps
    monitorctl.py stop

Every command takes --run DIR (default $ISSUELOOPER_RUN, else <repo>/.issuelooper/run).
`say` waits up to --timeout seconds (default 1800); --tag labels the message in
logs/owner.jsonl (default "owner").
"""

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402
from broker_socket import socket_path  # noqa: E402


def run_dir() -> pathlib.Path:
    v = os.environ.get("ISSUELOOPER_RUN", "").strip()
    return pathlib.Path(v) if v else config.repo_root() / ".issuelooper" / "run"


def call(run, quiet=False, **req):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(socket_path(pathlib.Path(run) / "broker.sock")))
    except (FileNotFoundError, ConnectionRefusedError):
        s.close()
        if quiet:
            return None
        sys.exit(
            f"the monitor is not running ({run}/broker.sock); start it with `monitorctl.py start`"
        )
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    resp = json.loads(data)
    if set(resp) == {"error"}:  # the broker's report of a failed command
        sys.exit(f"broker: {resp['error']}")
    return resp


def show_turn(r):
    if not r.get("done"):
        print(
            f"[turn {r['turn']} still running: {r.get('busy_for_s')} s so far, "
            f"last event {r.get('last_event_age_s')} s ago]"
        )
        for s in r.get("recent_steps", []):
            print(
                f"  · {s.get('type')} {s.get('tool') or ''} {s.get('text') or ''}".rstrip()
            )
        print("Use `monitorctl.py wait` to keep waiting.")
        return
    print(
        f"[turn {r['turn']} {r['status']} · {r.get('agent_seconds') or 0:.0f} s · {r['steps']} steps · "
        f"tools {r['tools']}]"
    )
    if r.get("subagents"):
        print("subagents:", "; ".join(r["subagents"]))
    for q in r.get("questions") or []:
        print("QUESTION:", q)
    if r.get("error"):
        print("ERROR:", r["error"])
    print("--- monitor ---")
    print((r.get("response") or "").strip())


def start(run: pathlib.Path) -> int:
    if call(run, quiet=True, cmd="status") is not None:
        print(f"the monitor is already running for {run}")
        return 0
    (run / "logs").mkdir(parents=True, exist_ok=True)
    with open(run / "logs" / "broker.log", "a") as log:
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "broker.py"), "--run", str(run)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    for _ in range(100):
        if (run / "broker.sock").exists():
            status = call(run, quiet=True, cmd="status")
            if status is not None:
                print(json.dumps(status))
                return 0
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    sys.exit(f"the broker did not start; see {run}/logs/broker.log")


def main():
    config.apply()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("cmd", choices=["start", "say", "wait", "status", "tail", "stop"])
    ap.add_argument("text", nargs="*")
    ap.add_argument("--run")
    ap.add_argument("--tag", default="owner")
    ap.add_argument("--turn", type=int)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("-n", type=int, default=20)
    a = ap.parse_intermixed_args()
    run = pathlib.Path(a.run).expanduser().resolve() if a.run else run_dir().resolve()
    text = " ".join(a.text).strip()

    if a.cmd == "start":
        return start(run)
    if a.cmd == "say":
        if not text:
            sys.exit("say: text required")
        r = call(run, cmd="say", text=text, tag=a.tag)
        if r.get("queued"):
            print(f"[queued as turn {r['turn']} behind the active turn]")
        if a.no_wait:
            print(f"[sent as turn {r['turn']}]")
            return 0
        show_turn(call(run, cmd="wait", turn=r["turn"], timeout=a.timeout))
    elif a.cmd == "wait":
        turn = a.turn or call(run, cmd="status")["sent"]
        show_turn(call(run, cmd="wait", turn=turn, timeout=a.timeout))
    elif a.cmd == "status":
        print(json.dumps(call(run, cmd="status"), indent=1))
    elif a.cmd == "tail":
        for s in call(run, cmd="tail", n=a.n)["steps"]:
            print(
                f"{str(s.get('i')):>4} {s.get('type') or '':<15} {s.get('tool') or '':<20} "
                f"{s.get('sec') or 0:>6}s {s.get('text') or ''}"
            )
    elif a.cmd == "stop":
        print(json.dumps(call(run, cmd="stop")))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
