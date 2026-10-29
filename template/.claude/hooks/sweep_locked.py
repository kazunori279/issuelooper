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

A clean checkout whose HEAD already has a green sweep record for the same command is
not swept again: it prints the record's `git:`, `sweep record:` and `green:` lines as
a sweep would, and --update-pr goes on from there (SWEEP_FORCE=1 sweeps anyway): hours
went to sweeping commits that were already green. It looks again once
it has the slot, since another sweep of the same commit may have turned green while it
waited, and then gives the slot back unused. A record counts only for the same command
and kind (a full sweep is never satisfied by an affected record) and the same
`[sweep] env_id` (SWEEP_ENV_ID). runs.jsonl notes each reuse, with "reuse": "before-slot"
or "after-slot" and the wait.

With `[sweep] format` (SWEEP_FORMAT, e.g. `uv run ruff format src tests`), an agent's
sweep of a clean checkout runs the formatter first. When it changes files, the sweep
stops there (exit 1) and names them: commit them and sweep again. Lint-only red runs
were a large share of red sweeps. The supervisor's own sweeps (SWEEP_CALLER set:
re-sweeps, auto-rebases, the main check) never format: what they check must stay as
committed, and a format diff there is red, as before.

With the merge train on (POOL_MERGE_TRAIN=1) and `[sweep] affected` set, a sweep of a
PR head (a worker's, a merger's, the supervisor's re-sweep or auto-rebase) runs only
the affected tests (sweep_run.py --affected): the train checks the whole suite on the
merged tree before anything merges. The main check and the train's own check
(SWEEP_CALLER main-check or merge-train) always run it all. --full and --affected
choose explicitly.

With --update-pr N, a green sweep that wrote its record also rewrites PR #N's
body: the `Sweep SHA:` line gets the swept head and the `green:` line is
replaced (or appended). That is the step a rebase makes necessary (Gate 2
refuses a merge whose Sweep SHA is not the head), so the merger's rebase and a
worker's update of a handed-back PR do it the same way.
"""

from __future__ import annotations

import argparse
import fcntl
import json
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


def wants_affected(args) -> bool:
    """Whether this sweep runs only the affected tests (see the docstring)."""
    if args.full:
        return False
    if args.affected:
        return True
    return (os.environ.get("POOL_MERGE_TRAIN", "").strip() in ("1", "true", "yes", "on")
            and "{tests}" in os.environ.get("SWEEP_AFFECTED", "")
            and os.environ.get("SWEEP_CALLER", "").strip() not in ("main-check", "merge-train"))


def green_record(root: str, affected: bool = False) -> tuple[str, pathlib.Path, dict] | None:
    """(HEAD, its record, the record) when the checkout is clean and HEAD has a green
    sweep record for this sweep command (a full one; for an affected sweep, a full one
    or an affected one from the same affected command), written under the same
    SWEEP_ENV_ID; else None."""
    def git(*a):
        r = subprocess.run(["git", "-C", root, *a], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    head = (git("rev-parse", "HEAD") or "").strip()
    status = git("status", "--porcelain", "--", ".")
    if len(head) != 40 or status is None or status.strip():
        return None
    command = os.environ.get("SWEEP_COMMAND", "").strip() or "make verify"
    accept = {("full", command)}
    if affected:
        accept.add(("affected", os.environ.get("SWEEP_AFFECTED", "").strip()))
    env_id = os.environ.get("SWEEP_ENV_ID", "").strip()
    for d in config.record_dirs("sweep"):
        path = d / f"{head}.json"
        try:
            rec = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (rec.get("sha") == head and (rec.get("kind") or "full", rec.get("command")) in accept
                and (rec.get("env_id") or "") == env_id and isinstance(rec.get("areas"), dict)):
            return head, path, rec
    return None


def format_first(root: str) -> list[str]:
    """Run [sweep] format in a clean checkout, for an agent's sweep: the files it changed
    ([] when it changed none, or did not run)."""
    cmd = os.environ.get("SWEEP_FORMAT", "").strip()
    if not cmd or os.environ.get("SWEEP_CALLER", "").strip():
        return []
    status = subprocess.run(["git", "-C", root, "status", "--porcelain", "--", "."], capture_output=True, text=True)
    if status.returncode != 0 or status.stdout.strip():
        return []  # uncommitted work: the sweep writes no record anyway, and it is not ours to touch
    try:
        subprocess.run(["bash", "-c", cmd], cwd=root, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return []
    after = subprocess.run(["git", "-C", root, "status", "--porcelain", "--", "."], capture_output=True, text=True)
    return sorted(ln[3:] for ln in after.stdout.splitlines() if ln.strip())


def reuse(head: str, path: pathlib.Path, rec: dict, when: str = "before-slot", wait_s: float = 0.0,
          lane: str = "") -> str:
    """Print what a green sweep prints, from its record, and note the run in runs.jsonl."""
    areas = rec["areas"]
    green = f"{sum(areas.values())} green: " + " · ".join(f"{k} {v}" for k, v in areas.items())
    since = " (written while this sweep waited for a slot)" if when == "after-slot" else ""
    out = (f"{head[:12]} already has a green sweep record for this command{since}; not swept again "
           f"(SWEEP_FORCE=1 sweeps anyway)\ngit:            {head} (clean)\nsweep record: {path}\n\n{green}\n")
    print(out, end="", flush=True)
    try:
        import sweep_run
        now = sweep_run.datetime.now(sweep_run.timezone.utc).isoformat()
        sweep_run.append_run({"run_id": f"reuse-{head[:8]}-{os.getpid()}", "sha": head, "clean": True,
                              "caller": sweep_run.caller(), "started_at": now, "finished_at": now, "seconds": 0,
                              "exit": 0, "outcome": "green", "reused": str(path), "reuse": when,
                              "kind": rec.get("kind") or "full", "lane": lane, "wait_s": round(wait_s, 1)})
    except Exception as exc:  # noqa: BLE001 - the record never changes the verdict
        print(f"note: run not recorded ({exc})", file=sys.stderr)
    return out


def reused(args, out: str) -> int:
    """After a reused record: done, or --update-pr from its lines."""
    if not args.update_pr:
        return 0
    sha, green = GIT_LINE.search(out), GREEN_LINE.findall(out)
    return update_pr(args.update_pr, sha.group(1), green[-1].strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--update-pr", metavar="N", help="after a green, recorded sweep, fix PR #N's body")
    ap.add_argument("--lane", metavar="NAME", help="take the lock LOCK.lane-NAME instead of a sweep slot")
    ap.add_argument("--affected", action="store_true", help="only the tests the change touches")
    ap.add_argument("--full", action="store_true", help="the whole suite, even with the merge train on")
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
    linked = config.link_shared_files(root)  # [sweep] link_files: every sweep, the same environment
    if linked:
        print(f"[sweep] linked {', '.join(linked)} from the shared checkout", flush=True)
    changed = format_first(root)
    if changed:
        print(f"[sweep] format changed {len(changed)} file(s): {', '.join(changed[:10])}\n"
              "Commit them (they are your change, formatted) and run the sweep again; sweep NOT run.")
        try:
            import sweep_run
            now = sweep_run.datetime.now(sweep_run.timezone.utc).isoformat()
            sweep_run.append_run({"run_id": f"format-{os.getpid()}", "caller": sweep_run.caller(),
                                  "started_at": now, "finished_at": now, "seconds": 0, "exit": 1,
                                  "outcome": "red-lint", "formatted": changed})
        except Exception as exc:  # noqa: BLE001
            print(f"note: run not recorded ({exc})", file=sys.stderr)
        return 1
    affected = wants_affected(args)
    force = os.environ.get("SWEEP_FORCE") == "1"
    found = None if force else green_record(root, affected)
    if found:
        return reused(args, reuse(*found))
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
    # Another sweep of this commit may have turned green while this one waited.
    found = None if force else green_record(root, affected)
    if found:
        os.close(fd)
        return reused(args, reuse(*found, when="after-slot", wait_s=got - start,
                                  lane=args.lane or os.path.basename(name)))
    # For sweep_run.py's line in runs.jsonl.
    env = dict(os.environ, SWEEP_LANE=args.lane or os.path.basename(name), SWEEP_WAIT_S=f"{got - start:.1f}",
               SWEEP_PR=args.update_pr or os.environ.get("SWEEP_PR", ""))
    proc = subprocess.Popen([sys.executable, RUNNER, *(["--affected"] if affected else [])], cwd=root, env=env,
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
