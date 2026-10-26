#!/usr/bin/env python3
"""Run the sweep (`sweep_run.py`) in this worktree while holding a sweep slot.

    python3 .claude/hooks/sweep_locked.py                  # from the worktree root
    python3 .claude/hooks/sweep_locked.py --update-pr 41   # ... then fix PR #41's body
    python3 .claude/hooks/sweep_locked.py --lane main      # its own lock, not a slot

One implementation of the sweep lock, shared by every worker, the merger's
rebases and the supervisor's re-sweeps: `flock` on an open file that is never
unlinked, and a deadline ($SWEEP_LOCK_DEADLINE_S, 30 minutes) after which the
sweep is NOT run. The kernel drops the lock when this process exits, however it
exits.

With --lane NAME it takes the lock LOCK.lane-NAME instead of a slot: one sweep at a
time in that lane, beside the SWEEP_SLOTS others. The supervisor's check of every new
main runs in the lane `main`: in a slot it held one of four for over half the run,
and session sweeps waited up to a quarter of an hour.

It prints how long it waited for the slot and how long the sweep took, then
sweep_run.py's own output: the `git:` provenance line, the `green:` line, and
`sweep record: <path>` when it wrote one. The exit status is sweep_run.py's.

With --update-pr N, a green sweep that wrote its record also rewrites PR #N's
body: the `Sweep SHA:` line gets the swept head and the `green:` line is
replaced (or appended). That is the step a rebase makes necessary (Gate 2
refuses a merge whose Sweep SHA is not the head), so the merger's rebase and a
worker's update of a handed-back PR do it the same way.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pathlib
import re
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import config  # noqa: E402

config.apply()

RUNNER = ".claude/hooks/sweep_run.py"
LOCK = os.environ.get("SWEEP_LOCK", "/tmp/issuelooper-sweep.lock")
# The slots are the machine's, shared by every loop on it (unlike the scratch files under
# $ISSUELOOPER_TMP): they share out its cores, and a lock file left behind holds nothing.
# SWEEP_SLOTS sweeps may run at once (a full suite is mostly single-threaded, so a
# many-core host can run several). Slot 0 is LOCK itself and is tried last; slot k
# is LOCK.k.
SLOTS = max(1, int(os.environ.get("SWEEP_SLOTS", "1") or 1))
DEADLINE_S = float(os.environ.get("SWEEP_LOCK_DEADLINE_S", "1800"))
GH = os.environ.get("CLAIM_GH", "gh")
GIT_LINE = re.compile(r"^git:\s+([0-9a-f]{40}) \(clean\)", re.MULTILINE)
GREEN_LINE = re.compile(r"^(?:\d+\s*(?:->|→)\s*)?\d+ green: .+$", re.MULTILINE)
BODY_GREEN = re.compile(r"(?m)^[`*#\s-]*(?:\d+\s*(?:->|→)\s*)?\d+\s+green:[^\r\n]*$")
BODY_SWEEP_SHA = re.compile(r"(?i)(\bSweep\s+SHA\b[*`:\s]*)[0-9a-f]{7,40}")


def update_pr(pr: str, sha: str, green: str) -> int:
    done = subprocess.run([GH, "pr", "view", pr, "--json", "body", "-q", ".body"], capture_output=True, text=True)
    if done.returncode != 0:
        print(f"could not read PR #{pr}: {done.stderr.strip()}", file=sys.stderr)
        return 1
    body = done.stdout
    if BODY_SWEEP_SHA.search(body):
        body = BODY_SWEEP_SHA.sub(lambda m: m.group(1) + sha, body, count=1)
    else:
        body = body.rstrip("\n") + f"\n- **Sweep SHA:** {sha}\n"
    if BODY_GREEN.search(body):
        body = BODY_GREEN.sub(green, body, count=1)
    else:
        body = body.rstrip("\n") + f"\n\n{green}\n"
    config.scratch_root().mkdir(parents=True, exist_ok=True)
    path = str(config.scratch_root() / f"sweep-pr-{pr}-body.md")
    with open(path, "w") as fh:
        fh.write(body)
    done = subprocess.run([GH, "pr", "edit", pr, "--body-file", path], capture_output=True, text=True)
    if done.returncode != 0:
        print(f"could not edit PR #{pr}: {done.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"PR #{pr} body now names Sweep SHA {sha[:12]} and `{green}`")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--update-pr", metavar="N", help="after a green, recorded sweep, fix PR #N's body")
    ap.add_argument("--lane", metavar="NAME", help="take the lock LOCK.lane-NAME instead of a sweep slot")
    args = ap.parse_args()
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    root = top.stdout.strip() if top.returncode == 0 else os.getcwd()
    shared = os.environ.get("SHARED_REPO_PATH", "")
    if shared and os.path.realpath(root) == os.path.realpath(shared):
        print(
            "sweep_locked: refusing to sweep the shared checkout; run it in your worktree",
            file=sys.stderr,
        )
        return 2
    if not os.path.isfile(os.path.join(root, RUNNER)):
        print(f"sweep_locked: {root} has no {RUNNER}; sweep NOT run", file=sys.stderr)
        return 2
    start = time.time()
    deadline = start + DEADLINE_S
    fd = None
    names = [f"{LOCK}.lane-{args.lane}"] if args.lane else [f"{LOCK}.{k}" for k in range(1, SLOTS)] + [LOCK]
    while fd is None:
        for name in names:
            cand = os.open(name, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(cand, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = cand
                break
            except BlockingIOError:
                os.close(cand)
        if fd is None:
            if time.time() > deadline:
                print(
                    f"could not take {LOCK} in {DEADLINE_S:.0f} s; sweep NOT run", file=sys.stderr
                )
                return 3
            time.sleep(5)
    got = time.time()
    print(f"sweep lock taken after {got - start:.0f} s", flush=True)
    # For sweep_run.py's line in runs.jsonl.
    env = dict(os.environ, SWEEP_LANE=args.lane or os.path.basename(name), SWEEP_WAIT_S=f"{got - start:.1f}",
               SWEEP_PR=args.update_pr or os.environ.get("SWEEP_PR", ""))
    proc = subprocess.Popen([sys.executable, RUNNER], cwd=root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    rc = proc.wait()
    os.close(fd)  # release the lock before any network call
    print(f"sweep took {time.time() - got:.0f} s (exit {rc})")
    if rc != 0 or not args.update_pr:
        return rc
    out = "".join(lines)
    sha, green = GIT_LINE.search(out), GREEN_LINE.findall(out)
    if not sha or not green or "sweep record:" not in out:
        print("no sweep record was written (dirty tree?); PR body left alone", file=sys.stderr)
        return 1
    return update_pr(args.update_pr, sha.group(1), green[-1].strip())


if __name__ == "__main__":
    sys.exit(main())
