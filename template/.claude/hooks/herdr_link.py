#!/usr/bin/env python3
"""issuelooper's side of herdr: the monitor and the aides as interactive agents in
herdr panes, where the owner talks to them directly.

herdr (a terminal workspace manager for coding agents) recognizes the agent CLI
running in a pane and reports whether it is idle, working or blocked. The launcher
uses that to start each role in its own pane (`ccrole.sh <role>`, so CC_ROLE and the
guard apply as for any session), and the supervisor uses it to send the monitor its
queue updates when the monitor is idle.

Every herdr call goes to the session named by $HERDR_SOCKET_PATH, which herdr sets
in each pane it manages; commands run from outside herdr take it from
$ISSUELOOPER_RUN/herdr.json, which the launcher writes.

    herdr_link.py start-role ROLE NAME PANE   run ccrole.sh ROLE in PANE, wait for the
                                              agent, name it NAME
    herdr_link.py status TARGET               the agent's status (idle, working, ...), or gone
    herdr_link.py prompt TARGET TEXT [--wait] submit TEXT to the agent
    herdr_link.py read TARGET [--lines N]     the agent's recent output
    herdr_link.py name ROOT ROLE [N]          the agent name for ROLE in the repo at ROOT
    herdr_link.py tab WORKSPACE CWD LABEL [K=V ...]        a new tab: prints "TAB PANE"
    herdr_link.py split PANE right|down CWD RATIO [K=V ...] a new pane beside PANE (RATIO or -)
    herdr_link.py run PANE COMMAND            type COMMAND and Enter into PANE's shell
    herdr_link.py close PANE                  close PANE (no error if it is gone)
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
READY = ("idle", "done")


class HerdrError(Exception):
    pass


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN", "").strip() or "herdr"


def run(*args: str, timeout: float = 60) -> str:
    """Run one herdr command; its stdout, or HerdrError with herdr's message."""
    try:
        p = subprocess.run(
            [herdr_bin(), *args], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise HerdrError(f"{herdr_bin()} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise HerdrError(
            f"herdr {' '.join(args[:2])} timed out after {timeout:.0f} s"
        ) from exc
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip()
        try:
            err = json.loads(msg)
            msg = (err.get("error") or {}).get("message") or msg
        except ValueError:
            pass
        raise HerdrError(f"herdr {' '.join(args[:2])}: {msg[:300]}")
    return p.stdout


def call(*args: str, timeout: float = 60) -> dict:
    """A herdr command that answers with JSON: its `result`."""
    out = run(*args, timeout=timeout)
    try:
        return json.loads(out).get("result") or {}
    except ValueError as exc:
        raise HerdrError(f"herdr {' '.join(args[:2])}: not JSON: {out[:200]}") from exc


def agent(target: str) -> dict | None:
    """The agent in a pane (or with a name), or None when there is none."""
    try:
        return call("agent", "get", target).get("agent")
    except HerdrError:
        return None


def status(target: str) -> str:
    a = agent(target)
    return str(a.get("agent_status") or "unknown") if a else "gone"


def prompt(target: str, text: str, wait: bool = False, timeout_s: float = 1800) -> dict:
    args = ["agent", "prompt", target, text]
    if wait:
        args += ["--wait", "--timeout", str(int(timeout_s * 1000))]
    return call(*args, timeout=(timeout_s + 30) if wait else 60)


def read(target: str, lines: int = 60, source: str = "recent-unwrapped") -> str:
    return run("agent", "read", target, "--source", source, "--lines", str(lines))


def snapshot(target: str) -> str:
    """What herdr sees at the bottom of the pane: the input box and the last lines."""
    try:
        return read(target, lines=20, source="detection")
    except HerdrError:
        return ""


def new_tab(
    workspace: str, cwd: str, label: str, env: dict[str, str]
) -> tuple[str, str]:
    args = ["tab", "create", "--cwd", cwd, "--label", label]
    if workspace:
        args += ["--workspace", workspace]
    for k, v in env.items():
        args += ["--env", f"{k}={v}"]
    r = call(*args)
    return r["tab"]["tab_id"], r["root_pane"]["pane_id"]


def split(
    pane: str, direction: str, cwd: str, env: dict[str, str], ratio: float | None = None
) -> str:
    args = ["pane", "split", pane, "--direction", direction, "--cwd", cwd, "--no-focus"]
    if ratio is not None:
        args += ["--ratio", str(ratio)]
    for k, v in env.items():
        args += ["--env", f"{k}={v}"]
    return call(*args)["pane"]["pane_id"]


def run_in(pane: str, command: str) -> None:
    run("pane", "run", pane, command)  # answers with nothing on success


def close(pane: str) -> None:
    try:
        run("pane", "close", pane)
    except HerdrError:
        pass


def agent_name(root: str, role: str, n: int = 0) -> str:
    """A name herdr accepts ([a-z][a-z0-9_-]{0,31}) and unique per repository and role:
    two repositories looped from one herdr session keep apart."""
    slug = (
        re.sub(r"[^a-z0-9-]+", "-", pathlib.Path(root).name.lower()).strip("-")
        or "repo"
    )
    if not slug[0].isalpha():
        slug = "r" + slug
    suffix = f"-{role}{n if n else ''}"
    return slug[: 32 - len(suffix)].rstrip("-") + suffix


def start_role(role: str, name: str, pane: str, timeout_s: float = 120) -> dict:
    """Start an interactive session for ROLE in PANE (at its shell prompt) through
    ccrole.sh, wait until herdr sees the agent ready, and give it NAME."""
    run_in(pane, f"{shlex.quote(str(HERE / 'ccrole.sh'))} {shlex.quote(role)}")
    deadline = time.time() + timeout_s
    a = None
    while time.time() < deadline:
        a = agent(pane)
        if a and a.get("agent_status") in READY + ("blocked",):
            break
        time.sleep(2)
    else:
        raise HerdrError(
            f"no agent became ready in pane {pane} within {timeout_s:.0f} s"
        )
    call("agent", "rename", pane, name)
    return a


class Monitor:
    """The supervisor's link to a monitor in a herdr pane, with the same two calls as
    the broker's: status() and say().

    The owner types into the same pane, so a queue update must not land in the middle
    of what they are writing. Besides herdr's own state (idle, not working or at a
    question), the pane has to stay unchanged for quiet_s seconds before a message
    goes in."""

    def __init__(self, target: str, quiet_s: float = 3, sleep=time.sleep):
        self.target, self.quiet_s, self.sleep = target, quiet_s, sleep

    def status(self) -> dict:
        state = status(self.target)
        busy = state not in READY
        if not busy and self.quiet_s:
            before = snapshot(self.target)
            self.sleep(self.quiet_s)
            busy = snapshot(self.target) != before or status(self.target) not in READY
        return {"busy": busy, "done": 1, "state": state}

    def say(self, text: str) -> dict:
        prompt(self.target, text)
        return {"turn": None}


def target_env() -> None:
    """Outside herdr, point herdr at the session the launcher recorded."""
    if os.environ.get("HERDR_SOCKET_PATH"):
        return
    run_dir = os.environ.get("ISSUELOOPER_RUN", "").strip()
    if not run_dir:
        return
    try:
        rec = json.loads((pathlib.Path(run_dir) / "herdr.json").read_text())
    except (OSError, ValueError):
        return
    if rec.get("socket"):
        os.environ["HERDR_SOCKET_PATH"] = rec["socket"]


def main(argv: list[str]) -> int:
    target_env()
    cmd, rest = (argv[1], argv[2:]) if len(argv) > 1 else ("", [])
    try:
        if cmd == "start-role" and len(rest) == 3:
            a = start_role(*rest)
            print(
                json.dumps({"name": rest[1], "pane": rest[2], "agent": a.get("agent")})
            )
        elif cmd == "status" and len(rest) == 1:
            print(status(rest[0]))
        elif cmd == "prompt" and len(rest) >= 2:
            wait = "--wait" in rest
            text = " ".join(x for x in rest[1:] if x != "--wait")
            prompt(rest[0], text, wait=wait)
            if wait:
                print(read(rest[0], lines=80))
        elif cmd == "read" and rest:
            lines = int(rest[rest.index("--lines") + 1]) if "--lines" in rest else 60
            print(read(rest[0], lines=lines))
        elif cmd == "tab" and len(rest) >= 3:
            tab, pane = new_tab(rest[0], rest[1], rest[2], dict(x.split("=", 1) for x in rest[3:]))
            print(tab, pane)
        elif cmd == "split" and len(rest) >= 4:
            ratio = None if rest[3] == "-" else float(rest[3])
            print(split(rest[0], rest[1], rest[2], dict(x.split("=", 1) for x in rest[4:]), ratio))
        elif cmd == "run" and len(rest) == 2:
            run_in(rest[0], rest[1])
        elif cmd == "close" and len(rest) == 1:
            close(rest[0])
        elif cmd == "name" and len(rest) in (2, 3):
            print(agent_name(rest[0], rest[1], int(rest[2]) if len(rest) == 3 else 0))
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except HerdrError as exc:
        print(f"herdr_link: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
