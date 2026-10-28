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

plus "env_id" when `[sweep] env_id` (SWEEP_ENV_ID) is set: a non-secret name for the test
environment (tool versions, say), which sweep_locked.py's reuse of a record must match.

and prints `sweep record: <path>`. A red run prints no green line and exits 1.

With --affected and `[sweep] affected = "<command with {tests}>"` (SWEEP_AFFECTED), it
runs that command with only the tests the commit's change touches, from where it
forked off origin/main: the test files it changes; Python test files that import a
module it changes; test files that name a non-Python file it changes (its name or
stem); for a change to a UI file (JS, HTML, CSS), also every browser test (a test file
that uses Playwright's page), since a page test reaches the change through the page,
not by its file name; and the tests that SWEEP_AFFECTED_MAP (a file of `<glob>: <test>
<test> ...` lines) maps its files to. Tests under SWEEP_AFFECTED_EXCLUDE (default
tests/accept/: the merge train runs the acceptance guard) are left out unless the change
edits them. It runs the whole suite instead when that selection is empty, when it would
take more than SWEEP_AFFECTED_BROWSER_CAP (10) browser test files (the full sweep is
cheaper then), or when the change touches the suite's own setup (SWEEP_AFFECTED_FULL
globs, default conftest.py, pyproject.toml, lock files, Makefile, package.json,
fixtures, .issuelooper/ and .claude/). The record and the run's
line say which: "kind": "affected" or "full", and "tests" (the selection).

Every run, green or red, also appends one JSON line to `runs.jsonl` in that directory,
written from `finally` (SIGTERM included), so a killed sweep is recorded too:

    {"run_id", "sha", "clean", "base": {"main", "merge_base"}, "caller": {"kind",
     "role", "session", "unit", "pr"}, "lane", "wait_s", "started_at", "finished_at",
     "seconds", "exit", "outcome": green | red-tests | red-lint | red-integrity | killed,
     "steps": [{"name", "status", "passed", "failed", "seconds"}],
     "failures": [{"test", "error"}], "changed_files", "junit": [<xml paths>]}

The caller comes from the environment: SWEEP_CALLER (the kind: worker, merger,
resweep, auto-rebase, main-check; else CC_ROLE), CC_ROLE, POOL_WORKER, POOL_UNIT and
SWEEP_PR; sweep_locked.py passes SWEEP_LANE and SWEEP_WAIT_S. red-lint is a failed
command with no failing test in a pytest summary (in practice the lint step). With
SWEEP_JUNIT=1 (`[sweep] junit`, for a pytest suite) each pytest run in the command
also writes `<run_id>-<n>.xml` beside runs.jsonl, through the plugin in
pytest_plugins/ (PYTEST_ADDOPTS); other suites ignore it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
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
SECONDS = re.compile(r" in ([\d.]+)s\b")
# pytest's short summary (`FAILED tests/x.py::t - AssertionError: ...`), a failure's
# section header (`____ test_name ____`) and the first `E   ...` line under it.
FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$")
SECTION = re.compile(r"^_{3,} (\S.*?) _{3,}$")
E_LINE = re.compile(r"^E\s+(.*)$")
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


def steps_of(output: str) -> list[dict]:
    """Each pytest run in the output as a step: its area name (as parse_suites names
    it), passed and failed counts, seconds, and status."""
    steps: list[dict] = []
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
        tally = {kind: int(n) for n, kind in COUNT.findall(s.group(1))}
        failed = sum(v for k, v in tally.items() if k in ("failed", "error", "errors"))
        sec = SECONDS.search(line)
        steps.append({"name": area or "tests", "status": "failed" if failed else "passed",
                      "passed": tally.get("passed", 0), "failed": failed,
                      "seconds": float(sec.group(1)) if sec else None})
        area = None
    return steps


def failures_of(output: str) -> list[dict]:
    """Each failing test pytest names (`FAILED <node id>`), with the first `E` line of
    its traceback (or the reason on its summary line)."""
    first_e: dict[str, str] = {}
    section = None
    for raw in output.splitlines():
        line = ANSI.sub("", raw).rstrip()
        m = SECTION.match(line)
        if m:
            section = m.group(1)
            continue
        e = E_LINE.match(line)
        if e and section and section not in first_e:
            first_e[section] = e.group(1).strip()[:300]
    out = []
    for raw in output.splitlines():
        m = FAILED_LINE.match(ANSI.sub("", raw).strip())
        if not m or any(f["test"] == m.group(1) for f in out):
            continue
        name = m.group(1).split("::")[-1]
        cls = "::".join(m.group(1).split("::")[1:])
        error = first_e.get(name) or first_e.get(cls.replace("::", ".")) or (m.group(2) or "")
        out.append({"test": m.group(1), "error": error[:300]})
    return out


def runs_path() -> Path:
    return config.record_dirs("sweep")[0] / "runs.jsonl"


def append_run(record: dict) -> None:
    """One line per run, in a single write (O_APPEND keeps concurrent lines whole)."""
    path = runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def caller() -> dict:
    role = os.environ.get("CC_ROLE", "")
    return {"kind": os.environ.get("SWEEP_CALLER", "") or role or "manual", "role": role,
            "session": os.environ.get("POOL_WORKER", ""), "unit": os.environ.get("POOL_UNIT", ""),
            "pr": os.environ.get("SWEEP_PR", "") or os.environ.get("MERGE_PR", "")}


def base_of(head: str | None) -> tuple[dict, list[str]]:
    """origin/main and the merge base of HEAD with it, and the files HEAD changes since."""
    main = (_git(["rev-parse", "origin/main"]) or "").strip()
    fork = (_git(["merge-base", "origin/main", "HEAD"]) or "").strip() if head and main else ""
    files = (_git(["diff", "--name-only", fork, "HEAD"]) or "").split() if fork else []
    return {"main": main, "merge_base": fork}, files


def junit_env(env: dict[str, str], prefix: Path) -> dict[str, str]:
    """PYTEST_ADDOPTS and PYTHONPATH for the plugin that gives each pytest run its own
    junit XML (a single --junitxml would be overwritten by the next pytest run)."""
    plugins = str(HERE / "pytest_plugins")
    env = dict(env)
    env["PYTEST_ADDOPTS"] = (env.get("PYTEST_ADDOPTS", "") + " -p sweep_junit").strip()
    env["PYTHONPATH"] = os.pathsep.join(p for p in (plugins, env.get("PYTHONPATH", "")) if p)
    env["SWEEP_JUNIT_PREFIX"] = str(prefix)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # no __pycache__ beside the plugin: the tree must not change
    return env


def sweep_line(counts: dict[str, int], before: int | None) -> str:
    total = sum(counts.values())
    head = f"{total}" if before is None else f"{before} → {total}"
    return f"{head} green: " + " · ".join(f"{name} {n}" for name, n in counts.items())


def write_sweep_record(
    sha: str, command: str, counts: dict[str, int], started_at: str, finished_at: str,
    kind: str = "full", tests: list[str] | None = None,
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
        "kind": kind,
    }
    if kind == "affected":
        record["tests"] = tests or []
    if os.environ.get("SWEEP_ENV_ID", "").strip():
        record["env_id"] = os.environ["SWEEP_ENV_ID"].strip()
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


DEFAULT_FULL = ("conftest.py", "*/conftest.py", "pyproject.toml", "*.lock", "requirements*.txt", "Makefile",
                "package.json", "package-lock.json", "setup.cfg", "tox.ini", "pytest.ini", "*/fixtures/*",
                ".issuelooper/*", ".claude/*")
TEST_FILE = re.compile(r"(?:^|/)(?:test_[\w-]+|[\w-]+_test)\.py$")
DEFAULT_EXCLUDE = ("tests/accept/",)
UI_FILE = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".scss", ".vue", ".svelte")
BROWSER = re.compile(r"playwright|page\.goto\(|\bPage\b|chromium|def test_\w*\([^)]*\bpage\b")


def select_tests(changed: list[str], tracked: list[str], read=None, mapping: str = "",
                 full_globs: tuple[str, ...] = DEFAULT_FULL, exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
                 browser_cap: int = 10) -> tuple[list[str], str]:
    """The test files a change touches (see the module docstring), and why the whole suite
    must run instead ('' when the selection stands). In cohort 55 a JS file's name pulled
    about 13 browser tests under tests/accept into every affected sweep (110 s median,
    against 48 s for the full sweep then); in run 10b an e2e test that drove the changed
    code by data-testid only, not naming its file, was left out (#104)."""
    import fnmatch
    read = read or (lambda p: (ROOT / p).read_text(errors="replace"))
    for f in changed:
        if any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(os.path.basename(f), g) for g in full_globs):
            return [], f"it changes {f}, part of the suite's setup"
    every = [t for t in tracked if TEST_FILE.search(t)]
    tests = [t for t in every if not t.startswith(tuple(exclude))]
    picked = {f for f in changed if TEST_FILE.search(f) and f in every}
    texts: dict[str, str] = {}

    def text(t: str) -> str:
        if t not in texts:
            try:
                texts[t] = read(t)
            except OSError:
                texts[t] = ""
        return texts[t]
    def browser(t: str) -> bool:
        return bool(BROWSER.search(text(t)))
    if any(f.endswith(UI_FILE) and not TEST_FILE.search(f) for f in changed):
        picked |= {t for t in tests if browser(t)}
    for f in changed:
        if TEST_FILE.search(f):
            continue
        if f.endswith(".py"):
            parts = f[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            mods = [".".join(parts[k:]) for k in range(len(parts)) if len(parts) - k >= 2] or [".".join(parts)]
            pats = [re.compile(rf"^\s*(?:import|from)\s+{re.escape(m)}\b", re.M) for m in mods]
            pats += [re.compile(rf"^\s*from\s+{re.escape(m.rsplit('.', 1)[0])}\s+import\s+[^\n]*\b{re.escape(m.rsplit('.', 1)[1])}\b", re.M)
                     for m in mods if "." in m]
            picked |= {t for t in tests if any(p.search(text(t)) for p in pats)}
        else:
            name = os.path.basename(f)
            stem = name.rsplit(".", 1)[0]
            pat = re.compile(rf"(?<![\w-]){re.escape(stem)}(?![\w-])" if len(stem) >= 3 else re.escape(name))
            picked |= {t for t in tests if pat.search(text(t))}
    for line in mapping.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        glob, targets = line.split(":", 1)
        if any(fnmatch.fnmatch(f, glob.strip()) for f in changed):
            picked |= {t for t in targets.split() if t in tracked}
    if not picked:
        return [], "no test is related to the change"
    pages = [t for t in picked if t not in changed and browser(t)]
    if len(pages) > browser_cap:
        return [], f"it would run {len(pages)} browser test files, over {browser_cap}: the full sweep costs less"
    return sorted(picked), ""


def affected_selection(head: str | None) -> tuple[list[str], str]:
    """select_tests for HEAD's change since it forked off origin/main, read from git."""
    _, changed = base_of(head)
    if not changed:
        return [], "nothing changed since origin/main (or git cannot say)"
    tracked = (_git(["ls-files"]) or "").split()
    mapping = ""
    map_file = os.environ.get("SWEEP_AFFECTED_MAP", "").strip()
    if map_file:
        try:
            mapping = (ROOT / map_file).read_text()
        except OSError:
            pass
    full = tuple(g.strip() for g in os.environ.get("SWEEP_AFFECTED_FULL", "").split(",") if g.strip()) or DEFAULT_FULL
    raw = os.environ.get("SWEEP_AFFECTED_EXCLUDE")
    exclude = DEFAULT_EXCLUDE if raw is None else tuple(g.strip() for g in raw.split(",") if g.strip())
    try:
        cap = int(os.environ.get("SWEEP_AFFECTED_BROWSER_CAP", "") or 10)
    except ValueError:
        cap = 10
    return select_tests(changed, tracked, mapping=mapping, full_globs=full, exclude=exclude, browser_cap=cap)


def run_command(command: str, env: dict[str, str] | None = None, sink: list[str] | None = None) -> tuple[int, str]:
    """Run the suite, echoing its output line by line, and return (status, output).
    Lines also go to sink as they come, for the record of a run that is killed."""
    proc = subprocess.Popen(
        ["bash", "-c", command],
        cwd=ROOT,
        env=env or _clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    lines: list[str] = sink if sink is not None else []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return proc.wait(), "".join(lines)


def main(argv=None) -> int:
    """The sweep, with its line in runs.jsonl written however it ends."""
    config.apply()
    run = {"started": time.time(), "outcome": "killed", "exit": None, "output": []}

    def stopped(signum, _frame):
        raise SystemExit(128 + signum)
    old = signal.signal(signal.SIGTERM, stopped)
    try:
        return sweep(argv, run)
    finally:
        signal.signal(signal.SIGTERM, old)
        try:
            write_run(run)
        except Exception as exc:  # noqa: BLE001 - the record never changes the verdict
            print(f"note: run not recorded ({exc})", file=sys.stderr)


def write_run(run: dict) -> None:
    out = "".join(run["output"])
    sha = run.get("sha")
    base, files = base_of(sha)
    steps = steps_of(out)
    outcome = run["outcome"]
    if outcome not in ("green", "killed", "red-integrity"):
        outcome = "red-tests" if any(st["failed"] for st in steps) or failures_of(out) else "red-lint"
    prefix = run.get("junit_prefix")
    append_run({
        "run_id": run.get("run_id") or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{os.getpid()}",
        "sha": sha, "clean": run.get("clean"), "base": base, "caller": caller(),
        "lane": os.environ.get("SWEEP_LANE", ""), "wait_s": float(os.environ.get("SWEEP_WAIT_S", "") or 0),
        "started_at": datetime.fromtimestamp(run["started"], timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "seconds": round(time.time() - run["started"], 1), "exit": run["exit"], "outcome": outcome,
        "kind": run.get("kind", "full"), "selected": run.get("selected", []),
        "steps": steps, "failures": failures_of(out), "changed_files": files,
        "junit": sorted(str(p) for p in prefix.parent.glob(prefix.name + "-*.xml")) if prefix else [],
    })


def sweep(argv, run: dict) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--before", type=int, help="the total the sweep line counts up from"
    )
    parser.add_argument("--affected", action="store_true",
                        help="only the tests the change touches, with $SWEEP_AFFECTED")
    args = parser.parse_args(argv)
    command = os.environ.get("SWEEP_COMMAND", "").strip() or DEFAULT_COMMAND
    kind, selected, recorded = "full", [], command

    started_at = datetime.now(timezone.utc).isoformat()
    head_before = git_head()
    status_before = git_status()
    dirty_before = bool(status_before.strip()) if status_before is not None else False
    run.update(sha=head_before, clean=not dirty_before,
               run_id=f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{(head_before or 'nohead')[:8]}-{os.getpid()}")
    env = _clean_env()
    if os.environ.get("SWEEP_JUNIT", "").strip().lower() in ("1", "true", "yes", "on"):
        run["junit_prefix"] = runs_path().parent / run["run_id"]
        runs_path().parent.mkdir(parents=True, exist_ok=True)
        env = junit_env(env, run["junit_prefix"])

    if args.affected:
        template = os.environ.get("SWEEP_AFFECTED", "").strip()
        selected, why = affected_selection(head_before) if "{tests}" in template else ([], "no [sweep] affected command")
        if selected:
            import shlex
            kind, recorded = "affected", template
            command = template.replace("{tests}", " ".join(shlex.quote(t) for t in selected))
            print(f"affected:       {len(selected)} test file(s): {', '.join(selected[:8])}"
                  + (" ..." if len(selected) > 8 else ""))
        else:
            print(f"affected:       the full sweep instead: {why}")
    run.update(kind=kind, selected=selected)
    print(f"sys.executable: {sys.executable}")
    print(f"root:           {ROOT}")
    print(f"command:        {command}")
    if head_before is not None:
        print(f"git:            {head_before} ({'dirty' if dirty_before else 'clean'})")
    else:
        print("git:            (not a git checkout)")
    print(flush=True)

    returncode, output = run_command(command, env, run["output"])
    run["exit"], run["outcome"] = returncode, "red"

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

    if integrity:
        run["outcome"] = "red-integrity"
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
        path = write_sweep_record(head_before, recorded, counts, started_at, finished_at, kind, selected)
        print(f"\nsweep record: {path}")

    run["outcome"] = "green"
    print(f"\n{sweep_line(counts, args.before)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
