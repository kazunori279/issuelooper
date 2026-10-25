#!/usr/bin/env python3
"""Check this worktree's head end to end, the way it runs for users, before the PR.

    python3 .claude/hooks/devcheck.py --script $ISSUELOOPER_TMP/wrk-3-12-live.py
    python3 .claude/hooks/devcheck.py --script $ISSUELOOPER_TMP/wrk-3-12-live.py --update-pr 41
    python3 .claude/hooks/devcheck.py --rerun <sha> --update-pr 41    # after a rebase that changed the diff

A developer's last step before a pull request: start the app from the branch in the
real environment (real models, real services, the project's credentials) and use it.
The sweep runs the test suite against test doubles; this runs the app for real. A
worker runs it from its worktree, never from the shared checkout.

1. Takes one of $DEVCHECK_SLOTS slots (`flock`, as the sweep does). Slot k listens
   on $DEVCHECK_PORT_BASE + k, so two checks never share a port.
2. Starts the app with $DEVCHECK_START (`bash -c` at the worktree root, in its own
   process group) and waits until $DEVCHECK_URL$DEVCHECK_READY_PATH answers with
   any status below 500.
3. Runs the project's end-to-end smoke ($DEVCHECK_COMMAND, when set), then each
   --script with $DEVCHECK_SCRIPT_RUNNER (default `python3`). Every step gets
   DEVCHECK_URL, DEVCHECK_PORT and DEVCHECK_OUT (a directory for screenshots) and
   passes when it exits 0.
4. Stops the app's whole process group and writes the record merge Gate 7 reads,
   when the run started from a clean tree on a commit and left HEAD and the tree
   as it found them:

    $DEVCHECK_RECORD_DIR/<sha>.json   (default $ISSUELOOPER_TMP/devcheck)
    {"sha", "key", "status": "pass"|"fail", "steps": [{"name", "command", "rc",
     "seconds", "log"}], "scripts": [<saved copies>], "out", "started_at", "finished_at"}

`key` hashes the lines the branch adds and removes since its fork point, so a
rebase that only moved the branch keeps its pass; a rebase that changed what the
branch does needs a new check (--rerun replays the recorded scripts).

It prints `devcheck: <sha> pass|fail (<steps>)` and `devcheck record: <path>`, and
exits 0 only on a pass. With --update-pr N a recorded pass also writes
`- **Live check:** <sha> pass` into PR #N's body.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import config  # noqa: E402

SELF = ".claude/hooks/devcheck.py"
BODY_LINE = re.compile(r"(?im)^([ \t>*-]*\*\*Live check:\*\*[ \t]*).*$")
_GIT_SCRUB = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def record_dir() -> pathlib.Path:
    return config.record_dirs("devcheck")[0]


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}


def git(root: str, *args: str, stdin: str | None = None) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            input=stdin,
            env=_clean_env(),
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


# --- the key a clean rebase keeps ---------------------------------------------


def change_key(diff: str) -> str:
    """A hash of what a diff changes: each file's name and its added and removed
    lines, in order. Context lines, line numbers and blob ids are left out, so the
    same change on a moved base keeps its key; so are blank lines and trailing
    whitespace, which resolving a rebase conflict adds or drops between two blocks
    (every Gate 7 hand-back in one run was a single added blank line)."""
    keep = []
    for ln in diff.splitlines():
        if ln.startswith("diff --git "):
            keep.append(ln)
        elif ln.startswith(("+++", "---")):
            continue
        elif ln.startswith(("+", "-")) and ln[1:].strip():
            keep.append(ln.rstrip())
    return hashlib.sha256("\n".join(keep).encode()).hexdigest()[:20]


def branch_key(root: str, base: str, head: str) -> str | None:
    """change_key of head's diff from where it forked off base; None when git cannot say."""
    fork = git(root, "merge-base", base, head)
    if not fork:
        return None
    diff = git(root, "diff", "--no-color", "--no-ext-diff", fork.strip(), head)
    return None if diff is None else change_key(diff)


# --- records ----------------------------------------------------------------------


def find_record(sha: str) -> dict | None:
    """The record for a full sha or a unique prefix of one; None when there is none."""
    sha = sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return None
    d = record_dir()
    path = d / f"{sha}.json"
    if len(sha) < 40:
        hits = sorted(d.glob(f"{sha}*.json")) if d.is_dir() else []
        if len(hits) != 1:
            return None
        path = hits[0]
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def write_record(rec: dict) -> pathlib.Path:
    d = record_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{rec['sha']}.json"
    fd, tmp = tempfile.mkstemp(dir=d, prefix="devcheck_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


# --- the slot -----------------------------------------------------------------------


def take_slot(lock: str, slots: int, deadline_s: float) -> tuple[int, int] | None:
    """(fd, slot index) once a slot is free, None after the deadline."""
    names = [(f"{lock}.{k}", k) for k in range(1, slots)] + [(lock, 0)]
    end = time.time() + deadline_s
    while True:
        for name, k in names:
            fd = os.open(name, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, k
            except BlockingIOError:
                os.close(fd)
        if time.time() > end:
            return None
        time.sleep(5)


# --- the app and the steps ------------------------------------------------------------


def stop_group(proc: subprocess.Popen) -> None:
    # The whole group, even when its leader is gone: `uv run` exiting first left the app
    # server it started still running.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def wait_ready(url: str, proc: subprocess.Popen, ready_s: float) -> str:
    """'' once url answers below 500; otherwise why it never did."""
    end = time.time() + ready_s
    while time.time() < end:
        if proc.poll() is not None:
            return f"the app exited ({proc.returncode}) before it answered"
        try:
            with urllib.request.urlopen(url, timeout=5):
                return ""
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return ""
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return f"no answer from {url} in {ready_s:.0f} s"


def run_step(
    name: str, command: str, root: str, env: dict, log: pathlib.Path, timeout_s: float
) -> dict:
    start = time.time()
    with open(log, "w") as fh:
        proc = subprocess.Popen(
            ["bash", "-c", command],
            cwd=root,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            stop_group(proc)
            rc = 124
            fh.write(f"\ndevcheck: {name} timed out after {timeout_s:.0f} s\n")
    seconds = round(time.time() - start, 1)
    print(
        f"  {name}: {'ok' if rc == 0 else f'exit {rc}'} in {seconds:.0f} s  ({log})",
        flush=True,
    )
    if rc != 0:
        tail = log.read_text(errors="replace").splitlines()[-15:]
        print("\n".join(f"    | {ln}" for ln in tail), flush=True)
    return {
        "name": name,
        "command": command,
        "rc": rc,
        "seconds": seconds,
        "log": str(log),
    }


def check(root: str, scripts: list[pathlib.Path], port: int) -> dict:
    start_cmd = _env("DEVCHECK_START")
    smoke = _env("DEVCHECK_COMMAND")
    runner = _env("DEVCHECK_SCRIPT_RUNNER", "python3")
    url = f"http://127.0.0.1:{port}"
    started_at = datetime.now(timezone.utc).isoformat()
    head = (git(root, "rev-parse", "HEAD") or "").strip()
    status = git(root, "status", "--porcelain", "--", ".")
    out = record_dir() / "out" / f"{head[:12] or 'nohead'}-{int(time.time())}"
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, s in enumerate(scripts, 1):
        # A rerun copies copies: strip their prefix, or names nest (script-1-script-1-...).
        name = re.sub(r"^(?:script-\d+-)+", "", s.name)
        copy = out / f"script-{i}-{name}"
        shutil.copyfile(s, copy)
        saved.append(copy)
    env = dict(
        _clean_env(), DEVCHECK_URL=url, DEVCHECK_PORT=str(port), DEVCHECK_OUT=str(out)
    )
    print(
        f"root:     {root}\ngit:      {head or '(no commit)'} ({'dirty' if (status or '').strip() else 'clean'})"
        f"\napp:      {start_cmd}\nurl:      {url}\nout:      {out}",
        flush=True,
    )

    steps: list[dict] = []
    server_log = out / "app.log"
    with open(server_log, "w") as fh:
        app = subprocess.Popen(
            ["bash", "-c", start_cmd],
            cwd=root,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        why = wait_ready(
            url + _env("DEVCHECK_READY_PATH", "/"),
            app,
            float(_env("DEVCHECK_READY_S", "120")),
        )
        if why:
            print(f"  start: {why}  ({server_log})", flush=True)
            steps.append(
                {
                    "name": "start",
                    "command": start_cmd,
                    "rc": 1,
                    "seconds": 0,
                    "log": str(server_log),
                    "error": why,
                }
            )
        else:
            timeout_s = float(_env("DEVCHECK_STEP_TIMEOUT_S", "900"))
            if smoke:
                steps.append(
                    run_step("smoke", smoke, root, env, out / "smoke.log", timeout_s)
                )
            for i, copy in enumerate(saved, 1):
                cmd = f"{runner} {shlex.quote(str(copy))}"
                steps.append(
                    run_step(
                        f"script {i}",
                        cmd,
                        root,
                        env,
                        out / f"script-{i}.log",
                        timeout_s,
                    )
                )
    finally:
        stop_group(app)

    rec = {
        "sha": head,
        "status": "pass" if steps and all(s["rc"] == 0 for s in steps) else "fail",
        "steps": steps,
        "scripts": [str(c) for c in saved],
        "out": str(out),
        "app_log": str(server_log),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    problems = []
    if len(head) != 40:
        problems.append("not on a git commit")
    elif status is None or status.strip():
        problems.append("the tree was dirty at the start (commit first)")
    elif (git(root, "rev-parse", "HEAD") or "").strip() != head:
        problems.append("HEAD moved during the check")
    elif git(root, "status", "--porcelain", "--", ".") != status:
        problems.append(
            "the check changed the working tree (point the app's files at $ISSUELOOPER_TMP)"
        )
    if not problems:
        rec["key"] = branch_key(root, "origin/main", head)
        rec["record"] = str(write_record(rec))
    rec["problems"] = problems
    return rec


def update_pr(pr: str, sha: str, summary: str) -> int:
    gh = os.environ.get("CLAIM_GH", "gh")
    done = subprocess.run(
        [gh, "pr", "view", pr, "--json", "body", "-q", ".body"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        print(f"could not read PR #{pr}: {done.stderr.strip()}", file=sys.stderr)
        return 1
    line = f"- **Live check:** {sha} pass ({summary})"
    body = done.stdout
    if BODY_LINE.search(body):
        body = BODY_LINE.sub(lambda m: line, body, count=1)
    else:
        body = body.rstrip("\n") + f"\n{line}\n"
    config.scratch_root().mkdir(parents=True, exist_ok=True)
    path = str(config.scratch_root() / f"devcheck-pr-{pr}-body.md")
    pathlib.Path(path).write_text(body)
    done = subprocess.run(
        [gh, "pr", "edit", pr, "--body-file", path], capture_output=True, text=True
    )
    if done.returncode != 0:
        print(f"could not edit PR #{pr}: {done.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"PR #{pr} body now says: {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    config.apply()
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--script",
        action="append",
        default=[],
        metavar="PATH",
        help="a script that replays the issue's steps against $DEVCHECK_URL (repeatable)",
    )
    ap.add_argument(
        "--rerun", metavar="SHA", help="replay the scripts recorded for an earlier head"
    )
    ap.add_argument(
        "--update-pr",
        metavar="N",
        help="after a recorded pass, write the Live check line into PR #N",
    )
    args = ap.parse_args(argv)

    top = git(os.getcwd(), "rev-parse", "--show-toplevel")
    root = top.strip() if top else os.getcwd()
    shared = _env("SHARED_REPO_PATH")
    if shared and os.path.realpath(root) == os.path.realpath(shared):
        print(
            "devcheck: refusing to run in the shared checkout; run it in your worktree",
            file=sys.stderr,
        )
        return 2
    if not _env("DEVCHECK_START"):
        print(
            "devcheck: [devcheck] start is not set in .issuelooper/config.toml; nothing to start",
            file=sys.stderr,
        )
        return 2
    scripts = [pathlib.Path(s) for s in args.script]
    if args.rerun:
        old = find_record(args.rerun)
        if not old:
            print(f"devcheck: no record for {args.rerun}", file=sys.stderr)
            return 2
        scripts += [pathlib.Path(s) for s in old.get("scripts") or []]
    missing = [str(s) for s in scripts if not s.is_file()]
    if missing:
        print(f"devcheck: no such script: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not scripts and not _env("DEVCHECK_COMMAND"):
        print(
            "devcheck: nothing to run: pass --script (the issue's steps) or set [devcheck] command",
            file=sys.stderr,
        )
        return 2

    # The slots (and so the ports) are the machine's, shared by every loop on it, as the
    # sweep's are; the records and logs are this repository's ($ISSUELOOPER_TMP).
    lock = _env("DEVCHECK_LOCK", "/tmp/issuelooper-devcheck.lock")
    slots = max(1, int(_env("DEVCHECK_SLOTS", "2")))
    begin = time.time()
    got = take_slot(lock, slots, float(_env("DEVCHECK_LOCK_DEADLINE_S", "1800")))
    if got is None:
        print(f"devcheck: no free slot of {lock} in time; NOT run", file=sys.stderr)
        return 3
    fd, slot = got
    print(f"devcheck slot {slot} taken after {time.time() - begin:.0f} s", flush=True)
    try:
        rec = check(root, scripts, int(_env("DEVCHECK_PORT_BASE", "8700")) + slot)
    finally:
        os.close(fd)

    summary = ", ".join(s["name"] for s in rec["steps"]) or "no step ran"
    print(f"\ndevcheck: {rec['sha'] or '(no commit)'} {rec['status']} ({summary})")
    for p in rec["problems"]:
        print(f"devcheck record not written: {p}")
    if rec.get("record"):
        print(f"devcheck record: {rec['record']}")
    if rec["status"] != "pass":
        return 1
    if args.update_pr:
        if not rec.get("record"):
            print("no devcheck record was written; PR body left alone", file=sys.stderr)
            return 1
        return update_pr(args.update_pr, rec["sha"], summary)
    return 0 if rec.get("record") else 1


if __name__ == "__main__":
    sys.exit(main())
