#!/usr/bin/env python3
"""Run the project's whole check suite and total what it holds, in the sweep format.

    python3 .claude/hooks/sweep_run.py                # the sweep
    python3 .claude/hooks/sweep_run.py --before 44    # ... with the arrow

Workers and the merger run it through `sweep_locked.py`, which takes a sweep slot
first. The suite is $SWEEP_COMMAND (default `make verify`), run with `bash -c` at
the root of the checkout this file lives in, so a copy in a worktree sweeps that
worktree.

It prints, in order: provenance (interpreter, root, command, and
`git: <sha> (clean|dirty)`), the command's output as it runs, and on a green run
the line to paste into a PR body on its own line:

    44 green: unit 21 · e2e 23

Each pytest summary line in the output (`21 passed, 1 warning in 0.97s`) is one
area, counted by the tests that passed. The area is named after the `tests/<dir>`
on the pytest command line echoed before it (`pytest -q tests/unit` -> `unit`),
else `tests`. A run whose output holds no pytest summary is judged by its exit
status alone and reports `0 green: sweep 0`.

When the run is green, started from a clean tree on a commit, and left HEAD and
the tree as it found them, it also writes the provenance record that
`role_guard.py` reads at merge time (Gate 2):

    $SWEEP_RECORD_DIR/<sha>.json   (default $ISSUELOOPER_TMP/sweep)
    {"sha": ..., "command": ..., "areas": {"unit": 21, "e2e": 23}, "total": 44,
     "started_at": ..., "finished_at": ...}

and prints `sweep record: <path>`. A red run prints no green line and exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# No __pycache__ beside the hooks: an untracked file there would make the tree
# read as dirty and the sweep would refuse to write its record.
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import config  # noqa: E402

ROOT = HERE.parents[1]
DEFAULT_COMMAND = "make verify"
# The area a run with no pytest summary reports, so its green line and its record
# still have the `<total> green: <area> <n>` shape Gate 2 parses.
NO_PYTEST_AREA = "sweep"

# Variables that point git at another repository. A sweep started from inside a
# git hook inherits them, and then both the provenance read and any test that
# runs git would act on that repository instead of this checkout.
_GIT_SCRUB = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
)

# `uv run pytest -q tests/unit` or `python -m pytest tests/e2e -x` as echoed by
# make, and pytest's closing line, e.g. `21 passed, 1 warning in 0.97s` or
# `== 1 failed, 20 passed in 1.2s ==`. A path to a file (`tests/test_x.py`) names
# no suite directory.
PYTEST_CMD = re.compile(r"\bpytest\b.*?\btests/([\w-]+)(?=/|\s|$)")
PYTEST_SUMMARY = re.compile(r"^=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s\b.*$")
COUNT = re.compile(r"(\d+) (\w+)")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}


def _git(args: list[str]) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def git_head() -> str | None:
    out = _git(["rev-parse", "HEAD"])
    return out.strip() if out is not None else None


def git_status() -> str | None:
    return _git(["status", "--porcelain", "--", "."])


def parse_suites(output: str) -> tuple[dict[str, int], list[str]]:
    """Passed counts per pytest suite, and any problems that make the run red."""
    counts: dict[str, int] = {}
    problems: list[str] = []
    area = None
    for raw in output.splitlines():
        line = ANSI.sub("", raw)
        m = PYTEST_CMD.search(line)
        if m and "passed" not in line:
            area = m.group(1)
            continue
        s = PYTEST_SUMMARY.match(line.strip())
        if not s:
            continue
        name = area or "tests"
        if name in counts:
            k = 2
            while f"{name}{k}" in counts:
                k += 1
            name = f"{name}{k}"
        tally = {kind: int(n) for n, kind in COUNT.findall(s.group(1))}
        bad = {k: v for k, v in tally.items() if k in ("failed", "error", "errors")}
        if bad:
            problems.append(f"{name}: {s.group(1)}")
        counts[name] = tally.get("passed", 0)
        if tally.get("skipped"):
            print(
                f"note: {name} skipped {tally['skipped']} test(s); only passed tests count"
            )
        area = None
    return counts, problems


def sweep_line(counts: dict[str, int], before: int | None) -> str:
    total = sum(counts.values())
    head = f"{total}" if before is None else f"{before} → {total}"
    return f"{head} green: " + " · ".join(f"{name} {n}" for name, n in counts.items())


def write_sweep_record(
    sha: str, command: str, counts: dict[str, int], started_at: str, finished_at: str
) -> Path:
    record_dir = config.record_dirs("sweep")[0]
    record_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "sha": sha,
        "command": command,
        "areas": counts,
        "total": sum(counts.values()),
        "started_at": started_at,
        "finished_at": finished_at,
    }
    target = record_dir / f"{sha}.json"
    fd, tmp = tempfile.mkstemp(dir=record_dir, prefix="sweep_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


def run_command(command: str) -> tuple[int, str]:
    """Run the suite, echoing its output line by line, and return (status, output)."""
    proc = subprocess.Popen(
        ["bash", "-c", command],
        cwd=ROOT,
        env=_clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return proc.wait(), "".join(lines)


def main(argv=None) -> int:
    config.apply()
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--before", type=int, help="the total the sweep line counts up from"
    )
    args = parser.parse_args(argv)
    command = os.environ.get("SWEEP_COMMAND", "").strip() or DEFAULT_COMMAND

    started_at = datetime.now(timezone.utc).isoformat()
    head_before = git_head()
    status_before = git_status()
    dirty_before = bool(status_before.strip()) if status_before is not None else False

    print(f"sys.executable: {sys.executable}")
    print(f"root:           {ROOT}")
    print(f"command:        {command}")
    if head_before is not None:
        print(f"git:            {head_before} ({'dirty' if dirty_before else 'clean'})")
    else:
        print("git:            (not a git checkout)")
    print(flush=True)

    returncode, output = run_command(command)

    counts, problems = parse_suites(output)
    if returncode != 0:
        problems.insert(0, f"`{command}` exited {returncode}")
    if not counts and not problems:
        counts = {NO_PYTEST_AREA: 0}
        print("\nnote: no pytest summary in the output; the exit status is the verdict")

    integrity: list[str] = []
    if head_before is not None:
        head_after = git_head()
        if head_after != head_before:
            integrity.append(f"git HEAD moved: {head_before} -> {head_after}")
        status_after = git_status()
        if (
            status_after is not None
            and status_before is not None
            and status_after != status_before
        ):
            new = [
                ln
                for ln in status_after.splitlines()
                if ln not in status_before.splitlines()
            ]
            integrity.append(
                "working tree changed during the run (git-ignore what the suite writes):\n"
                + "\n".join(f"  {ln}" for ln in new)
            )

    if problems or integrity:
        print("\n== sweep is red ==")
        for p in problems + integrity:
            print(f"   {p}")
        print("\nsweep record not written")
        return 1

    if head_before is None or len(head_before) != 40:
        print("\nsweep record not written: not on a git commit")
    elif status_before is None:
        print("\nsweep record not written: could not determine git status")
    elif dirty_before:
        print("\nsweep record not written: project tree dirty at start")
    else:
        finished_at = datetime.now(timezone.utc).isoformat()
        path = write_sweep_record(head_before, command, counts, started_at, finished_at)
        print(f"\nsweep record: {path}")

    print(f"\n{sweep_line(counts, args.before)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
