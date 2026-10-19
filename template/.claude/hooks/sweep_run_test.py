#!/usr/bin/env python3
"""Cases for sweep_run.py. Run it: `python3 sweep_run_test.py`.

Each case copies sweep_run.py (and config.py) into a fresh git repository under
.claude/hooks/, commits, and runs it with a fake $SWEEP_COMMAND that prints what
make and pytest would. That is the layout a vendored install has, and it is how
the runner finds the root it sweeps.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import sweep_run  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(config_toml: str = "") -> pathlib.Path:
    repo = pathlib.Path(tempfile.mkdtemp(prefix="sweep-run-test-"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "test@example.com")
    hooks = repo / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("sweep_run.py", "config.py"):
        shutil.copy(HERE / name, hooks / name)
    if config_toml:
        (repo / ".issuelooper").mkdir()
        (repo / ".issuelooper" / "config.toml").write_text(config_toml)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def sweep(repo: pathlib.Path, command: str | None, *args: str, **env_extra: str):
    """Run the repo's copy of the runner. Returns (exit, stdout, record dir)."""
    records = pathlib.Path(tempfile.mkdtemp(prefix="sweep-run-records-"))
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("SWEEP_COMMAND", "SHARED_REPO_PATH", "GIT_DIR", "GIT_WORK_TREE")
    }
    env["SWEEP_RECORD_DIR"] = str(records)
    if command is not None:
        env["SWEEP_COMMAND"] = command
    env.update(env_extra)
    done = subprocess.run(
        [sys.executable, str(repo / ".claude" / "hooks" / "sweep_run.py"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done.returncode, done.stdout + done.stderr, records


def last_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


TWO_SUITES = (
    "echo 'uv run ruff check .'; "
    "echo 'uv run pytest -q tests/unit'; echo '...'; echo '3 passed in 0.10s'; "
    "echo 'uv run pytest -q tests/e2e'; echo '..'; echo '== 2 passed, 1 warning in 1.20s =='"
)


def test_green_two_suites_writes_record() -> None:
    repo = make_repo()
    sha = git(repo, "rev-parse", "HEAD")
    rc, out, records = sweep(repo, TWO_SUITES)
    check("a green run exits 0", rc, 0)
    check(
        "provenance names the clean head", f"git:            {sha} (clean)" in out, True
    )
    check("provenance names the command", f"command:        {TWO_SUITES}" in out, True)
    check("the command's output is passed through", "uv run ruff check ." in out, True)
    check(
        "the green line is last, one area per suite",
        last_line(out),
        "5 green: unit 3 · e2e 2",
    )
    record_path = records / f"{sha}.json"
    check("the record path is printed", f"sweep record: {record_path}" in out, True)
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    check("the record names the sha", record.get("sha"), sha)
    check("the record holds the areas", record.get("areas"), {"unit": 3, "e2e": 2})
    check("the record holds the total", record.get("total"), 5)
    check("the record names the command", record.get("command"), TWO_SUITES)
    check(
        "the record is timed",
        bool(record.get("started_at") and record.get("finished_at")),
        True,
    )
    check(
        "no temp files are left beside the record",
        sorted(p.name for p in records.iterdir()),
        [f"{sha}.json"],
    )


def test_before_arrow() -> None:
    repo = make_repo()
    rc, out, _ = sweep(repo, TWO_SUITES, "--before", "4")
    check(
        "--before draws the arrow",
        (rc, last_line(out)),
        (0, "4 → 5 green: unit 3 · e2e 2"),
    )


def test_no_pytest_summary_is_judged_by_exit_status() -> None:
    repo = make_repo()
    sha = git(repo, "rev-parse", "HEAD")
    rc, out, records = sweep(repo, "echo 'npm test'; echo 'all good'")
    check("a passing command with no pytest summary is green", rc, 0)
    check("its green line has the one fixed area", last_line(out), "0 green: sweep 0")
    record = json.loads((records / f"{sha}.json").read_text())
    check(
        "and its record matches the line",
        (record["areas"], record["total"]),
        ({"sweep": 0}, 0),
    )

    rc, out, records = sweep(repo, "echo 'npm test'; exit 2")
    check("a failing command with no pytest summary is red", rc, 1)
    check("the red run names the exit status", "exited 2" in out, True)
    check("a red run prints no green line", " green: " in out, False)
    check("a red run writes no record", list(records.iterdir()), [])


def test_red_runs() -> None:
    repo = make_repo()
    rc, out, records = sweep(repo, TWO_SUITES + "; exit 1")
    check("a nonzero exit is red even with passing summaries", rc, 1)
    check("and writes no record", list(records.iterdir()), [])
    check("and says so", "sweep record not written" in out, True)

    rc, out, _ = sweep(
        repo, "echo 'pytest tests/unit'; echo '1 failed, 4 passed in 0.5s'"
    )
    check("a failed test in a summary is red even with exit 0", rc, 1)
    check("the problem names the suite", "unit: 1 failed, 4 passed" in out, True)


def test_dirty_tree_writes_no_record() -> None:
    repo = make_repo()
    (repo / "scratch.txt").write_text("uncommitted\n")
    rc, out, records = sweep(repo, TWO_SUITES)
    check("a dirty tree still sweeps green", rc, 0)
    check("its provenance says dirty", "(dirty)" in out, True)
    check("but writes no record", list(records.iterdir()), [])
    check("and says why", "project tree dirty at start" in out, True)
    check("the green line is still printed", last_line(out), "5 green: unit 3 · e2e 2")


def test_tree_or_head_changing_is_red() -> None:
    repo = make_repo()
    rc, out, records = sweep(repo, TWO_SUITES + "; echo junk > left_behind.txt")
    check("a file left by the suite turns the run red", rc, 1)
    check("and names it", "left_behind.txt" in out, True)
    check("no record", list(records.iterdir()), [])

    repo = make_repo()
    rc, out, _ = sweep(repo, TWO_SUITES + "; git commit -q --allow-empty -m moved")
    check("a suite that moves HEAD is red", (rc, "git HEAD moved" in out), (1, True))


def test_area_names() -> None:
    counts, problems = sweep_run.parse_suites(
        "pytest -q tests\n3 passed in 0.1s\n"
        "python -m pytest tests/test_one.py\n2 passed in 0.1s\n"
        "pytest tests/api/test_x.py -x\n4 passed, 1 skipped in 0.2s\n"
    )
    check(
        "no suite directory -> tests, then tests2; a file names its directory",
        (counts, problems),
        ({"tests": 3, "tests2": 2, "api": 4}, []),
    )

    counts, _ = sweep_run.parse_suites(
        "\x1b[32m7 passed\x1b[0m\x1b[32m in 0.30s\x1b[0m\n"
    )
    check("a colored summary is still counted", counts, {"tests": 7})

    counts, _ = sweep_run.parse_suites(
        "uv run pytest tests/unit\nplatform linux -- Python 3.12, pytest-8.3.0\n"
        "rootdir: /x\ncachedir: .pytest_cache\n9 passed in 2.00s (0:00:02)\n"
    )
    check("pytest's header lines do not reset the area", counts, {"unit": 9})


def test_config_file_supplies_the_command() -> None:
    repo = make_repo(
        '[sweep]\ncommand = "echo pytest tests/cfg; echo 6 passed in 0.1s"\n'
    )
    rc, out, _ = sweep(repo, None)
    check(
        "with SWEEP_COMMAND unset the config's command runs",
        (rc, last_line(out)),
        (0, "6 green: cfg 6"),
    )
    rc, out, _ = sweep(repo, "echo 'pytest tests/env'; echo '1 passed in 0.1s'")
    check(
        "an exported SWEEP_COMMAND wins over the file", last_line(out), "1 green: env 1"
    )


def test_git_dir_from_the_caller_is_ignored() -> None:
    repo = make_repo()
    other = make_repo()
    (other / "x.txt").write_text("x\n")
    git(other, "add", "x.txt")
    git(other, "commit", "-q", "-m", "other")
    sha = git(repo, "rev-parse", "HEAD")
    rc, out, records = sweep(repo, TWO_SUITES, GIT_DIR=str(other / ".git"))
    check(
        "an inherited GIT_DIR does not change what is swept",
        (
            rc,
            f"git:            {sha} (clean)" in out,
            (records / f"{sha}.json").exists(),
        ),
        (0, True, True),
    )


def test_no_bytecode_beside_the_hooks() -> None:
    repo = make_repo()
    sweep(repo, TWO_SUITES)
    check(
        "running the sweep leaves no __pycache__ in .claude/hooks",
        (repo / ".claude" / "hooks" / "__pycache__").exists(),
        False,
    )


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all sweep_run cases pass ({len(tests)} groups, {CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
