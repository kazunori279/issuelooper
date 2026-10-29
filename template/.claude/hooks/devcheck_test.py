#!/usr/bin/env python3
"""Cases for devcheck.py. Run it: `python3 devcheck_test.py`.

It runs the real script in a throwaway git repository, with `python3 -m http.server`
standing in for the app, so the slot, the app's process group, the steps and the
record are exercised as a worker would run them.
"""

from __future__ import annotations

import atexit
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

# Every temporary file this file and what it runs make goes under one directory,
# removed at exit: the suite left hundreds of directories in /tmp, and a run's /tmp
# ran out of inodes. Once per process: a test file another one imports keeps it.
if "IL_TEST_TMP" not in os.environ:
    os.environ["IL_TEST_TMP"] = os.environ["TMPDIR"] = tempfile.mkdtemp(prefix="il-test-")
    os.environ["ISSUELOOPER_TMP"] = os.path.join(os.environ["IL_TEST_TMP"], "scratch")
    atexit.register(shutil.rmtree, os.environ["IL_TEST_TMP"], True)
tempfile.tempdir = os.environ["IL_TEST_TMP"]

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "devcheck.py"
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import devcheck  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo() -> pathlib.Path:
    repo = pathlib.Path(tempfile.mkdtemp(prefix="devcheck-repo-"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Tester")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "src").mkdir()
    (repo / "src" / "app.txt").write_text("".join(f"line {i}\n" for i in range(1, 20)))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "update-ref", "refs/remotes/origin/main", git(repo, "rev-parse", "HEAD"))
    (repo / "src" / "app.txt").write_text(
        (repo / "src" / "app.txt").read_text() + "the change\n"
    )
    git(repo, "commit", "-q", "-am", "the change")
    return repo


def script(body: str) -> str:
    fd, path = tempfile.mkstemp(prefix="devcheck-script-", suffix=".py")
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return path


OK_SCRIPT = script(
    "import os, pathlib, urllib.request\n"
    "urllib.request.urlopen(os.environ['DEVCHECK_URL'] + '/', timeout=5).read()\n"
    "pathlib.Path(os.environ['DEVCHECK_OUT'], 'shot-1.png').write_text('png')\n"
)
BAD_SCRIPT = script("import sys\nprint('the card shows the wrong city')\nsys.exit(3)\n")


def env_for(tmp: pathlib.Path, **extra: str) -> dict:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("DEVCHECK_", "POOL_"))
    }
    env.update(
        SHARED_REPO_PATH=str(tmp / "shared"),
        DEVCHECK_START="exec python3 -m http.server $DEVCHECK_PORT --bind 127.0.0.1",
        DEVCHECK_RECORD_DIR=str(tmp / "records"),
        DEVCHECK_LOCK=str(tmp / "devcheck.lock"),
        DEVCHECK_PORT_BASE=str(free_port()),
        DEVCHECK_READY_S="20",
    )
    env.update(extra)
    return env


def run(repo: pathlib.Path, env: dict, *args: str) -> tuple[int, str]:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return done.returncode, done.stdout + done.stderr


def record(env: dict, sha: str) -> dict:
    return json.loads(
        (pathlib.Path(env["DEVCHECK_RECORD_DIR"]) / f"{sha}.json").read_text()
    )


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def test_change_key_ignores_where_the_change_sits() -> None:
    a = (
        "diff --git a/x.js b/x.js\nindex 111..222 100644\n--- a/x.js\n+++ b/x.js\n"
        "@@ -3,3 +3,4 @@\n line 3\n line 4\n+added\n line 5\n"
    )
    moved = (
        "diff --git a/x.js b/x.js\nindex 333..444 100644\n--- a/x.js\n+++ b/x.js\n"
        "@@ -9,3 +9,4 @@\n other context\n line 4\n+added\n line 5 (changed on main)\n"
    )
    other = a.replace("+added", "+added differently")
    check(
        "context, line numbers and blob ids do not change the key",
        devcheck.change_key(a),
        devcheck.change_key(moved),
    )
    check(
        "a different change does",
        devcheck.change_key(a) == devcheck.change_key(other),
        False,
    )
    # A rebase that resolves two additions at one place often leaves a blank line
    # between them, or drops one; most such Gate 7 hand-backs were that.
    blank = a.replace("+added\n", "+added   \n+\n")
    check("an added blank line and trailing whitespace do not change the key",
          devcheck.change_key(a), devcheck.change_key(blank))
    indented = a.replace("+added", "+    added")
    check("indentation does", devcheck.change_key(a) == devcheck.change_key(indented), False)
    # The same own lines, but once the base moved git showed an added line before a
    # removed block in one diff and after it in the other.
    hdr = "diff --git a/feed.js b/feed.js\n--- a/feed.js\n+++ b/feed.js\n@@ -1,5 +1,6 @@\n"
    checked = (hdr + " const card = make();\n+if (!card) return;\n \n-if (narrow()) {\n"
               "-  place(card);\n+const turn = ensure();\n+turn.push(card);\n")
    head = (hdr + " const card = make();\n \n-if (narrow()) {\n-  place(card);\n"
            "+if (!card) return;\n+const turn = ensure();\n+turn.push(card);\n")
    check("PR 101: an added line on the other side of a removed block: the same key",
          devcheck.change_key(checked), devcheck.change_key(head))
    # PR 98: a new method inserted before one with the same closing braces; git put the
    # two lines of `}` at the start of the added block in one diff and at its end in the
    # other.
    hdr = "diff --git a/app.js b/app.js\n--- a/app.js\n+++ b/app.js\n@@ -1,6 +1,12 @@\n"
    checked = (hdr + "   handle(key) {\n    if (key) {\n      this.a();\n+    }\n+  }\n+\n"
               "+  added(v) {\n+    if (v) {\n+      this.b(v);\n     }\n   }\n")
    head = (hdr + "   handle(key) {\n    if (key) {\n      this.a();\n     }\n   }\n+\n"
            "+  added(v) {\n+    if (v) {\n+      this.b(v);\n+    }\n+  }\n")
    check("PR 98: lines of only brackets aligned elsewhere: the same key",
          devcheck.change_key(checked), devcheck.change_key(head))
    # Issue #21: the same lines in another order are another change.
    prefix = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n-old()\n"
    check("the same added lines in another order: another key",
          devcheck.change_key(prefix + "+authorize()\n+transfer_money()\n")
          == devcheck.change_key(prefix + "+transfer_money()\n+authorize()\n"), False)
    swapped_removed = "-a()\n-b()\n+c()\n".join(["diff --git a/z.py b/z.py\n--- a/z.py\n+++ b/z.py\n@@ -1,2 +1 @@\n", ""])
    check("the same removed lines in another order: another key",
          devcheck.change_key(swapped_removed)
          == devcheck.change_key(swapped_removed.replace("-a()\n-b()", "-b()\n-a()")), False)
    changed = head.replace("+      this.b(v);", "+      this.c(v);")
    check("a line that really changed: another key",
          devcheck.change_key(head) == devcheck.change_key(changed), False)
    flipped = prefix + "+authorize()\n-transfer_money()\n"
    check("adding a line and removing it are different changes",
          devcheck.change_key(prefix + "+authorize()\n+transfer_money()\n")
          == devcheck.change_key(flipped), False)
    moved = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n+authorize()\n-old()\n+transfer_money()\n"
    check("a removed line shown between two added ones: the same key",
          devcheck.change_key(prefix + "+authorize()\n+transfer_money()\n"), devcheck.change_key(moved))


def test_a_passing_check_writes_its_record() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="devcheck-"))
    repo = make_repo()
    env = env_for(tmp, DEVCHECK_COMMAND='test -n "$DEVCHECK_URL"')
    head = git(repo, "rev-parse", "HEAD")
    rc, out = run(repo, env, "--script", OK_SCRIPT)
    check(f"exit 0 on a pass ({out[-400:]})", rc, 0)
    check(
        "it prints the verdict and the record",
        (f"devcheck: {head} pass" in out, "devcheck record:" in out),
        (True, True),
    )
    rec = record(env, head)
    check(
        "the record: pass, the smoke first, then the script",
        (rec["status"], [s["name"] for s in rec["steps"]]),
        ("pass", ["smoke", "script 1"]),
    )
    check(
        "its key is the branch's change since origin/main",
        rec["key"],
        devcheck.branch_key(str(repo), "origin/main", head),
    )
    check(
        "the script is saved beside the logs",
        pathlib.Path(rec["scripts"][0]).is_file(),
        True,
    )
    check(
        "the script wrote into DEVCHECK_OUT",
        (pathlib.Path(rec["out"]) / "shot-1.png").is_file(),
        True,
    )
    check(
        "the app is stopped afterwards", port_free(int(env["DEVCHECK_PORT_BASE"])), True
    )
    os.environ["DEVCHECK_RECORD_DIR"] = env["DEVCHECK_RECORD_DIR"]
    try:
        check(
            "find_record takes a unique prefix",
            (devcheck.find_record(head[:10]) or {}).get("sha"),
            head,
        )
    finally:
        os.environ.pop("DEVCHECK_RECORD_DIR")

    # After a new commit, --rerun replays the recorded scripts at the new head.
    (repo / "src" / "app.txt").write_text(
        (repo / "src" / "app.txt").read_text() + "one more\n"
    )
    git(repo, "commit", "-q", "-am", "one more")
    rc, out = run(repo, env, "--rerun", head[:12])
    new = git(repo, "rev-parse", "HEAD")
    check(
        f"--rerun passes at the new head ({out[-300:]})",
        (rc, record(env, new)["status"]),
        (0, "pass"),
    )
    check(
        "with the earlier script",
        [s["name"] for s in record(env, new)["steps"]],
        ["smoke", "script 1"],
    )
    first = pathlib.Path(record(env, head)["scripts"][0]).name
    (repo / "src" / "app.txt").write_text((repo / "src" / "app.txt").read_text() + "and another\n")
    git(repo, "commit", "-q", "-am", "and another")
    run(repo, env, "--rerun", new[:12])
    again = git(repo, "rev-parse", "HEAD")
    check("a rerun of a rerun keeps the script's name (run 8: script-1-script-1-...)",
          pathlib.Path(record(env, again)["scripts"][0]).name, first)


def test_failures_are_recorded_and_exit_1() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="devcheck-"))
    repo = make_repo()
    head = git(repo, "rev-parse", "HEAD")
    env = env_for(tmp)
    rc, out = run(repo, env, "--script", BAD_SCRIPT)
    check("a failing script exits 1", rc, 1)
    check("its output is shown", "the card shows the wrong city" in out, True)
    check("and recorded as a failure", record(env, head)["status"], "fail")

    env = env_for(tmp, DEVCHECK_START="echo cannot start; exit 3")
    rc, out = run(repo, env, "--script", OK_SCRIPT)
    rec = record(env, head)
    check(
        "an app that never comes up fails the check",
        (rc, rec["status"], rec["steps"][0]["name"]),
        (1, "fail", "start"),
    )
    check("and says it exited", "exited" in out, True)


def test_no_record_without_a_clean_commit() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="devcheck-"))
    repo = make_repo()
    (repo / "src" / "app.txt").write_text("uncommitted\n")
    env = env_for(tmp)
    rc, out = run(repo, env, "--script", OK_SCRIPT)
    check(
        "a dirty tree runs but writes no record",
        (rc, "record not written" in out),
        (1, True),
    )
    check(
        "nothing in the record directory",
        list((tmp / "records").glob("*.json")) if (tmp / "records").is_dir() else [],
        [],
    )


def test_stop_group_reaps_after_its_leader_exits() -> None:
    """`uv run` exiting first left the app server it had started still running."""
    pid_file = pathlib.Path(tempfile.mkdtemp(prefix="devcheck-group-")) / "pid"
    try:
        proc = subprocess.Popen(["bash", "-c", f"sleep 60 >/dev/null 2>&1 & echo $! > {pid_file}"],
                                start_new_session=True)
        proc.wait(timeout=10)
        child = int(pid_file.read_text())
        devcheck.stop_group(proc)
        for _ in range(50):
            try:
                os.kill(child, 0)
                state = pathlib.Path(f"/proc/{child}/stat").read_text().split()[2]
            except (OSError, IndexError):
                state = "gone"
            if state in ("gone", "Z"):
                break
            time.sleep(0.1)
        check("the server its leader left behind is stopped", state in ("gone", "Z"), True)
        devcheck.stop_group(proc)  # nothing left: no error
    finally:
        shutil.rmtree(pid_file.parent, ignore_errors=True)


def test_refusals() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="devcheck-"))
    repo = make_repo()
    rc, out = run(repo, env_for(tmp, DEVCHECK_START=""), "--script", OK_SCRIPT)
    check("no start command: exit 2", (rc, "start is not set" in out), (2, True))
    rc, out = run(repo, env_for(tmp))
    check("nothing to run: exit 2", (rc, "nothing to run" in out), (2, True))
    rc, out = run(repo, env_for(tmp, SHARED_REPO_PATH=str(repo)), "--script", OK_SCRIPT)
    check("the shared checkout is refused", (rc, "shared checkout" in out), (2, True))
    rc, out = run(repo, env_for(tmp), "--script", "/nonexistent/replay.py")
    check("a missing script is named", (rc, "/nonexistent/replay.py" in out), (2, True))


def test_update_pr_writes_the_line() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="devcheck-"))
    state = tmp / "body.md"
    state.write_text("<!-- worker -->\nFixes #4\n- **Live check:** 0000000 pass\n")
    gh = tmp / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\nimport pathlib, shutil, sys\n"
        f"state = pathlib.Path({str(state)!r})\n"
        "a = sys.argv[1:]\n"
        "if a[:2] == ['pr', 'view']: print(state.read_text(), end='')\n"
        "elif a[:2] == ['pr', 'edit']: shutil.copyfile(a[a.index('--body-file') + 1], state)\n"
        "else: sys.exit(1)\n"
    )
    gh.chmod(0o755)
    repo = make_repo()
    head = git(repo, "rev-parse", "HEAD")
    rc, out = run(
        repo, env_for(tmp, CLAIM_GH=str(gh)), "--script", OK_SCRIPT, "--update-pr", "4"
    )
    body = state.read_text()
    check(
        f"--update-pr replaces the line ({out[-300:]})",
        (rc, f"- **Live check:** {head} pass (script 1)" in body, "0000000" in body),
        (0, True, False),
    )
    state.write_text("<!-- worker -->\nFixes #4\n")
    run(repo, env_for(tmp, CLAIM_GH=str(gh)), "--script", OK_SCRIPT, "--update-pr", "4")
    check("or appends it", f"- **Live check:** {head} pass" in state.read_text(), True)
    # PR #75: a worker's plain line stayed above the new bold one, and gate 7 read it.
    state.write_text("Fixes #4\nLive check: 73c1601 pass\n- **Sweep SHA:** abc\n- **live check:** 1111111 pass\n")
    run(repo, env_for(tmp, CLAIM_GH=str(gh)), "--script", OK_SCRIPT, "--update-pr", "4")
    body = state.read_text()
    check("a plain line and a bold one: one line left, the new one, in the first one's place",
          (body.lower().count("live check"), "73c1601" in body, "1111111" in body,
           body.splitlines()[1], "- **Sweep SHA:** abc" in body),
          (1, False, False, f"- **Live check:** {head} pass (script 1)", True))
    state.write_text("Fixes #4\nLive check passed on staging yesterday.\n**Live check**: 2222222 pass\n")
    run(repo, env_for(tmp, CLAIM_GH=str(gh)), "--script", OK_SCRIPT, "--update-pr", "4")
    body = state.read_text()
    check("prose about a live check is not a field and stays; `**Live check**:` is one and is rewritten",
          ("Live check passed on staging yesterday." in body, "2222222" in body,
           f"- **Live check:** {head} pass (script 1)" in body), (True, False, True))


def test_the_record_keeps_the_checklist_rows_a_script_declares() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="devcheck-"))
    repo = make_repo()
    env = env_for(tmp)
    rows = script(open(OK_SCRIPT).read() + "# accept-rows: viewport:320x640, lang:ja\n"
                  "#accept-rows: always:keyboard-tab-enter\n"
                  "# accept-skip: live:voice-and-typed: no microphone in headless Chromium\n"
                  "# accept-skip: lang:en\n")
    rc, out = run(repo, env, "--script", rows)
    rec = record(env, git(repo, "rev-parse", "HEAD"))
    check(f"rows and skips with a reason are recorded ({out[-300:]})",
          (rc, rec["rows"], rec["skips"]),
          (0, ["always:keyboard-tab-enter", "lang:ja", "viewport:320x640"],
           {"live:voice-and-typed": "no microphone in headless Chromium"}))
    check("and printed", "checklist: ran always:keyboard-tab-enter lang:ja viewport:320x640; skipped" in out, True)
    (repo / "src" / "app.txt").write_text((repo / "src" / "app.txt").read_text() + "more\n")
    git(repo, "commit", "-q", "-am", "more")
    run(repo, env, "--rerun", rec["sha"][:12])
    check("a rerun replays the saved scripts, so the same rows",
          record(env, git(repo, "rev-parse", "HEAD"))["rows"], rec["rows"])
    run(repo, env, "--script", OK_SCRIPT)
    check("a script that declares nothing: none, but the key is there",
          (record(env, git(repo, "rev-parse", "HEAD"))["rows"], record(env, git(repo, "rev-parse", "HEAD"))["skips"]),
          ([], {}))


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all devcheck cases pass ({len(tests)} groups, {CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
