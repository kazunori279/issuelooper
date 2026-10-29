#!/usr/bin/env python3
"""Cases for role_guard.py. Run it: `python3 role_guard_test.py`.

Two of the guard's worst defects were one assertion each: `allow()` printed a bare
`{}`, which agy reads as a refusal (it blocked the merger on its own first
command), and the fail-open handler had the same shape. Hence this file.

It drives the guard as a subprocess with a payload on stdin, exactly as a host does,
so it tests the wire contract rather than the functions. Two hosts answer differently
for the same verdict and getting that mapping wrong is the whole of the first defect,
so `run()` returns a plain "allow"/"refuse" and the host difference is asserted
separately in `test_host_shapes`.
"""

from __future__ import annotations

import atexit
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

# Every temporary file this file and what it runs make goes under one directory,
# removed at exit: the suite left hundreds of directories in /tmp, and a run's /tmp
# ran out of inodes. Once per process: a test file another one imports keeps it.
if "IL_TEST_TMP" not in os.environ:
    os.environ["IL_TEST_TMP"] = os.environ["TMPDIR"] = tempfile.mkdtemp(prefix="il-test-")
    os.environ["ISSUELOOPER_TMP"] = os.path.join(os.environ["IL_TEST_TMP"], "scratch")
    atexit.register(shutil.rmtree, os.environ["IL_TEST_TMP"], True)
tempfile.tempdir = os.environ["IL_TEST_TMP"]

GUARD = pathlib.Path(__file__).with_name("role_guard.py")
RUNNER = ".claude/hooks/sweep_run.py"

CLAUDE, AGY = "claude", "agy"

# Isolate all tests from the real heartbeat files and sweep records in /tmp.
TEST_TMPDIR = tempfile.TemporaryDirectory(prefix="role_guard_test_")
TEST_HEARTBEAT = pathlib.Path(TEST_TMPDIR.name) / "role_guard.last"
TEST_RUNS = pathlib.Path(TEST_TMPDIR.name) / "role_guard_runs"
os.environ["ROLE_GUARD_HEARTBEAT_FILE"] = str(TEST_HEARTBEAT)
os.environ["ROLE_GUARD_RUNS_DIR"] = str(TEST_RUNS)
TEST_SWEEP_RECORDS = pathlib.Path(TEST_TMPDIR.name) / "sweep_records"
os.environ["SWEEP_RECORD_DIR"] = str(TEST_SWEEP_RECORDS)
# Gate 5 (the review record) has its own cases below; the other merge cases leave it off.
os.environ["REVIEW_GATE"] = "off"
# The secret-URL rule is off unless a case turns it on.
os.environ["GUARD_SECRET_URLS"] = ""
# Live checks on, so Gate 4 applies; one case below turns them off.
os.environ["POOL_LIVE"] = "1"
# The live check (Gate 7) is off unless a case turns it on; its records are isolated.
os.environ["POOL_DEVCHECK"] = "0"
os.environ["DEVCHECK_PATHS"] = ""
TEST_DEVCHECK_RECORDS = pathlib.Path(TEST_TMPDIR.name) / "devcheck_records"
os.environ["DEVCHECK_RECORD_DIR"] = str(TEST_DEVCHECK_RECORDS)
# The guard reads .issuelooper/config.toml from $SHARED_REPO_PATH; none here.
os.environ["SHARED_REPO_PATH"] = TEST_TMPDIR.name


def write_sweep_record(sha: str, green_line: str) -> None:
    """The record sweep_run.py would have written for `green_line` at `sha`."""
    total, rest = green_line.split(" green:", 1)
    areas = {s: int(n) for s, n in re.findall(r"([a-zA-Z0-9_-]+)\s+(\d+)", rest)}
    TEST_SWEEP_RECORDS.mkdir(parents=True, exist_ok=True)
    (TEST_SWEEP_RECORDS / f"{sha}.json").write_text(
        json.dumps({"sha": sha, "areas": areas, "total": int(total)}))


def run(
    command: str,
    role: str = "monitor",
    host: str = CLAUDE,
    extra_env: dict | None = None,
    cwd: str | None = None,
) -> tuple[str, str]:
    """Returns (verdict, reason), verdict in {"allow", "refuse"}. cwd: Claude Code's
    working directory for the call (agy sends none)."""
    if host == CLAUDE:
        payload = {"tool_input": {"command": command}, "session_id": "test-session"}
        if cwd:
            payload["cwd"] = cwd
    else:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": command}},
            "conversationId": "test-conversation",
        }
    env = dict(os.environ)
    if role:
        env["CC_ROLE"] = role
    else:
        env.pop("CC_ROLE", None)
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    out = json.loads(proc.stdout or "{}")
    decision = out.get("decision", "")
    verdict = "refuse" if decision in ("block", "deny") else "allow"
    return verdict, out.get("reason", "")


FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


# --------------------------------------------------------------------------- #
# Secret URLs (GUARD_SECRET_URLS)
#
# Some endpoints answer with a secret in the clear because a browser needs it.
# A fetch that looks like a health check then prints the secret into the
# transcript. The rule binds every role, so the cases run for several.
# --------------------------------------------------------------------------- #

SECRETS = {"GUARD_SECRET_URLS": "/api/settings, /admin/token"}


def test_secret_urls() -> None:
    for role in ("monitor", "merger", "tester", "worker"):
        verdict, reason = run("curl -s http://localhost:8080/api/settings", role=role,
                              extra_env=SECRETS)
        check(f"bare curl of a secret URL is refused for {role}", verdict, "refuse")
        check(f"the refusal for {role} names the python filter", "python3 -c" in reason, True)
        check(f"the refusal for {role} names the status probe", "-o /dev/null" in reason, True)

    verdict, reason = run("wget -qO- https://staging.example.com/admin/token", role="merger",
                          extra_env=SECRETS)
    check("every listed path is guarded, wget too", verdict, "refuse")
    check("the refusal names the path", "/admin/token" in reason, True)

    # Over ssh the endpoint is inside a quoted argument rather than at the top level.
    verdict, _ = run("""ssh devbox "curl -s localhost:8080/api/settings" """, role="merger",
                     extra_env=SECRETS)
    check("curl inside an ssh argument is refused", verdict, "refuse")

    # A jq or sed redaction prints the secret when it fails or misses the field.
    verdict, _ = run("curl -s localhost:8080/api/settings | jq 'del(.api_key)'", role="merger",
                     extra_env=SECRETS)
    check("a jq redaction is refused", verdict, "refuse")

    verdict, _ = run("curl -s localhost:8080/api/settings-v2", role="merger", extra_env=SECRETS)
    check("a longer path that merely starts with a listed one is allowed", verdict, "allow")

    verdict, _ = run("curl -s localhost:8080/api/settings", role="merger",
                     extra_env={"GUARD_SECRET_URLS": ""})
    check("with GUARD_SECRET_URLS empty the rule is off", verdict, "allow")


def test_secret_urls_allow_the_safe_forms() -> None:
    safe = (
        """ssh devbox 'curl -s localhost:8080/api/settings | python3 -c """
        '''"import json,sys; d=json.load(sys.stdin); print(d[\\"revision\\"]); print(sorted(d))"' '''
    )
    verdict, _ = run(safe, role="merger", extra_env=SECRETS)
    check("a python filter that selects field names is allowed", verdict, "allow")

    verdict, _ = run(
        "curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/settings",
        role="merger", extra_env=SECRETS,
    )
    check("a status-code probe that discards the body is allowed", verdict, "allow")

    # Reading the source is not fetching the endpoint, and sessions grep for paths
    # constantly. A rule that catches the grep would be turned off in an hour.
    verdict, _ = run("grep -rn '/api/settings' src/", role="monitor", extra_env=SECRETS)
    check("grepping the source for the path is allowed", verdict, "allow")

    # The scheme is part of what you grep for, so a fetcher list containing `http`
    # would refuse the grep for containing its own URL.
    verdict, _ = run("grep -rn 'http://localhost:8080/api/settings' docs/", role="monitor",
                     extra_env=SECRETS)
    check("grepping for the full URL is allowed", verdict, "allow")

    verdict, _ = run("curl -s http://localhost:8080/api/health", role="merger", extra_env=SECRETS)
    check("another endpoint on the same host is allowed", verdict, "allow")


def test_secret_urls_come_from_the_config_file() -> None:
    repo = pathlib.Path(tempfile.mkdtemp(prefix="role-guard-config-"))
    (repo / ".issuelooper").mkdir()
    (repo / ".issuelooper" / "config.toml").write_text('[guard]\nsecret_urls = ["/v1/keys"]\n')
    env = {k: v for k, v in os.environ.items() if k != "GUARD_SECRET_URLS"}
    env["CC_ROLE"] = "merger"
    env["SHARED_REPO_PATH"] = str(repo)
    payload = {"tool_input": {"command": "curl -s localhost:9000/v1/keys"}, "session_id": "s"}
    out = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                         capture_output=True, text=True, env=env, check=True).stdout
    check("guard.secret_urls in config.toml turns the rule on",
          json.loads(out or "{}").get("decision"), "block")


def test_kill_by_pattern() -> None:
    # A session's own command line carries its prompt, which names the server it
    # was told to stop, so `pkill -f` on that name matches the session itself.
    for role in ("tester", "worker", "merger", "monitor"):
        for cmd in ('pkill -f "myapp --port 8021"', "sleep 1; killall uvicorn",
                    "pkill -9 -f uvicorn || true"):
            verdict, reason = run(cmd, role=role)
            check(f"{cmd!r} is refused for {role}", verdict, "refuse")
        check(f"the refusal for {role} names the substitute", "lsof -tiTCP:" in reason, True)
    for cmd in ("lsof -tiTCP:8021 -sTCP:LISTEN | xargs kill", "kill $(cat /tmp/app.pid)"):
        verdict, _ = run(cmd, role="tester")
        check(f"{cmd!r} is allowed", verdict, "allow")
    # The word alone is enough (a grep for it is refused too): cheap, and never needed.
    verdict, _ = run("grep -n pkill SKILL.md", role="tester")
    check("any command naming pkill is refused", verdict, "refuse")


def test_background_commands_must_let_go_of_the_terminal() -> None:
    # Run 11: `nohup uv run concierge --port 8121 ... &` held agy until a 70-minute timeout.
    refused = ["nohup uv run concierge --port 8121 &",
               "nohup uv run concierge --port 8121 > $ISSUELOOPER_TMP/s.log 2>&1 &",
               "uv run concierge --port 8121 > s.log &",
               "setsid -f uv run concierge --port 8121",
               "cmd 2>&1 > s.log < /dev/null &",
               'bash -c "nohup uv run concierge &"']
    allowed = ["nohup uv run concierge --port 8121 > $ISSUELOOPER_TMP/s.log 2>&1 < /dev/null &",
               "setsid -f uv run concierge > $ISSUELOOPER_TMP/s.log 2>&1 < /dev/null",
               "uv run concierge &> $ISSUELOOPER_TMP/s.log < /dev/null & sleep 2; curl -sf localhost:8121/",
               "pytest tests/a & pytest tests/b & wait",
               'echo "a & b" && git status 2>/dev/null | head',
               "cd wt && (uv run x > l 2>&1 < /dev/null &) && sleep 1"]
    for role in ("tester", "worker"):
        for cmd in refused:
            verdict, reason = run(cmd, role=role, host=AGY)
            check(f"{cmd!r} is refused for the {role}", verdict, "refuse")
        check("the refusal shows the right form", "2>&1 < /dev/null" in reason and "setsid -f" in reason, True)
        for cmd in allowed:
            check(f"{cmd!r} is allowed for the {role}", run(cmd, role=role, host=AGY)[0], "allow")
    check("off with GUARD_BACKGROUND=0",
          run(refused[0], role="tester", extra_env={"GUARD_BACKGROUND": "0"})[0], "allow")


def test_an_author_resolving_its_pr_stays_on_it() -> None:
    # ccpool's author resolve: the worker's conversation, resumed for its PR's conflict.
    env = {"RESOLVE_PR": "41", "RESOLVE_BRANCH": "issue-7"}
    for cmd, want in (("git push --force-with-lease origin HEAD:issue-7", "allow"),
                      ("git push --force-with-lease origin issue-7", "allow"),
                      ("git push --force-with-lease", "allow"),
                      ("git push origin HEAD:issue-8", "refuse"),
                      ("git push origin HEAD:main", "refuse"),
                      ("gh pr comment 41 --body '<!-- worker -->\nthe hunk needs a decision'", "allow"),
                      ("gh pr comment 40 --body '<!-- worker -->\nx'", "refuse"),
                      ("gh pr create --title x --body y", "refuse"),
                      ("gh issue edit 7 --add-label stage:triage --remove-label stage:pr", "refuse"),
                      ("gh pr merge 41 --squash", "refuse")):
        check(f"resolving PR #41: {cmd!r}", run(cmd, role="worker", host=AGY, extra_env=env)[0], want)
    check("an ordinary worker pushes its branch as before",
          run("git push origin HEAD:issue-8", role="worker", host=AGY)[0], "allow")


def test_sessions_do_not_read_the_loops_logs() -> None:
    # Run 12: UX run 3 copied the previous run's score from its report; run 4 grepped run 2's stream.
    env = {"LOGDIR": "/repo/.issuelooper/run/pool/0182-tester-ux_abc"}
    for cmd in ('find .issuelooper/run -name "*ux_report*"',
                'grep -m 5 journeys.py "$SHARED_REPO_PATH/.issuelooper/run/pool/0096-tester-ux_df2/session.jsonl"',
                'tail -n 30 "$ISSUELOOPER_TMP/guard.last"', 'ls -la "$ISSUELOOPER_TMP/guard-runs"'):
        check(f"a tester reading {cmd!r} is refused", run(cmd, role="tester", host=AGY, extra_env=env)[0], "refuse")
    for cmd in ("cat > $LOGDIR/ux_report.md <<EOF\nScore: provisional\nEOF",
                "head -3 /repo/.issuelooper/run/pool/0182-tester-ux_abc/ux_report.md",
                "uv run python ux/journeys.py --port 8021 --out $SHARED_REPO_PATH/.ux/abc"):
        check(f"its own $LOGDIR and the app are fine: {cmd!r}", run(cmd, role="tester", host=AGY, extra_env=env)[0],
              "allow")
    check("the monitor reads the logs as before", run("tail .issuelooper/run/pool/pool.jsonl", role="monitor")[0], "allow")
    check("off with GUARD_LOOP_LOGS=0",
          run("tail .issuelooper/run/pool/pool.jsonl", role="worker", extra_env={"GUARD_LOOP_LOGS": "0"})[0], "allow")


def test_arguments_are_refused_at_once() -> None:
    # `role_guard.py check-pr 98` used to wait on stdin until a watchdog killed it.
    proc = subprocess.Popen([sys.executable, str(GUARD), "check-pr", "98"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        err = "hung"
    check("an argument exits at once without reading stdin", (proc.returncode, "not a command" in err), (1, True))


# --------------------------------------------------------------------------- #
# What the guard already did. These are here because nothing asserted them.
# --------------------------------------------------------------------------- #


def test_reads_are_untouched() -> None:
    for cmd in (
        "gh issue view 269",
        "gh issue list --label bug",
        "gh api repos/o/r/issues/1",
        "git status",
    ):
        verdict, _ = run(cmd, role="monitor")
        check(f"read is allowed: {cmd}", verdict, "allow")


def test_no_role_cannot_write() -> None:
    verdict, reason = run("gh issue close 1", role="")
    check("a session with no role may not write", verdict, "refuse")
    check("and is told to ask rather than to choose", "Ask the owner" in reason, True)

    verdict, _ = run("gh issue view 1", role="")
    check("but may still read", verdict, "allow")


def test_comments_need_the_marker() -> None:
    verdict, _ = run("gh issue comment 1 --body 'looks fine'", role="monitor")
    check("an unsigned comment is refused", verdict, "refuse")

    verdict, _ = run("gh issue comment 1 --body '<!-- monitor --> looks fine'", role="monitor")
    check("a signed comment is allowed", verdict, "allow")

    verdict, _ = run("gh issue comment 1 --body '<!-- merger --> looks fine'", role="monitor")
    check("someone else's marker is refused", verdict, "refuse")

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write("<!-- monitor -->\nbody\n")
        path = fh.name
    verdict, _ = run(f"gh issue comment 1 --body-file {path}", role="monitor")
    check("the marker is found inside a --body-file", verdict, "allow")
    pathlib.Path(path).unlink()

    # A bare close carries no text, so it cannot carry a marker.
    verdict, _ = run("gh issue close 1 --reason 'not planned'", role="monitor")
    check("a bare close needs no marker", verdict, "allow")

    verdict, _ = run("gh issue close 1", role="monitor")
    check("bare close without comment is allowed for monitor", verdict, "allow")

    # Unreadable forms (stdin, heredoc, --body-file -) are refused and name --body-file
    verdict, reason = run("gh issue comment 1 --body-file -", role="tester")
    check("--body-file - is refused", verdict, "refuse")
    check("--body-file - refusal names --body-file", "--body-file" in reason, True)

    verdict, reason = run("echo 'hi' | gh issue comment 1", role="tester")
    check("stdin pipe comment is refused", verdict, "refuse")
    check("stdin pipe refusal names --body-file", "--body-file" in reason, True)

    verdict, reason = run("gh issue comment 1 <<EOF\n<!-- tester -->\nbody\nEOF", role="tester")
    check("heredoc comment is refused", verdict, "refuse")
    check("heredoc refusal names --body-file", "--body-file" in reason, True)

    # …but the dominant shape in this repo writes the heredoc to a file and then
    # names that file, in one Bash call. The body is on disk and readable, and a
    # bare `<<` test in cli_comment_status would refuse every session in the loop.
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write("<!-- monitor -->\nbody\n")
        path = fh.name
    verdict, _ = run(
        f"cat > {path} <<'MD'\n<!-- monitor -->\nbody\nMD\ngh issue comment 1 --body-file {path}",
        role="monitor",
    )
    check("heredoc written to a file, then --body-file, is allowed", verdict, "allow")
    pathlib.Path(path).unlink()


def test_the_aide_files_and_comments_but_does_not_move_the_queue() -> None:
    """The owner's ad-hoc sessions get a role rather than a fail-open gap."""
    verdict, _ = run("gh issue create --title t --body '<!-- aide --> found this'", role="aide")
    check("a signed aide filing is allowed", verdict, "allow")

    verdict, _ = run("gh issue create --title t --body 'found this'", role="aide")
    check("an unsigned aide filing is refused", verdict, "refuse")

    verdict, _ = run("gh issue comment 1 --body '<!-- monitor --> hi'", role="aide")
    check("the aide may not sign as the monitor", verdict, "refuse")

    # It says how bad the thing looks, and never re-ranks anyone else's issue.
    verdict, _ = run("gh issue edit 1 --add-label P1", role="aide")
    check("the aide may set a priority", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label P2", role="aide")
    check("the aide may not clear a priority", verdict, "refuse")

    # Nothing that moves the queue.
    verdict, _ = run("gh issue close 1 --reason 'not planned'", role="aide")
    check("the aide may not close", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label stage:ready", role="aide")
    check("the aide may not apply stage:ready", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label stage:pr", role="aide")
    check("the aide may not apply stage:pr", verdict, "refuse")

    verdict, _ = run("gh pr merge 7 --squash", role="aide")
    check("the aide may not merge", verdict, "refuse")

    # stage:triage is listed in LABEL_RULES, allowing aide to file and clear on manual triage.
    verdict, _ = run("gh issue edit 1 --add-label stage:triage", role="aide")
    check("the aide may file at stage:triage", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label stage:triage", role="aide")
    check("the aide may remove stage:triage", verdict, "allow")


def test_rest_comments_need_the_marker() -> None:
    # gh api repos/o/r/issues/296/comments -f body=... posts a comment too.
    for role in ("tester", "monitor", "merger"):
        verdict, _ = run("gh api repos/o/r/issues/296/comments -f body='no marker'", role=role)
        check(f"unsigned REST comment is refused for {role}", verdict, "refuse")

    verdict, _ = run("gh api repos/o/r/issues/296/comments -f body='<!-- tester --> valid'", role="tester")
    check("signed REST comment passes for tester", verdict, "allow")

    verdict, _ = run("gh api repos/o/r/issues/296/comments -f body='<!-- tester --> valid'", role="monitor")
    check("REST comment signed with another role is refused", verdict, "refuse")

    # Flag variations: --field, --raw-field, -F
    verdict, _ = run("gh api repos/o/r/issues/296/comments --field body='no marker'", role="tester")
    check("--field unsigned is refused", verdict, "refuse")

    verdict, _ = run("gh api repos/o/r/issues/296/comments --raw-field body='<!-- tester --> valid'", role="tester")
    check("--raw-field signed passes", verdict, "allow")

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write("<!-- tester -->\nbody from file\n")
        path = fh.name
    verdict, _ = run(f"gh api repos/o/r/issues/296/comments -F body=@{path}", role="tester")
    check("-F body=@path signed passes", verdict, "allow")
    pathlib.Path(path).unlink()

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"body": "<!-- tester -->\njson body\n"}, fh)
        path = fh.name
    verdict, _ = run(f"gh api repos/o/r/issues/296/comments --input {path}", role="tester")
    check("--input path signed passes", verdict, "allow")
    pathlib.Path(path).unlink()

    # Unreadable forms are refused with a message naming --body-file
    verdict, reason = run("gh api repos/o/r/issues/296/comments --input -", role="tester")
    check("--input - is refused", verdict, "refuse")
    check("--input - refusal names --body-file", "--body-file" in reason, True)

    verdict, reason = run("gh api repos/o/r/issues/296/comments -F body=@-", role="tester")
    check("-F body=@- is refused", verdict, "refuse")
    check("-F body=@- refusal names --body-file", "--body-file" in reason, True)

    # Read requests are allowed without marker
    verdict, _ = run("gh api repos/o/r/issues/296/comments", role="tester")
    check("GET comments needs no marker", verdict, "allow")


def test_label_rules() -> None:
    verdict, _ = run("gh issue edit 1 --add-label stage:ready", role="monitor")
    check("the monitor may not apply stage:ready", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label stage:ready", role="worker", extra_env=claim_env(1))
    check("the worker may, on its claim", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --add-label 'on hold'", role="worker", extra_env=claim_env(1))
    check("nobody may apply on hold", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label P0", role="merger")
    check("the merger may not re-prioritize", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --remove-label P1 --add-label P0", role="monitor")
    check("the monitor may", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label stage:ready", role="monitor")
    check("the monitor may remove stage:ready", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label stage:pr", role="monitor")
    check("the monitor may remove stage:pr", verdict, "allow")


def test_stage_transitions() -> None:
    # Multiple stage labels cannot be added at once
    verdict, _ = run("gh issue edit 1 --add-label stage:ready,stage:pr", role="worker", extra_env=claim_env(1))
    check("multiple stage labels at once is refused", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label stage:triage --add-label stage:ready", role="worker", extra_env=claim_env(1))
    check("multiple stage flags at once is refused", verdict, "refuse")

    # Advancing to stage:pr must clear prior stage labels (stage:triage, stage:ready, stage:owner)
    verdict, _ = run("gh issue edit 1 --add-label stage:pr", role="worker", extra_env=claim_env(1))
    check("adding stage:pr with no remove is refused", verdict, "refuse")

    # The triage -> pr shortcut that left stage:triage attached
    verdict, reason = run("gh issue edit 1 --add-label stage:pr --remove-label stage:ready,stage:doing", role="worker", extra_env=claim_env(1))
    check("adding stage:pr while only removing stage:ready is refused", verdict, "refuse")
    check("refusal mentions missing stage:triage", "stage:triage" in reason, True)

    verdict, _ = run("gh issue edit 1 --add-label stage:pr --remove-label stage:triage,stage:doing", role="worker", extra_env=claim_env(1))
    check("adding stage:pr while only removing stage:triage is refused", verdict, "refuse")

    verdict, reason = run("gh issue edit 1 --add-label stage:pr --remove-label stage:ready,stage:triage,stage:doing", role="worker", extra_env=claim_env(1))
    check("adding stage:pr without removing stage:owner is refused", verdict, "refuse")
    check("refusal mentions missing stage:owner", "stage:owner" in reason, True)

    # Fully clearing prior stages is allowed for the worker
    verdict, _ = run(
        "gh issue edit 1 --add-label stage:pr --remove-label stage:ready,stage:triage,stage:owner,stage:doing",
        role="worker", extra_env=claim_env(1),
    )
    check("adding stage:pr while removing all prior stages is allowed for the worker", verdict, "allow")

    verdict, _ = run(
        "gh issue edit 1 --add-label stage:pr --remove-label stage:doing,stage:triage,stage:ready,stage:owner",
        role="worker", extra_env=claim_env(1),
    )
    check("order of remove labels does not matter", verdict, "allow")

    verdict, _ = run(
        "gh issue edit 1 --add-label stage:pr --remove-label stage:ready --remove-label stage:triage --remove-label stage:owner --remove-label stage:doing",
        role="worker", extra_env=claim_env(1),
    )
    check("multiple --remove-label flags clearing prior stages is allowed", verdict, "allow")

    # Merger or monitor cannot add stage:pr even with proper removes
    verdict, _ = run(
        "gh issue edit 1 --add-label stage:pr --remove-label stage:ready,stage:triage,stage:owner",
        role="merger",
    )
    check("merger cannot add stage:pr", verdict, "refuse")


def test_host_shapes() -> None:
    """The first defect: what "allowed" looks like on the wire, per host.

    Claude Code reads a `decision`-less object as "no opinion, proceed". agy reads
    the same object as a refusal, with no reason attached — which is how the
    merger came to be blocked on `echo "$CC_ROLE"` with a blank message.
    """
    payload = {"tool_input": {"command": "git status"}, "session_id": "s"}
    out = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "CC_ROLE": "monitor"},
        check=True,
    ).stdout
    check("Claude Code gets a bare {} for an allow", json.loads(out), {})

    payload = {
        "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
        "conversationId": "c",
    }
    out = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "CC_ROLE": "merger"},
        check=True,
    ).stdout
    check("agy gets an explicit allow", json.loads(out).get("decision"), "allow")

    verdict, _ = run("gh issue edit 1 --add-label 'on hold'", role="merger", host=AGY)
    check("and an explicit deny", verdict, "refuse")


# --------------------------------------------------------------------------- #
# The fail-open that failed closed
#
# `__main__` catches everything main() throws and lets the command run, because a
# guard that bricks the loop over an unusual payload costs more than the gap it
# closes. That only works if the handler answers in the host's own vocabulary,
# and it could not: the host was read out of the payload, so a payload that would
# not parse left it at Claude Code's default. On agy the handler then printed no
# `decision` at all, which agy reads as a refusal — the fail-open inverted into a
# fail-closed on exactly the input it exists to survive.
# --------------------------------------------------------------------------- #


def raw_full(payload: str, role: str = "merger") -> tuple[dict, str]:
    """Drive the guard with bytes rather than a dict, so the parse can fail.

    Returns stderr as well, because on agy that is where the guard has to put
    anything it wants to say — see the case below that pins it.
    """
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "CC_ROLE": role},
        check=True,
    )
    return json.loads(proc.stdout or "{}"), proc.stderr


def raw(payload: str, role: str = "merger") -> dict:
    return raw_full(payload, role)[0]


def test_unparsable_payload_fails_open_on_both_hosts() -> None:
    # Truncated mid-object: valid enough to say who is asking, not to parse.
    agy = '{"conversationId": "c", "toolCall": {"args": {"CommandLine": "git st'
    out, err = raw_full(agy)
    check("agy: a broken payload still gets an explicit allow",
          out.get("decision"), "allow")
    check("agy: and is told why, on stderr", "role_guard error" in err, True)

    cc = '{"session_id": "s", "tool_input": {"command": "git st'
    out = raw(cc)
    check("Claude Code: a broken payload carries no decision",
          "decision" in out, False)
    check("Claude Code: and is told why",
          "role_guard error" in out.get("systemMessage", ""), True)


def test_the_agy_error_object_declares_only_what_agy_declares() -> None:
    """agy's `PreToolHookResult` has decision, reason, overwrite,
    permission_overrides, allow_tool and deny_reason — and no `systemMessage`.
    The binary carries `DisallowUnknownFields`, so writing a field agy has no
    contract for risks the object being rejected *inside the error handler*,
    which is the one place a fail-open must not fail."""
    out, err = raw_full('{"conversationId": "c", "toolCall": {"args": {"Comm')
    allowed = {"decision", "reason", "overwrite", "permission_overrides",
               "permissionOverrides", "allow_tool", "deny_reason"}
    check("agy: the error object declares no field agy does not have",
          sorted(set(out) - allowed), [])
    check("agy: the note is not lost, it is on stderr",
          "role_guard error" in err, True)


def test_an_unknown_envelope_from_recognized_host_is_refused() -> None:
    """When the host is recognized (matches AGY_PAYLOAD or CC_PAYLOAD) but the
    envelope is unknown or unparseable, allowing it is a fail-open defect that
    lets commands run completely unguarded (merges went through unguarded under
    agy this way). The guard must fail closed with a refusal."""
    out = raw('{"conversationId": "c", "someNewCall": {"args": {"CommandLine": "ls"}}}')
    check("agy: an unknown envelope from a recognized host is refused",
          out.get("decision"), "deny")

    # The same shape on Claude Code must also fail closed with block.
    out = raw('{"session_id": "s", "some_new_input": {"command": "ls"}}')
    check("Claude Code: an unknown envelope from a recognized host is refused",
          out.get("decision"), "block")


def test_host_is_read_before_the_parse() -> None:
    """Not JSON at all, so nothing downstream of json.loads can classify it."""
    out = raw("this is not json, and never was")
    check("garbage from an unidentifiable host falls back to Claude Code's shape",
          "decision" in out, False)

    # The tie-break is deliberate rather than incidental: Claude Code's shape
    # means "no opinion, ask the owner as usual", agy's explicit allow skips the
    # prompt. An unrecognized payload gets the one that still asks.
    out = raw('{"session_id": "s", "conversationId": "c", "tool_input": {"command')
    check("a payload claiming both hosts is treated as Claude Code",
          "decision" in out, False)


def test_the_word_toolcall_inside_a_command_is_not_the_host() -> None:
    """A Claude Code payload whose *command* mentions agy's keys is still Claude
    Code. JSON escapes the quotes around a string inside a string, which is what
    makes the quoted-key match safe — and this is not hypothetical, since the
    command that finds these keys is a grep for them."""
    out = raw(json.dumps({
        "session_id": "s",
        "tool_input": {"command": 'grep -rn \'"toolCall":\' .claude/hooks/'},
    }))
    check("the grep is allowed", out, {})

    # Now the same command in a payload truncated past the point of parsing.
    body = json.dumps({"tool_input": {"command": 'grep -rn \'"toolCall":\' x'}})
    out = raw(body[:-8])
    check("and a truncated one does not become an agy auto-approve",
          "decision" in out, False)


def test_unparseable_command_payload_from_recognized_host_is_refused() -> None:
    """A recognized host (AGY or CC) sending a command hook payload that
    cannot be parsed into a command must be refused (fail-closed), not allowed."""
    out_agy = raw('{"conversationId": "c", "toolCall": {"name": "run_command", "args": {}}}')
    check("agy: unparseable command payload is refused with deny",
          out_agy.get("decision"), "deny")

    out_cc = raw('{"session_id": "s", "tool_input": {}}')
    check("Claude Code: unparseable command payload is refused with block",
          out_cc.get("decision"), "block")


def test_heartbeat_and_check_alive() -> None:
    """Heartbeat recording, per-session tracking, test isolation and --check-alive."""
    import time
    from role_guard import HEARTBEAT_FILE, RUNS_DIR

    # 1. Heartbeat and session file creation in isolated test environment
    check("heartbeat file uses isolated directory", str(TEST_HEARTBEAT), str(HEARTBEAT_FILE))
    check("runs dir uses isolated directory", str(TEST_RUNS), str(RUNS_DIR))

    sess = "test-heartbeat-sess"
    out = raw('{"session_id": "%s", "tool_input": {"command": "git status"}}' % sess, role="merger")
    check("role_guard runs cleanly", "decision" in out, False)
    check("heartbeat file exists in test dir", HEARTBEAT_FILE.is_file(), True)
    sess_file = RUNS_DIR / sess
    check("session record exists", sess_file.is_file(), True)
    if sess_file.is_file():
        content = sess_file.read_text(encoding="utf-8")
        check("session record contains role", "merger" in content, True)
        check("session record contains command", "git status" in content, True)

    # 2. --check-alive with valid active session ID returns 0
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--check-alive", sess],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    check("--check-alive <valid_session> returns 0", proc.returncode, 0)
    check("--check-alive <valid_session> reports active", f"role_guard is active for session {sess}" in proc.stdout, True)

    # 3. --check-alive with nonexistent session ID returns 1
    proc_nonexistent = subprocess.run(
        [sys.executable, str(GUARD), "--check-alive", "nonexistent-session-12345"],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    check("--check-alive <nonexistent_session> returns 1", proc_nonexistent.returncode, 1)
    check("--check-alive <nonexistent_session> reports not found", "heartbeat not found for session nonexistent-session-12345" in proc_nonexistent.stderr, True)

    # 4. --check-alive with stale session returns 1
    stale_sess = "stale-session-test"
    stale_file = RUNS_DIR / stale_sess
    stale_file.write_text("old\n", encoding="utf-8")
    past_time = time.time() - 3600
    os.utime(stale_file, (past_time, past_time))
    proc_stale = subprocess.run(
        [sys.executable, str(GUARD), "--check-alive", stale_sess],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    check("--check-alive <stale_session> returns 1", proc_stale.returncode, 1)
    check("--check-alive <stale_session> reports stale", "is stale" in proc_stale.stderr, True)

    # 5. --check-alive without session ID when active session exists returns 0
    proc_noarg = subprocess.run(
        [sys.executable, str(GUARD), "--check-alive"],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    check("--check-alive (no args) with active sessions returns 0", proc_noarg.returncode, 0)
    check("--check-alive (no args) reports active", "role_guard is active" in proc_noarg.stdout, True)

    # 6. --check-alive without session ID when RUNS_DIR is empty returns 1
    with tempfile.TemporaryDirectory(prefix="empty_runs_") as empty_dir:
        env_empty = {**os.environ, "ROLE_GUARD_RUNS_DIR": empty_dir}
        proc_empty = subprocess.run(
            [sys.executable, str(GUARD), "--check-alive"],
            capture_output=True,
            text=True,
            env=env_empty,
        )
        check("--check-alive empty runs dir returns 1", proc_empty.returncode, 1)
        check("--check-alive empty runs dir reports empty", "runs directory is empty" in proc_empty.stderr, True)

    # 7. --check-alive without session ID when all sessions are stale returns 1
    with tempfile.TemporaryDirectory(prefix="stale_runs_") as stale_dir:
        stale_p = pathlib.Path(stale_dir) / "old-session"
        stale_p.write_text("old\n", encoding="utf-8")
        os.utime(stale_p, (past_time, past_time))
        env_stale = {**os.environ, "ROLE_GUARD_RUNS_DIR": stale_dir}
        proc_all_stale = subprocess.run(
            [sys.executable, str(GUARD), "--check-alive"],
            capture_output=True,
            text=True,
            env=env_stale,
        )
        check("--check-alive all stale returns 1", proc_all_stale.returncode, 1)
        check("--check-alive all stale reports stale across all", "stale across all" in proc_all_stale.stderr, True)

    # Clean up stale session from TEST_RUNS so subsequent tests don't see it
    try:
        stale_file.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# `hold:monitor`
#
# A hold written as a comment ("hold this patch") is invisible to the merger,
# which polls labels, so the patch it names can merge minutes later with nobody
# doing anything wrong. The hold is a label the guard enforces.
#
# These cases stub `gh` on PATH rather than the network: the guard shells out to
# `gh issue view --json labels`, and a test that reached GitHub would be slow,
# flaky, and would need a real held issue to exist.
# --------------------------------------------------------------------------- #

FAKE_GH = '''#!/usr/bin/env python3
import json, os, pathlib, sys

args = sys.argv[1:]
state = json.loads(pathlib.Path(os.environ["FAKE_GH_STATE"]).read_text())
pathlib.Path(os.environ["FAKE_GH_CALLED"]).write_text("yes")

if state.get("broken"):
    sys.exit(1)
if state.get("sleep"):
    import time
    time.sleep(state["sleep"])
if args[:2] == ["issue", "view"]:
    names = state.get("labels", {}).get(args[2], [])
    bodies = state.get("comments", {}).get(args[2], [])
    print(json.dumps({
        "labels": [{"name": n} for n in names],
        "body": state.get("bodies", {}).get(args[2], ""),
        "comments": [{"body": b, "authorAssociation": "NONE" if b.startswith("[stranger]") else "OWNER"}
                     for b in bodies],
    }))
elif args[:2] == ["pr", "view"]:
    key = args[2] if len(args) > 2 and args[2].isdigit() else ""
    refs = state.get("linked", {}).get(key, [])
    pr_state = state.get("prs", {}).get(key)
    if pr_state is not None:
        head = pr_state.get("headRefOid", "0123456789abcdef0123456789abcdef01234567")
        body = pr_state.get("body", f"<!-- worker -->\\nCloses #{key}\\n- **Sweep SHA:** {head}\\n- **Sweep:** waived - owner ruling\\n- **Fail-before:** waived - owner ruling\\n")
        files = pr_state.get("files", [])
    else:
        head = "0123456789abcdef0123456789abcdef01234567"
        body = f"<!-- worker -->\\nCloses #{key}\\n- **Sweep SHA:** {head}\\n- **Sweep:** waived - owner ruling\\n- **Fail-before:** waived - owner ruling\\n"
        files = []
    print(json.dumps({
        "closingIssuesReferences": [{"number": int(n)} for n in refs],
        "headRefOid": head,
        "baseRefOid": (pr_state or {}).get("baseRefOid", ""),
        "headRefName": (pr_state or {}).get("headRefName", ""),
        "body": body,
        "files": files,
    }))
else:
    sys.exit(1)
'''


def make_clean_repo() -> pathlib.Path:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="role-guard-repo-"))
    env = dict(os.environ)
    subprocess.check_call(["git", "init", "-b", "main", str(tmp)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(["git", "-C", str(tmp), "config", "user.name", "Tester"], env=env)
    subprocess.check_call(["git", "-C", str(tmp), "config", "user.email", "test@example.com"], env=env)
    subprocess.check_call(["git", "-C", str(tmp), "commit", "--allow-empty", "-m", "init"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    head = subprocess.check_output(["git", "-C", str(tmp), "rev-parse", "HEAD"], env=env).decode().strip()
    subprocess.check_call(["git", "-C", str(tmp), "update-ref", "refs/remotes/origin/main", head], env=env)
    return tmp


def with_fake_gh(
    labels: dict | None = None,
    linked: dict | None = None,
    broken: bool = False,
    prs: dict | None = None,
    comments: dict | None = None,
    sleep: float = 0,
    bodies: dict | None = None,
):
    """A PATH with a stub `gh` on it, plus the path of its call marker."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="role-guard-gh-"))
    (tmp / "gh").write_text(FAKE_GH)
    (tmp / "gh").chmod(0o755)
    state = tmp / "state.json"
    state.write_text(json.dumps(
        {
            "labels": labels or {},
            "linked": linked or {},
            "broken": broken,
            "sleep": sleep,
            "prs": prs or {},
            "comments": comments or {},
            "bodies": bodies or {},
        }
    ))
    called = tmp / "called"
    repo = make_clean_repo()
    return {
        "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
        "FAKE_GH_STATE": str(state),
        "FAKE_GH_CALLED": str(called),
        "SHARED_REPO_PATH": str(repo),
    }, called


def make_commit(files: dict[str, str], parent: str = "") -> str:
    """Create a git commit object with the specified files and return its SHA.

    `parent` gives the commit an ancestor, which the case-count gate needs: it
    compares against `git merge-base`, and two parentless commits have none.
    """
    env = dict(os.environ)
    fd, path = tempfile.mkstemp(prefix="git-index-")
    os.close(fd)
    os.unlink(path)
    try:
        env["GIT_INDEX_FILE"] = path
        subprocess.check_call(["git", "read-tree", "--empty"], env=env)
        for p, content in files.items():
            blob = subprocess.check_output(
                ["git", "hash-object", "-w", "--stdin"],
                input=content.encode(),
                env=env,
            ).decode().strip()
            subprocess.check_call(
                ["git", "update-index", "--add", "--cacheinfo", "100644", blob, p],
                env=env,
            )
        tree = subprocess.check_output(["git", "write-tree"], env=env).decode().strip()
        # A unique message, because commit identity is (tree, parent, message,
        # author, second). Two calls with the same tree and parent inside one
        # second collide, and the fork-point case then measures a commit against
        # itself and reports no growth - a flake, roughly one run in three.
        argv = ["git", "commit-tree", tree, "-m", f"test commit {uuid.uuid4()}"]
        if parent:
            argv += ["-p", parent]
        commit = subprocess.check_output(argv, env=env).decode().strip()
        return commit
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_hold_monitor_stops_the_close_and_the_merge() -> None:
    env, _ = with_fake_gh(labels={"46": ["P0", "hold:monitor"], "47": ["P2"]},
                          linked={"84": ["46"], "83": ["47"]})

    verdict, reason = run("gh issue close 46 --reason completed",
                          role="monitor", extra_env=env)
    check("a held issue does not close", verdict, "refuse")
    check("and the refusal names the issue", "#46" in reason, True)

    verdict, _ = run("gh pr merge 84 --squash", role="merger", extra_env=env)
    check("a PR whose linked issue is held does not merge", verdict, "refuse")

    verdict, _ = run("gh issue close 47", role="monitor", extra_env=env)
    check("an unheld issue still closes", verdict, "allow")

    verdict, _ = run("gh pr merge 83 --squash", role="merger", extra_env=env)
    check("and its PR still merges", verdict, "allow")

    # The REST spellings of both, which the label rules already had to learn once
    # and which would otherwise be the way around this.
    verdict, _ = run(
        "gh api -X PATCH repos/o/r/issues/46 -f state=closed",
        role="monitor", extra_env=env)
    check("the REST close is held too", verdict, "refuse")

    verdict, _ = run("gh api -X PUT repos/o/r/pulls/84/merge",
                     role="merger", extra_env=env)
    check("the REST merge is held too", verdict, "refuse")


def test_lifting_the_hold_in_the_same_command_is_the_way_through() -> None:
    env, _ = with_fake_gh(labels={"46": ["hold:monitor"]})
    verdict, _ = run(
        "gh issue edit 46 --remove-label hold:monitor && gh issue close 46",
        role="monitor", extra_env=env)
    check("removing the hold and closing in one command is allowed",
          verdict, "allow")

    # Nobody else can do that, because nobody else can clear the label.
    verdict, _ = run(
        "gh issue edit 46 --remove-label hold:monitor && gh issue close 46",
        role="merger", extra_env=env)
    check("the merger cannot lift it on its way past", verdict, "refuse")


def test_only_the_monitor_sets_the_hold() -> None:
    for role in ("merger", "tester", "aide"):
        verdict, _ = run(f"gh issue edit 46 --add-label {'hold:monitor'}", role=role)
        check(f"the {role} cannot apply hold:monitor", verdict, "refuse")
        verdict, _ = run("gh issue edit 46 --remove-label hold:monitor", role=role)
        check(f"the {role} cannot clear hold:monitor", verdict, "refuse")

    verdict, _ = run("gh issue edit 46 --add-label hold:monitor", role="monitor")
    check("the monitor applies it", verdict, "allow")
    verdict, _ = run("gh issue edit 46 --remove-label hold:monitor", role="monitor")
    check("and clears it", verdict, "allow")


def test_the_hold_lookup_fails_open_and_is_not_run_on_other_writes() -> None:
    """Two properties, and both are load-bearing.

    Fail-open, because this is the guard's only network call and a loop that
    stops when GitHub is slow costs more than the rule saves. And *not run*
    on anything but a close or a merge, because every Bash call goes through
    here — a query on each one would be felt immediately.
    """
    env, called = with_fake_gh(labels={"46": ["hold:monitor"]}, broken=True)
    verdict, _ = run("gh issue close 46", role="monitor", extra_env=env)
    check("a gh that cannot answer allows the close", verdict, "allow")

    env, called = with_fake_gh(labels={"46": ["hold:monitor"]})
    run("gh issue comment 46 --body '<!-- monitor --> hi'",
        role="monitor", extra_env=env)
    check("commenting on a held issue queries nothing", called.exists(), False)
    check("and is allowed", run("gh issue comment 46 --body '<!-- monitor --> hi'",
                                role="monitor", extra_env=env)[0], "allow")


def test_pr_merge_fails_closed_when_github_cannot_be_read() -> None:
    # The gates were skipped (`continue`) when `gh pr view` failed, and the merge went on.
    env, _ = with_fake_gh(broken=True)
    verdict, reason = run("gh pr merge 100 --squash", role="merger", extra_env=env)
    check("gh fails: the merge is refused", (verdict, "could not be read from GitHub" in reason), ("refuse", True))
    env, _ = with_fake_gh(sleep=10)
    verdict, reason = run("gh pr merge 100 --squash", role="merger", extra_env=env)
    check("gh times out: the merge is refused", (verdict, "could not be read from GitHub" in reason), ("refuse", True))
    env, _ = with_fake_gh(prs={"100": {"headRefOid": "", "body": "x", "files": []}})
    verdict, _ = run("gh pr merge 100 --squash", role="merger", extra_env=env)
    check("an answer without a head: refused", verdict, "refuse")
    env, _ = with_fake_gh(prs={"100": {
        "headRefOid": "9" * 40, "body": f"<!-- worker -->\n- **Sweep SHA:** {'9' * 40}\n",
        "files": [{"path": "src/gone.py", "changeType": "MODIFIED"}]}})
    verdict, reason = run("gh pr merge 100 --squash", role="merger", extra_env=env)
    check("a changed .py file that cannot be read at the head: refused (Gate 1 cannot check it)",
          (verdict, "could not be read at PR #100 head" in reason), ("refuse", True))
    env, _ = with_fake_gh(broken=True)
    verdict, _ = run("gh issue comment 5 --body '<!-- monitor --> ok'", role="monitor", extra_env=env)
    check("everything else stays fail-open", verdict, "allow")
    proc = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(
        {"tool_input": {"command": "ls"}, "session_id": "s"}), capture_output=True, text=True,
        env=dict(os.environ, CC_ROLE="merger", GUARD_EXPLICIT_ALLOW="1"))
    check("GUARD_EXPLICIT_ALLOW=1: an allow says so", json.loads(proc.stdout), {"decision": "allow"})


def test_pr_merge_requires_sweep_sha() -> None:
    env, _ = with_fake_gh(
        prs={"100": {
            "headRefOid": "1111111222222233333333444444445555555566",
            "body": "<!-- worker -->\nCloses #10\nNo sweep SHA here\n",
            "files": [],
        }}
    )
    verdict, reason = run("gh pr merge 100 --squash", role="merger", extra_env=env)
    check("PR without sweep SHA is refused", verdict, "refuse")
    check("refusal mentions missing Sweep SHA", "missing `Sweep SHA`" in reason, True)


def test_pr_merge_rejects_stale_sweep_sha() -> None:
    env, _ = with_fake_gh(
        prs={"101": {
            "headRefOid": "2222222333333344444444555555556666666677",
            "body": "<!-- worker -->\nCloses #11\n- **Sweep SHA:** 1111111\n",
            "files": [],
        }}
    )
    verdict, reason = run("gh pr merge 101 --squash", role="merger", extra_env=env)
    check("PR with stale sweep SHA is refused", verdict, "refuse")
    check("refusal mentions does not match", "does not match Sweep SHA" in reason, True)
    check(
        "and prints both SHAs in full",
        "2222222333333344444444555555556666666677" in reason,
        True,
    )

    verdict, reason = run(
        "gh api -X PUT repos/o/r/pulls/101/merge",
        role="merger",
        extra_env=env,
    )
    check("REST merge with stale sweep SHA is refused", verdict, "refuse")


def test_pr_merge_allows_matching_sweep_sha() -> None:
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n",
                          "tests/unit/test_clean.py": "def test_foo():\n    assert True\n",
                          "tests/unit/test_other.py": "def test_other():\n    assert True\n"})
    env, _ = with_fake_gh(
        prs={"102": {
            "headRefOid": commit,
            "body": (f"<!-- worker -->\nCloses #12\n- **Sweep SHA:** {commit[:7]}\n"
                     "- **Fail-before:** tests/unit/test_clean.py 41 -> 42\n"),
            "files": [
                {"path": "clean.py", "changeType": "MODIFIED"},
                {"path": "tests/unit/test_clean.py", "changeType": "MODIFIED", "additions": 6},
            ],
        }}
    )
    verdict, _ = run("gh pr merge 102 --squash", role="merger", extra_env=env)
    check("PR with matching sweep SHA and valid python is allowed", verdict, "allow")


def test_pr_merge_detects_conflict_markers() -> None:
    for marker in ("<<<<<<< HEAD", "=======", ">>>>>>> issue-560"):
        commit = make_commit({
            "src/deploy.py": f"# header\n{marker}\ndef handler():\n    pass\n"
        })
        env, _ = with_fake_gh(
            prs={"103": {
                "headRefOid": commit,
                "body": f"<!-- worker -->\nCloses #13\n- **Sweep SHA:** {commit}\n",
                "files": [{"path": "src/deploy.py", "changeType": "MODIFIED"}],
            }}
        )
        verdict, reason = run("gh pr merge 103 --squash", role="merger", extra_env=env)
        check(f"PR with conflict marker {marker[:7]} is refused", verdict, "refuse")
        check("refusal mentions conflict marker", "Conflict marker" in reason, True)


def test_pr_merge_detects_syntax_errors() -> None:
    commit = make_commit({
        "src/deploy.py": "def invalid_syntax(\n"
    })
    env, _ = with_fake_gh(
        prs={"104": {
            "headRefOid": commit,
            "body": f"<!-- worker -->\nCloses #14\n- **Sweep SHA:** {commit}\n",
            "files": [{"path": "src/deploy.py", "changeType": "MODIFIED"}],
        }}
    )
    verdict, reason = run("gh pr merge 104 --squash", role="merger", extra_env=env)
    check("PR with syntax error is refused", verdict, "refuse")
    check("refusal mentions syntax error", "Syntax error" in reason, True)


def test_pr_merge_refuses_when_shared_checkout_dirty() -> None:
    for scoped in ("src", "tools", ".claude", ".agents"):
        repo = make_clean_repo()
        target_dir = repo / scoped
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "dirty_file.txt").write_text("unreviewed write\n")
        commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n",
                          "tests/unit/test_clean.py": "def test_foo():\n    assert True\n",
                          "tests/unit/test_other.py": "def test_other():\n    assert True\n"})
        env, _ = with_fake_gh(
            prs={"201": {
                "headRefOid": commit,
                "body": (f"<!-- worker -->\nCloses #21\n- **Sweep SHA:** {commit[:7]}\n"
                         "- **Fail-before:** waived - docs only\n"),
                "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
            }}
        )
        env["SHARED_REPO_PATH"] = str(repo)
        verdict, reason = run("gh pr merge 201 --squash", role="merger", extra_env=env)
        check(f"PR merge when {scoped} is dirty is refused", verdict, "refuse")
        check(f"refusal for dirty {scoped} mentions dirty checkout", "is dirty" in reason, True)
        check(f"refusal for dirty {scoped} mentions unreviewed writes", "Unreviewed writes" in reason, True)


def test_pr_merge_refuses_any_untracked_file() -> None:
    repo = make_clean_repo()
    unrelated = repo / "other-project"
    unrelated.mkdir(parents=True, exist_ok=True)
    (unrelated / "notes.txt").write_text("scratch in another project\n")
    (repo / "scratch.md").write_text("random note\n")
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n",
                          "tests/unit/test_clean.py": "def test_foo():\n    assert True\n",
                          "tests/unit/test_other.py": "def test_other():\n    assert True\n"})
    env, _ = with_fake_gh(
        prs={"204": {
            "headRefOid": commit,
            "body": (f"<!-- worker -->\nCloses #24\n- **Sweep SHA:** {commit[:7]}\n"
                     "- **Fail-before:** waived - docs only\n"),
            "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
        }}
    )
    env["SHARED_REPO_PATH"] = str(repo)
    verdict, _ = run("gh pr merge 204 --squash", role="merger", extra_env=env)
    check("PR merge with any untracked project file is refused", verdict, "refuse")


def test_pr_merge_refuses_when_local_main_ahead_of_origin_main() -> None:
    repo = make_clean_repo()
    (repo / "stray.txt").write_text("stray write\n")
    git_env = dict(os.environ)
    subprocess.check_call(["git", "-C", str(repo), "add", "stray.txt"], env=git_env)
    subprocess.check_call(["git", "-C", str(repo), "commit", "-m", "stray commit directly on main"], env=git_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n",
                          "tests/unit/test_clean.py": "def test_foo():\n    assert True\n",
                          "tests/unit/test_other.py": "def test_other():\n    assert True\n"})
    env, _ = with_fake_gh(
        prs={"202": {
            "headRefOid": commit,
            "body": f"<!-- worker -->\nCloses #22\n- **Sweep SHA:** {commit[:7]}\n",
            "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
        }}
    )
    env["SHARED_REPO_PATH"] = str(repo)
    verdict, reason = run("gh pr merge 202 --squash", role="merger", extra_env=env)
    check("PR merge when local main has stray commits is refused", verdict, "refuse")
    check("refusal mentions stray commits", "stray commits" in reason, True)
    check("refusal mentions ahead of origin/main", "ahead of `origin/main`" in reason, True)


def test_pr_merge_allows_when_shared_checkout_clean() -> None:
    repo = make_clean_repo()
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n",
                          "tests/unit/test_clean.py": "def test_foo():\n    assert True\n",
                          "tests/unit/test_other.py": "def test_other():\n    assert True\n"})
    env, _ = with_fake_gh(
        prs={"203": {
            "headRefOid": commit,
            "body": (f"<!-- worker -->\nCloses #23\n- **Sweep SHA:** {commit[:7]}\n"
                     "- **Fail-before:** waived - docs only\n"),
            "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
        }}
    )
    env["SHARED_REPO_PATH"] = str(repo)
    verdict, _ = run("gh pr merge 203 --squash", role="merger", extra_env=env)
    check("PR merge when shared checkout is clean is allowed", verdict, "allow")


def test_pr_merge_requires_fail_before() -> None:
    """Gate 3. The rewritten-expectation shape is the third case here: counts
    equal, expected value rewritten, suite green."""
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n",
                          "tests/unit/test_clean.py": "def test_foo():\n    assert True\n",
                          "tests/unit/test_other.py": "def test_other():\n    assert True\n"})
    base = f"<!-- worker -->\nCloses #{{n}}\n- **Sweep SHA:** {commit[:7]}\n"
    code_only = [{"path": "clean.py", "changeType": "MODIFIED", "additions": 4}]
    with_case = code_only + [
        {"path": "tests/unit/test_clean.py", "changeType": "MODIFIED", "additions": 6}
    ]

    def verdict_for(body: str, files: list, key: str = "301") -> tuple[str, str]:
        env, _ = with_fake_gh(
            prs={key: {"headRefOid": commit, "body": body, "files": files}}
        )
        return run(f"gh pr merge {key} --squash", role="merger", extra_env=env)

    verdict, reason = verdict_for(base.format(n=301), with_case)
    check("PR with no Fail-before line is refused", verdict, "refuse")
    check("refusal names Fail-before", "missing `Fail-before`" in reason, True)

    verdict, reason = verdict_for(
        base.format(n=302) + "- **Fail-before:** tests/unit/test_clean.py 368 -> 368\n",
        with_case, "302")
    check("equal counts are refused", verdict, "refuse")
    check("refusal says the fix is pinned by nothing",
          "pinned by nothing" in reason, True)

    verdict, reason = verdict_for(
        base.format(n=303) + "- **Fail-before:** tests/unit/test_clean.py 367 -> 368\n",
        code_only, "303")
    check("a rising count with no added check line is refused", verdict, "refuse")
    check("refusal names the rewritten expectation",
          "expected value was rewritten" in reason, True)

    verdict, _ = verdict_for(
        base.format(n=304) + "- **Fail-before:** tests/unit/test_clean.py 367 -> 368\n",
        with_case, "304")
    check("a real fail-before with an added case is allowed", verdict, "allow")

    for reason_text in ("needs: tester repro", "loop/process", "docs only",
                        "owner ruling"):
        verdict, _ = verdict_for(
            base.format(n=305) + f"- **Fail-before:** waived - {reason_text}\n",
            code_only, "305")
        check(f"waiver '{reason_text}' is allowed", verdict, "allow")

    verdict, _ = verdict_for(
        base.format(n=306) + "- **Fail-before:** waived - I could not be bothered\n",
        code_only, "306")
    check("an unrecognized waiver reason is refused", verdict, "refuse")

    # The line must name a test file under tests/, and that file, not some
    # other one, must be the one that grew.
    verdict, reason = verdict_for(
        base.format(n=307) + "- **Fail-before:** tools/clean_check.py 367 -> 368\n",
        with_case, "307")
    check("a Fail-before naming a non-pytest path is refused", verdict, "refuse")
    check("refusal says it is not a pytest file", "not a pytest file" in reason, True)

    other_file = code_only + [
        {"path": "tests/unit/test_other.py", "changeType": "MODIFIED", "additions": 6}
    ]
    verdict, reason = verdict_for(
        base.format(n=308) + "- **Fail-before:** tests/unit/test_clean.py 367 -> 368\n",
        other_file, "308")
    check("growth in a different test file does not satisfy the named one", verdict, "refuse")


def test_pr_merge_counts_cases_not_lines() -> None:
    """Lines change in the test file, cases do not, because an existing expected
    value was inverted to match the new behavior."""
    src = "def where() -> str:\n    return 'x'\n"
    two_cases = (
        "def test_a():\n"
        "    assert where() == 'a'\n"
        "def test_b():\n"
        "    assert where() == 'b'\n"
    )
    rewritten = (
        "def test_a_renamed_and_its_expectation_inverted():\n"
        "    assert where() == 'x'\n"
        "def test_b():\n"
        "    assert where() == 'b'\n"
    )
    three_cases = two_cases + "async def test_c():\n    assert where() == 'c'\n"

    base = make_commit({"src.py": src, "tests/unit/test_sv.py": two_cases})
    body = ("<!-- worker -->\nCloses #41\n- **Sweep SHA:** {sha}\n"
            "- **Sweep:** waived - loop/process\n"
            "- **Fail-before:** tests/unit/test_sv.py 1 -> 2\n")
    files = [
        {"path": "src.py", "changeType": "MODIFIED", "additions": 2},
        {"path": "tests/unit/test_sv.py", "changeType": "MODIFIED", "additions": 1},
    ]

    def verdict_for(head: str, key: str) -> tuple[str, str]:
        env, _ = with_fake_gh(prs={key: {
            "headRefOid": head, "baseRefOid": base,
            "body": body.format(sha=head[:7]), "files": files,
        }})
        return run(f"gh pr merge {key} --squash", role="merger", extra_env=env)

    head_rewritten = make_commit(
        {"src.py": src, "tests/unit/test_sv.py": rewritten}, parent=base)
    verdict, reason = verdict_for(head_rewritten, "401")
    check("a rewritten expectation with no new case is refused", verdict, "refuse")
    check("refusal reports the case counts", "2 -> 2" in reason, True)

    head_added = make_commit(
        {"src.py": src, "tests/unit/test_sv.py": three_cases}, parent=base)
    verdict, _ = verdict_for(head_added, "402")
    check("an added case is allowed", verdict, "allow")

    # A branch that forked before the base grew must not be read as a deletion.
    # Against the base tip this is 3 -> 2; against the fork point it is 2 -> 2.
    moved_base = make_commit(
        {"src.py": src, "tests/unit/test_sv.py": three_cases}, parent=base)
    env, _ = with_fake_gh(prs={"403": {
        "headRefOid": head_added, "baseRefOid": moved_base,
        "body": body.format(sha=head_added[:7]), "files": files,
    }})
    verdict, _ = run("gh pr merge 403 --squash", role="merger", extra_env=env)
    check("a branch forked before the base moved is measured at the fork",
          verdict, "allow")

    # Say nothing when the objects are not here, as the sweep gate does: an
    # unfetched branch must not become a refusal nobody can act on.
    env, _ = with_fake_gh(prs={"404": {
        "headRefOid": head_rewritten, "baseRefOid": "",
        "body": body.format(sha=head_rewritten[:7]), "files": files,
    }})
    verdict, _ = run("gh pr merge 404 --squash", role="merger", extra_env=env)
    check("an unresolvable fork point does not refuse", verdict, "allow")

    # A new test file is absent at the fork point and starts from zero cases.
    new_base = make_commit({"src.py": src})
    new_head = make_commit({"src.py": src, "tests/unit/test_new.py": two_cases},
                           parent=new_base)
    env, _ = with_fake_gh(prs={"405": {
        "headRefOid": new_head, "baseRefOid": new_base,
        "body": body.format(sha=new_head[:7]).replace("test_sv.py", "test_new.py"),
        "files": [{"path": "tests/unit/test_new.py", "changeType": "ADDED", "additions": 4}],
    }})
    verdict, _ = run("gh pr merge 405 --squash", role="merger", extra_env=env)
    check("a new test file counts from zero at the fork", verdict, "allow")


def test_merge_may_not_close_a_needs_live_issue() -> None:
    """Gate 4. `Closes #N` on an issue that can only be checked live is refused,
    because closing it is what takes it out of every queue."""
    env, _ = with_fake_gh(
        labels={"64": ["P1", "needs:live"], "65": ["P1"]},
        linked={"77": ["64"], "78": ["65"]},
    )
    verdict, reason = run("gh pr merge 77 --squash", role="merger", extra_env=env)
    check("a merge that would close a needs:live issue is refused", verdict, "refuse")
    check("the refusal names Refs #N", "Refs #64" in reason, True)

    verdict, _ = run("gh pr merge 78 --squash", role="merger", extra_env=env)
    check("a merge closing an ordinary issue is unaffected", verdict, "allow")

    verdict, _ = run("gh pr merge 77 --squash", role="merger", extra_env={**env, "POOL_LIVE": "0"})
    check("with live checks off, needs:live does not hold the merge", verdict, "allow")

    # `Refs #N` leaves closingIssuesReferences empty, which is the whole point:
    # the field is populated from the closing keyword and from nothing else.
    env, _ = with_fake_gh(labels={"64": ["P1", "needs:live"]}, linked={"79": []})
    verdict, _ = run("gh pr merge 79 --squash", role="merger", extra_env=env)
    check("the same branch merges once the body says Refs", verdict, "allow")


def test_merge_may_not_change_protected_paths() -> None:
    """Gate 6. GUARD_PROTECTED_PATHS are the owner's: workers edited a fixture the
    owner's demo replays to fit their own tests."""
    env, _ = with_fake_gh(prs={
        "710": {"files": [{"path": "src/app/cards.js", "changeType": "MODIFIED"},
                          {"path": "spec/fixtures/script.json", "changeType": "MODIFIED"}]},
        "711": {"files": [{"path": "src/app/cards.js", "changeType": "MODIFIED"},
                          {"path": "tests/e2e/fixtures/spec_script.json", "changeType": "ADDED"},
                          {"path": "specs.md", "changeType": "ADDED"}]},
    })
    protected = {**env, "GUARD_PROTECTED_PATHS": "spec/,LICENSE"}
    verdict, reason = run("gh pr merge 710 --squash", role="merger", extra_env=protected)
    check("a pull request that changes a protected path is refused", verdict, "refuse")
    check("the refusal names the file and the way out",
          ("spec/fixtures/script.json" in reason, "git checkout origin/main --" in reason), (True, True))
    verdict, _ = run("gh pr merge 711 --squash", role="merger", extra_env=protected)
    check("a test's own copy, and a path that only starts the same, are fine", verdict, "allow")
    verdict, _ = run("gh pr merge 710 --squash", role="merger", extra_env=env)
    check("no protected paths: the gate is off", verdict, "allow")


def test_merge_may_not_remove_baseline_tests() -> None:
    """A consolidation chore's PR deleted a file of the owner's baseline tests, and the
    review gate skipped it as a test-only PR."""
    baseline = "def test_a01():\n    assert 1\ndef test_a02():\n    assert 1\n"
    base = make_commit({"tests/e2e/test_iteration_1.py": baseline, "tests/e2e/test_x.py": "def test_x():\n    pass\n"})
    body = ("<!-- worker -->\nCloses #132\n- **Sweep SHA:** {sha}\n- **Sweep:** waived - loop/process\n"
            "- **Fail-before:** waived - loop/process\n")

    def verdict_for(head: str, files: list, labels=None, env_extra=None) -> tuple[str, str]:
        env, _ = with_fake_gh(labels={"132": labels or ["P2", "type:chore"]}, linked={"801": ["132"]}, prs={"801": {
            "headRefOid": head, "baseRefOid": base, "body": body.format(sha=head[:7]), "files": files,
            "headRefName": "issue-132"}})
        return run("gh pr merge 801 --squash", role="merger",
                   extra_env={**env, "GUARD_BASELINE_TESTS": "tests/e2e/test_iteration_1.py", **(env_extra or {})})
    deleted = make_commit({"tests/e2e/test_x.py": "def test_x():\n    pass\n"}, parent=base)
    verdict, reason = verdict_for(deleted, [{"path": "tests/e2e/test_iteration_1.py", "changeType": "DELETED"}])
    check("deleting a baseline test file is refused, naming the tests",
          (verdict, "test_iteration_1.py::test_a01" in reason, "owner-request" in reason), ("refuse", True, True))
    folded = make_commit({"tests/e2e/test_iteration_1.py": "def test_a01():\n    assert 1\n",
                          "tests/e2e/test_x.py": "def test_x():\n    pass\n"}, parent=base)
    verdict, reason = verdict_for(folded, [{"path": "tests/e2e/test_iteration_1.py", "changeType": "MODIFIED"}])
    check("folding one away is refused too", (verdict, "::test_a02" in reason, "::test_a01" in reason),
          ("refuse", True, False))
    moved = make_commit({"tests/e2e/test_iteration_1.py": "def test_a01():\n    assert 1\n",
                         "tests/e2e/test_flow.py": "def test_a02():\n    assert 1\n",
                         "tests/e2e/test_x.py": "def test_x():\n    pass\n"}, parent=base)
    verdict, _ = verdict_for(moved, [{"path": "tests/e2e/test_iteration_1.py", "changeType": "MODIFIED"},
                                     {"path": "tests/e2e/test_flow.py", "changeType": "ADDED"}])
    check("a baseline test moved to another file, under its name, is fine", verdict, "allow")
    added = make_commit({"tests/e2e/test_iteration_1.py": baseline + "def test_a03():\n    assert 1\n",
                         "tests/e2e/test_x.py": "def test_x():\n    pass\n"}, parent=base)
    verdict, _ = verdict_for(added, [{"path": "tests/e2e/test_iteration_1.py", "changeType": "MODIFIED"}])
    check("adding a case to a baseline file is fine", verdict, "allow")
    verdict, _ = verdict_for(deleted, [{"path": "tests/e2e/test_iteration_1.py", "changeType": "DELETED"}],
                             labels=["P1", "owner-request"])
    check("the owner asked for it (owner-request): allowed", verdict, "allow")
    verdict, _ = verdict_for(deleted, [{"path": "tests/e2e/test_iteration_1.py", "changeType": "DELETED"}],
                             env_extra={"GUARD_BASELINE_TESTS": "off"})
    check("[guard] baseline_tests = off: the gate is off", verdict, "allow")


def test_needs_live_close_requires_evidence() -> None:
    """The other end of gate 4: the issue stays open until somebody says where
    they saw the behavior."""
    env, _ = with_fake_gh(labels={"64": ["P1", "needs:live", "stage:live"]})
    verdict, reason = run("gh issue close 64", role="monitor", extra_env=env)
    check("closing a needs:live issue with a bare thread is refused", verdict, "refuse")
    check("the refusal names the line it wants", "live-verified:" in reason, True)

    env, _ = with_fake_gh(
        labels={"64": ["P1", "needs:live", "stage:live"]},
        comments={"64": ["<!-- tester -->\nRan it on staging.\n\nlive-verified: checkout flow on staging 2026-09-14\n"]},
    )
    verdict, _ = run("gh issue close 64", role="monitor", extra_env=env)
    check("a thread carrying the evidence line closes", verdict, "allow")
    env, _ = with_fake_gh(
        labels={"64": ["P1", "needs:live", "stage:live"]},
        comments={"64": ["[stranger] live-verified: works for me 2026-09-14\n"]},
    )
    verdict, _ = run("gh issue close 64", role="monitor", extra_env=env)
    check("the line from someone without write access is not evidence", verdict, "refuse")
    for body, want, who in (
        ("<!-- monitor -->\nThe PR's checks passed.\nlive-verified: PR #106 ran it 2026-10-23\n", "refuse", "the monitor"),
        ("<!-- worker -->\nlive-verified: my own run 2026-10-23\n", "refuse", "a worker"),
        ("Checked it myself.\nlive-verified: on my laptop 2026-10-23\n", "allow", "the owner (no role marker)"),
    ):
        env, _ = with_fake_gh(labels={"64": ["P1", "needs:live", "stage:live"]}, comments={"64": [body]})
        verdict, _ = run("gh issue close 64", role="monitor", extra_env=env)
        check(f"a live-verified line from {who}: {want}", verdict, want)
    for stage, want in (("stage:live", "refuse"), ("stage:owner", "refuse"), ("stage:triage", "allow")):
        env, _ = with_fake_gh(labels={"64": ["P1", "needs:live", stage]})
        verdict, reason = run(f"gh issue edit 64 --remove-label needs:live,{stage} && gh issue close 64",
                              role="monitor", extra_env=env)
        check(f"the monitor taking needs:live off at {stage}: {want}", verdict, want)
    check("and the refusal says to ask the owner", "stage:owner" in reason or want == "allow", True)
    env, _ = with_fake_gh(labels={"64": ["P1", "needs:live", "stage:live"]})
    verdict, reason = run("gh issue edit 64 --remove-label needs:live", role="monitor", extra_env=env)
    check("in a command of its own too", (verdict, "Evidence in the pull request is not a live check" in reason),
          ("refuse", True))

    # Taking the label off in the same command is the sanctioned way out, the
    # same shape as lifting `hold:monitor` and closing in one breath.
    env, _ = with_fake_gh(labels={"64": ["P1", "needs:live"]})
    verdict, _ = run(
        "gh issue edit 64 --remove-label needs:live && gh issue close 64",
        role="monitor", extra_env=env)
    check("dropping the label in the same command is allowed", verdict, "allow")

    env, _ = with_fake_gh(labels={"65": ["P1"]})
    verdict, _ = run("gh issue close 65", role="monitor", extra_env=env)
    check("closing an ordinary issue is unaffected", verdict, "allow")


def test_needs_live_label_permissions() -> None:
    """Removing the marker is the bypass, so it is narrower than applying it."""
    env, _ = with_fake_gh()
    for role in ("tester", "monitor", "aide"):
        verdict, _ = run("gh issue edit 64 --add-label needs:live",
                         role=role, extra_env=env)
        check(f"{role} may mark an issue needs:live", verdict, "allow")

    verdict, _ = run("gh issue edit 64 --remove-label needs:live",
                     role="tester", extra_env=env)
    check("the tester may not take needs:live off", verdict, "refuse")

    verdict, _ = run("gh issue edit 64 --remove-label needs:live",
                     role="monitor", extra_env=env)
    check("the monitor may take needs:live off", verdict, "allow")

    verdict, _ = run("gh issue edit 64 --add-label stage:live --remove-label stage:pr",
                     role="merger", extra_env=env)
    check("the merger swaps stage:pr for stage:live at the merge", verdict, "allow")

    # Merger handback: swap stage:pr for stage:triage on refusal
    verdict, _ = run("gh issue edit 64 --remove-label stage:pr --add-label stage:triage",
                     role="merger", extra_env=env)
    check("the merger swaps stage:pr for stage:triage on handback", verdict, "allow")

    verdict, _ = run("gh issue edit 64 --remove-label stage:triage",
                     role="merger", extra_env=env)
    check("the merger cannot remove stage:triage", verdict, "refuse")

    verdict, _ = run("gh issue edit 64 --add-label stage:live", role="tester",
                     extra_env=env)
    check("the tester does not move issues into stage:live", verdict, "refuse")

    # stage:triage permissions for tester and aide
    verdict, _ = run("gh issue edit 1 --add-label stage:triage", role="tester")
    check("the tester may apply stage:triage", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label stage:triage", role="tester")
    check("the tester may not remove stage:triage", verdict, "refuse")


def test_pr_merge_validates_sweep_breakdown_and_completeness() -> None:
    """Gate 2: the `green:` line adds up, and it is the line the sweep recorded."""
    commit = make_commit({RUNNER: "# runner\n", "clean.py": "def foo() -> int:\n    return 42\n"})
    full = "65 green: unit 40 · e2e 25"

    def verdict_for(key: str, body_extra: str) -> tuple[str, str]:
        env, _ = with_fake_gh(prs={key: {
            "headRefOid": commit,
            "body": (f"<!-- worker -->\nCloses #{key}\n- **Sweep SHA:** {commit[:7]}\n"
                     f"- **Fail-before:** waived - owner ruling\n{body_extra}"),
            "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
        }})
        return run(f"gh pr merge {key} --squash", role="merger", extra_env=env)

    # 1. The stated total is not the sum of the areas.
    verdict, reason = verdict_for("11", "`67 green: unit 40 · e2e 25`\n")
    check("a total that is not the sum of its areas is refused", verdict, "refuse")
    check("refusal mentions sum mismatch",
          "sweep breakdown sum does not match stated total" in reason, True)
    check("refusal gives the difference", "-2" in reason, True)
    verdict, reason = verdict_for("12", "`62 green: unit 40 · e2e 25`\n")
    check("refusal gives a positive difference too", "+3" in reason, True)

    write_sweep_record(commit, full)

    # 2. An area left out of the line does not match the record.
    verdict, reason = verdict_for("13", "`40 green: unit 40`\n")
    check("a line missing an area the sweep recorded is refused", verdict, "refuse")
    check("refusal names the missing area", "e2e - vs 25" in reason, True)

    # 3. With a sweep waiver the line is not checked.
    verdict, _ = verdict_for("14", "- **Sweep:** waived - loop/process\n`40 green: unit 40`\n")
    check("a partial line with a sweep waiver is allowed", verdict, "allow")

    # 4. The full line matching the record.
    verdict, _ = verdict_for("15", f"`{full}`\n")
    check("a full line matching the record is allowed", verdict, "allow")

    # 5. A summary count without the line.
    verdict, reason = verdict_for("16", "- **The sweep:** 2/2 green (65 cases)\n")
    check("a summary count with no green line is refused", verdict, "refuse")
    check("refusal asks for the full line", "missing the full sweep line" in reason, True)

    # 6. Areas the sweep never reported, with a total that adds up.
    verdict, reason = verdict_for("17", "`65 green: unit 30 · smoke 10 · e2e 25`\n")
    check("an invented area is refused", verdict, "refuse")
    check("refusal names the invented area", "smoke 10 vs -" in reason, True)
    (TEST_SWEEP_RECORDS / f"{commit}.json").unlink()

    # 7. A run with no pytest summary reports `0 green: sweep 0`, and its record matches.
    write_sweep_record(commit, "0 green: sweep 0")
    verdict, _ = verdict_for("18", "`0 green: sweep 0`\n")
    check("the no-pytest green line is accepted against its record", verdict, "allow")
    (TEST_SWEEP_RECORDS / f"{commit}.json").unlink()


def test_pr_merge_requires_sweep_record() -> None:
    """The `green:` line must match what the sweep recorded at the head. A real line
    measured on a dirty tree before the commit has no record at the head."""
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n",
                          "tests/unit/test_clean.py": "def test_foo():\n    assert True\n",
                          "tests/unit/test_other.py": "def test_other():\n    assert True\n"})
    line = "65 green: unit 40 · e2e 25"

    def verdict_for(key: str, body_line: str, extra: str = "") -> tuple[str, str]:
        env, _ = with_fake_gh(prs={key: {
            "headRefOid": commit,
            "body": (f"<!-- worker -->\nCloses #{key}\n- **Sweep SHA:** {commit[:7]}\n"
                     f"- **Fail-before:** waived - owner ruling\n{extra}`{body_line}`\n"),
            "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
        }})
        return run(f"gh pr merge {key} --squash", role="merger", extra_env=env)

    verdict, reason = verdict_for("51", line)
    check("a green line with no record at the head is refused", verdict, "refuse")
    check("refusal says there is no sweep record", "no sweep record" in reason, True)
    check("refusal says to commit before sweeping", "Commit first" in reason, True)
    check("refusal names the sweep command", "sweep_locked.py" in reason, True)

    write_sweep_record(commit, line.replace("unit 40", "unit 37").replace("65", "62"))
    verdict, reason = verdict_for("52", line)
    check("a green line that differs from the record is refused", verdict, "refuse")
    check("refusal names the differing area", "unit 40 vs 37" in reason, True)

    write_sweep_record(commit, line)
    verdict, _ = verdict_for("53", line)
    check("a green line matching the record is allowed", verdict, "allow")

    (TEST_SWEEP_RECORDS / f"{commit}.json").unlink()
    verdict, _ = verdict_for("54", line, "- **Sweep:** waived - loop/process\n")
    check("a sweep waiver needs no record", verdict, "allow")


def test_empty_stdin() -> None:
    out = raw("")
    check("empty stdin is allowed, not refused", "decision" in out, False)


def test_pr_create_gates() -> None:
    # 1. Missing Sweep SHA
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(
            "<!-- worker -->\n"
            "## Summary\n"
            "Testing gates.\n\n"
            "- **Fail-before:** tests/unit/test_app.py 762 -> 763\n"
        )
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'test' --body-file {path}", role="worker")
        check("pr create missing Sweep SHA is refused", verdict, "refuse")
        check("refusal names Sweep SHA", "Sweep SHA" in reason, True)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 2. Missing Fail-before (no waiver)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(
            "<!-- worker -->\n"
            "## Summary\n"
            "Testing gates.\n\n"
            "- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\n"
        )
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'test' --body-file {path}", role="worker")
        check("pr create missing Fail-before is refused", verdict, "refuse")
        check("refusal names Fail-before", "Fail-before" in reason, True)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 3. A body written as prose (lacking both lines) is blocked
    prose_body = (
        "<!-- worker -->\n"
        "Refs #60\n\n"
        "## Summary\n"
        "Restricts the allowed hosts.\n\n"
        "## Verification\n"
        "- Before change: tests/test_hosts.py fails\n"
        "- After change: All 763 cases hold\n\n"
        "## Sweep\n"
        "- Base SHA: b77698190d1069f1981f92fc34fd0a1fa41dc035\n"
        "- 5394 green\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(prose_body)
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'fix' --body-file {path}", role="worker")
        check("a prose body without the gate lines is refused", verdict, "refuse")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 4. The same body with both lines passes
    corrected_body = (
        "<!-- worker -->\n"
        "Refs #60\n\n"
        "## Summary\n"
        "Restricts the allowed hosts.\n\n"
        "## Verification\n"
        "- **Fail-before:** tests/unit/test_app.py 762 -> 763\n\n"
        "## Sweep\n"
        "- **Sweep SHA:** 24d2b25da196d45904593f7d1b5b40581771c7e6\n"
        "- 5394 green\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(corrected_body)
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'fix' --body-file {path}", role="worker")
        check("the body with both lines is allowed", verdict, "allow")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 5. Draft carve-out: --draft or -d with body missing sweep/fail-before passes
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(
            "<!-- worker -->\n"
            "## Summary\n"
            "WIP draft PR without sweep or fail-before.\n"
        )
        path = fh.name
    try:
        verdict, _ = run(f"gh pr create --draft --title 'WIP' --body-file {path}", role="worker")
        check("gh pr create --draft skips sweep and fail-before checks", verdict, "allow")

        verdict, _ = run(f"gh pr create -d --title 'WIP' --body-file {path}", role="worker")
        check("gh pr create -d skips sweep and fail-before checks", verdict, "allow")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 6. Each of the 4 fail-before waivers passes
    waivers = [
        "waived - needs: tester repro",
        "waived - loop/process",
        "waived - docs only",
        "waived - owner ruling",
    ]
    for w in waivers:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(
                f"<!-- worker -->\n"
                f"## Summary\nTesting waiver.\n\n"
                f"- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\n"
                f"- **Fail-before:** {w}\n"
            )
            path = fh.name
        try:
            verdict, reason = run(f"gh pr create --title 'waiver test' --body-file {path}", role="worker")
            check(f"Fail-before {w} is allowed", verdict, "allow")
        finally:
            pathlib.Path(path).unlink(missing_ok=True)

    # 7. Flag variations: -F, -F=, --body-file=, --body, -b
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(corrected_body)
        path = fh.name
    try:
        verdict, _ = run(f"gh pr create --title 'fix' -F {path}", role="worker")
        check("gh pr create -F is allowed", verdict, "allow")

        verdict, _ = run(f"gh pr create --title 'fix' -F={path}", role="worker")
        check("gh pr create -F= is allowed", verdict, "allow")

        verdict, _ = run(f"gh pr create --title 'fix' --body-file={path}", role="worker")
        check("gh pr create --body-file= is allowed", verdict, "allow")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # Inline --body and -b
    inline_body = "<!-- worker -->\\n- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\\n- **Fail-before:** waived - loop/process"
    verdict, _ = run(f'gh pr create --title "fix" --body "{inline_body}"', role="worker")
    check("gh pr create --body is allowed", verdict, "allow")

    verdict, _ = run(f'gh pr create --title "fix" -b "{inline_body}"', role="worker")
    check("gh pr create -b is allowed", verdict, "allow")

    # Any pytest file under tests/ will do, at any depth: no suite layout is assumed.
    for test_path in ("tests/test_hosts.py", "tests/integration/api/test_hosts.py"):
        body = ("<!-- worker -->\\n- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\\n"
                f"- **Fail-before:** {test_path} 3 -> 4")
        verdict, _ = run(f'gh pr create --title "fix" --body "{body}"', role="worker")
        check(f"Fail-before naming {test_path} is allowed", verdict, "allow")

    # 8. Fail-before non-increasing count is refused
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(
            "<!-- worker -->\n"
            "## Summary\nEqual counts.\n\n"
            "- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\n"
            "- **Fail-before:** tests/unit/test_app.py 762 -> 762\n"
        )
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'fix' --body-file {path}", role="worker")
        check("Fail-before equal counts is refused", verdict, "refuse")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 9. Fail-before naming a non-pytest path is refused at creation too
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(
            "<!-- worker -->\n"
            "- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\n"
            "- **Fail-before:** tools/hosts_check.py 762 -> 763\n"
        )
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'fix' --body-file {path}", role="worker")
        check("pr create naming a non-pytest Fail-before path is refused", verdict, "refuse")
        check("refusal says it is not a pytest file", "not a pytest file" in reason, True)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_sweep_line_required_when_the_runner_is_in_the_tree() -> None:
    """A head that carries .claude/hooks/sweep_run.py could have been swept, so its
    body needs the sweep's green line (or a waiver), and the line needs its record.
    The areas are whatever the sweep reported; none are assumed."""
    commit = make_commit({RUNNER: "# runner\n", "src.py": "x = 1\n"})
    line = "12 green: tests 9 · integration 3"

    def verdict_for(body_extra: str, key: str) -> tuple[str, str]:
        env, _ = with_fake_gh(prs={key: {
            "headRefOid": commit,
            "body": (f"<!-- worker -->\nCloses #{key}\n- **Sweep SHA:** {commit}\n"
                     f"- **Fail-before:** waived - owner ruling\n{body_extra}"),
            "files": [{"path": "src.py", "changeType": "MODIFIED"}],
        }})
        return run(f"gh pr merge {key} --squash", role="merger", extra_env=env)

    verdict, reason = verdict_for("", "31")
    check("a head with the runner and no green line is refused", verdict, "refuse")
    check("refusal asks for the sweep's line", "missing the sweep's `green:` line" in reason, True)
    check("refusal names the sweep command", "sweep_locked.py" in reason, True)

    verdict, _ = verdict_for("- **Sweep:** waived - docs only\n", "32")
    check("a sweep waiver stands in for the line", verdict, "allow")

    verdict, reason = verdict_for(f"`{line}`\n", "33")
    check("a full green line with no record is refused", verdict, "refuse")
    check("refusal says there is no sweep record", "no sweep record" in reason, True)

    write_sweep_record(commit, line)
    verdict, _ = verdict_for(f"`{line}`\n", "34")
    check("a full green line matching the record is allowed", verdict, "allow")
    (TEST_SWEEP_RECORDS / f"{commit}.json").unlink()

    # A head without the runner (or one this checkout cannot read) asks for nothing.
    bare = make_commit({"src.py": "x = 1\n"})
    env, _ = with_fake_gh(prs={"35": {
        "headRefOid": bare,
        "body": (f"<!-- worker -->\nCloses #35\n- **Sweep SHA:** {bare}\n"
                 "- **Fail-before:** waived - owner ruling\n"),
        "files": [{"path": "src.py", "changeType": "MODIFIED"}],
    }})
    verdict, _ = run("gh pr merge 35 --squash", role="merger", extra_env=env)
    check("a head without the runner needs no green line", verdict, "allow")


# --------------------------------------------------------------------------- #
# The worker pool. ccpool.py claims one issue per worker (claim.py, written to
# $WRK_CLAIM_FILE), and the worker may move stage labels only there.
# --------------------------------------------------------------------------- #


def claim_env(issue: int) -> dict:
    path = pathlib.Path(TEST_TMPDIR.name) / f"claim-{issue}.json"
    path.write_text(json.dumps({"issue": issue, "token": "t", "loop": "3"}))
    return {"WRK_CLAIM_FILE": str(path)}


def test_worker_comments_and_labels_on_its_claim() -> None:
    env = claim_env(12)
    verdict, _ = run("gh issue comment 12 --body 'working on it'", role="worker", extra_env=env)
    check("an unsigned worker comment is refused", verdict, "refuse")
    verdict, _ = run("gh issue comment 12 --body '<!-- worker --> traced'", role="worker", extra_env=env)
    check("a signed worker comment is allowed", verdict, "allow")

    cmd = "gh issue edit 12 --add-label stage:pr --remove-label stage:doing,stage:triage,stage:ready,stage:owner"
    verdict, _ = run(cmd, role="worker", extra_env=env)
    check("stage:pr on the claimed issue, clearing every prior stage, is allowed", verdict, "allow")

    verdict, reason = run(
        "gh issue edit 12 --add-label stage:pr --remove-label stage:triage,stage:ready,stage:owner",
        role="worker", extra_env=env)
    check("stage:pr that leaves stage:doing set is refused", verdict, "refuse")
    check("the refusal names stage:doing", "stage:doing" in reason, True)

    verdict, reason = run(cmd.replace(" 12 ", " 13 "), role="worker", extra_env=env)
    check("stage:pr on another issue is refused", verdict, "refuse")
    check("the refusal names the claim", "#12" in reason and "#13" in reason, True)

    verdict, _ = run(cmd, role="worker")
    check("stage moves without a claim file are refused", verdict, "refuse")

    verdict, _ = run("gh issue edit 12 --add-label stage:triage --remove-label stage:doing",
                     role="worker", extra_env=env)
    check("releasing its own claim to stage:triage is allowed", verdict, "allow")

    verdict, _ = run("gh api -X POST repos/o/r/issues/13/labels -f 'labels[]=stage:doing'",
                     role="worker", extra_env=env)
    check("the REST route to another issue's stage is refused", verdict, "refuse")

    verdict, _ = run("gh issue edit 12 --add-label needs:live", role="worker", extra_env=env)
    check("the worker may apply needs:live (it does the triage analysis now)", verdict, "allow")

    verdict, _ = run("gh issue edit 12 --remove-label needs:live", role="worker", extra_env=env)
    check("but may not remove it", verdict, "refuse")


def test_worker_does_not_land_or_rank() -> None:
    env = claim_env(12)
    for cmd, what in (
        ("gh pr merge 40 --squash", "merge"),
        ("gh issue close 12", "close"),
        ("gh api -X PATCH repos/o/r/issues/12 -f state=closed", "close over REST"),
        ("gh issue edit 12 --add-label P0 --remove-label P2", "re-prioritize"),
        ("gh issue edit 12 --add-label hold:monitor", "set the monitor's hold"),
        ("gh issue edit 12 --add-label stage:live --remove-label stage:doing", "set stage:live"),
    ):
        verdict, _ = run(cmd, role="worker", extra_env=env)
        check(f"the worker may not {what}", verdict, "refuse")


def test_worker_git_boundaries() -> None:
    for cmd in (
        "git push origin main",
        "git push origin HEAD:main",
        "git -C /tmp/wrk-3-12-wt push origin HEAD:refs/heads/main",
        "git push --force origin +main",
    ):
        verdict, _ = run(cmd, role="worker")
        check(f"worker push to main is refused: {cmd}", verdict, "refuse")
    for cmd in (
        "git push -u origin issue-12",
        "git -C /tmp/wrk-3-12-wt push --force-with-lease origin HEAD:issue-12",
        "git push origin issue-12-main-fix",
    ):
        verdict, _ = run(cmd, role="worker")
        check(f"worker push of a branch is allowed: {cmd}", verdict, "allow")
    verdict, _ = run("git add .claude/hooks/role_guard.py src/x.py", role="worker")
    check("worker staging .claude/ is refused", verdict, "refuse")
    verdict, _ = run("git -C /tmp/wrk-3-12-wt add .agents/rules/global.md", role="worker")
    check("worker staging .agents/ is refused", verdict, "refuse")
    verdict, _ = run("git add .issuelooper/config.toml", role="worker")
    check("worker staging .issuelooper/ is refused", verdict, "refuse")
    verdict, _ = run("git commit --no-verify -m fix", role="worker")
    check("worker --no-verify is refused", verdict, "refuse")
    verdict, _ = run("git add src tests && git commit -m 'fix the weather card'", role="worker",
                     cwd="/tmp/wrk-3-12-wt")
    check("worker committing project files is allowed", verdict, "allow")
    verdict, _ = run("git push origin main", role="merger")
    check("the push rule is the worker's only (merger unchanged)", verdict, "allow")
    verdict, _ = run('git push -u origin "HEAD:issue-5"\necho "PR against main next"', role="worker")
    check("a command ends at a newline: `main` on the next line is not the push's", verdict, "allow")
    verdict, _ = run("cd /tmp/wrk-3-12-wt\ngit push origin HEAD:main", role="worker")
    check("a push to main on its own line is still refused", verdict, "refuse")


def test_git_stash_and_the_shared_checkout() -> None:
    # A worker's `git stash push` in its worktree left the shared checkout conflicted
    # (worktrees share refs/stash with it), and merges stopped on Gate 0.
    for role in ("worker", "merger", "tester", "monitor", "aide"):
        for cmd in ("cd /tmp/wrk-3-12-wt && git stash push -u -q", "git -C /tmp/wrk-3-12-wt stash",
                    "git stash pop", "cd /tmp/x\ngit -c user.name=x stash list"):
            verdict, reason = run(cmd, role=role)
            check(f"{cmd!r} is refused for {role}", verdict, "refuse")
        check(f"the refusal for {role} says to commit work in progress", "Commit work in progress" in reason, True)
    verdict, _ = run("git stash list", role="")
    check("a session with no role (the owner's own) is not bound", verdict, "allow")
    shared = os.environ["SHARED_REPO_PATH"]
    for cmd in (f"git -C {shared} reset --hard HEAD", 'git -C "$SHARED_REPO_PATH" checkout -- src/a.py',
                f"cd {shared} && git clean -fd", "git reset --hard origin/main", "git pull --rebase",
                "cd /tmp/wrk-3-12-wt && git status; cd $SHARED_REPO_PATH; git commit -am x"):
        verdict, reason = run(cmd, role="worker")
        check(f"changing the shared checkout is refused: {cmd!r}", verdict, "refuse")
    check("and the refusal says where to run git", "your own worktree" in reason, True)
    verdict, _ = run("git reset --hard origin/main", role="worker", cwd=shared)
    check("Claude Code's cwd at the shared checkout counts too", verdict, "refuse")
    for cmd in ('git -C "$SHARED_REPO_PATH" fetch -q origin main',
                'git -C "$SHARED_REPO_PATH" worktree add -B issue-12 /tmp/wrk-3-12-wt origin/main',
                'git -C "$SHARED_REPO_PATH" archive abc | tar -x -C /tmp/tst-8021-tree',
                "cd /tmp/wrk-3-12-wt && git reset --hard origin/main && git rebase origin/main",
                'git -C "$WT" rebase origin/main', "git status", "git log --oneline -3 origin/main"):
        verdict, _ = run(cmd, role="worker")
        check(f"allowed: {cmd!r}", verdict, "allow")
    verdict, _ = run("git commit -m 'fix'", role="worker", cwd="/tmp/wrk-3-12-wt")
    check("a bare commit in Claude Code's worktree cwd is allowed", verdict, "allow")
    verdict, _ = run("cd /tmp/wrk-3-12-wt && git commit -m fix", role="worker", host=AGY)
    check("agy: a commit after cd into the worktree is allowed", verdict, "allow")
    verdict, _ = run("git commit -m fix", role="worker", host=AGY)
    check("agy: a bare commit runs in the shared checkout and is refused", verdict, "refuse")


def test_a_push_of_a_stopped_rebase_is_refused() -> None:
    # #100 and #120: `git rebase origin/main` stopped on a conflict, the push on the next
    # line of the same command sent origin/main's head to the issue branch, and GitHub
    # closed the pull request for good.
    for role in ("worker", "merger"):
        for cmd in ('cd "$WT" || exit 1\ngit rebase origin/main\ngit push --force-with-lease -u origin "HEAD:issue-4"',
                    'cd "$WT" || exit 1\ngit merge-base --is-ancestor origin/main HEAD || git rebase origin/main\n'
                    'git push --force-with-lease origin "HEAD:issue-33"',
                    'cd "$WT" && git rebase origin/main && uv run ruff check src; git push origin HEAD:issue-4',
                    'git -C "$WT" pull --rebase origin main | tail -3 && git -C "$WT" push origin HEAD:issue-4',
                    'cd "$WT"\ngit add a.py\nGIT_EDITOR=true git rebase --continue\ngit push -f origin HEAD:issue-4'):
            verdict, reason = run(cmd, role=role, host=AGY)
            check(f"{role}: a push after an unchecked rebase is refused: {cmd!r}", verdict, "refuse")
        check(f"{role}: the refusal says to join them with &&", "Join them with `&&`" in reason, True)
    for cmd in ('cd "$WT" && git rebase origin/main && git push --force-with-lease origin HEAD:issue-4',
                'cd "$WT" || exit 1\ngit rebase origin/main || exit 1\nuv run ruff check src\ngit push -f origin HEAD:issue-4',
                'set -e\ncd "$WT"\ngit rebase origin/main\ngit push -f origin HEAD:issue-4',
                'cd "$WT" && git add a.py && GIT_EDITOR=true git rebase --continue && git push -f origin HEAD:issue-4',
                'cd "$WT"\ngit rebase origin/main\ngit status'):
        verdict, reason = run(cmd, role="worker", host=AGY)
        check(f"allowed: {cmd!r}", (verdict, reason), ("allow", ""))

    # The state the push would send: a worktree stopped mid-rebase, and a head with no
    # commits beyond origin/main.
    with tempfile.TemporaryDirectory(prefix="rg-push-") as tmp:
        root = pathlib.Path(tmp)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"}

        def git(*args: str, cwd: pathlib.Path = root / "clone", ok: bool = True) -> None:
            subprocess.run(["git", *args], cwd=cwd, env=env, check=ok, capture_output=True)

        git("init", "-q", "--bare", "-b", "main", str(root / "origin.git"), cwd=root)
        git("clone", "-q", str(root / "origin.git"), str(root / "clone"), cwd=root)
        git("checkout", "-q", "-b", "main")
        (root / "clone/a.py").write_text("x = 1\n")
        git("add", "a.py")
        git("commit", "-qm", "base")
        git("push", "-q", "origin", "main")
        wt = root / "wt"
        git("worktree", "add", "-q", "-b", "issue-4", str(wt), "origin/main")
        (wt / "a.py").write_text("x = 2\n")
        git("commit", "-qam", "fix", cwd=wt)
        push = f"git -C {wt} push --force-with-lease origin HEAD:issue-4"
        verdict, reason = run(push, role="worker", host=AGY)
        check("a head with commits beyond main pushes", (verdict, reason), ("allow", ""))
        (root / "clone/a.py").write_text("x = 3\n")
        git("commit", "-qam", "main moved")
        git("push", "-q", "origin", "main")
        git("fetch", "-q", "origin", cwd=wt)
        git("rebase", "origin/main", cwd=wt, ok=False)  # stops on the conflict in a.py
        verdict, reason = run(push, role="worker", host=AGY)
        check("a push from a worktree mid-rebase is refused", verdict, "refuse")
        check("the refusal says to finish the rebase", "git rebase --continue" in reason, True)
        verdict, reason = run(f"cd {wt} && git rebase --abort && git push -f origin HEAD:issue-4", role="worker",
                              host=AGY)
        check("aborting the rebase in the same command lets the push through", (verdict, reason), ("allow", ""))
        git("rebase", "--abort", cwd=wt)
        git("reset", "-q", "--hard", "origin/main", cwd=wt)
        verdict, reason = run(push, role="merger", host=AGY)
        check("a head with no commits beyond origin/main is refused", verdict, "refuse")
        check("the refusal says to check the log", "origin/main..HEAD" in reason, True)
        verdict, _ = run(f"git -C {wt} push origin HEAD:refs/heads/scratch", role="worker", host=AGY)
        check("a push to a branch that is not an issue branch is not this rule's", verdict, "allow")


def test_files_in_the_shared_checkout() -> None:
    # The shared checkout's .env (only the owner has it) disappeared in a test run of the sample and live
    # checks stopped for hours; workers link it into their worktrees and remove the link.
    shared = os.environ["SHARED_REPO_PATH"]
    for cmd in ("rm -f .env", 'rm "$SHARED_REPO_PATH/.env"', "mv .env .env.bak", f"rm -f {shared}/.env",
                'cp /tmp/x.py "${SHARED_REPO_PATH}/src/a.py"', "echo KEY=1 > .env", "printf x >>.env",
                'ln -sf "$SHARED_REPO_PATH/.env" .env', 'ln -sf /a/.env "$SHARED_REPO_PATH/.env"',
                "uv sync && rm -rf src/old", 'unlink "$SHARED_REPO_PATH/.env"'):
        verdict, reason = run(cmd, role="worker", host=AGY)
        check(f"refused (it runs in, or names, the shared checkout): {cmd!r}", verdict, "refuse")
    check("the refusal says to name the worktree", "rm -f \"$WT/.env\"" in reason, True)
    for cmd in ('cd "$WT"\nrm -f .env', 'cd "$WT"; ln -sf "$SHARED_REPO_PATH/.env" .env'):
        verdict, reason = run(cmd, role="worker", host=AGY)
        check(f"refused (if the cd fails it runs in the shared checkout): {cmd!r}",
              (verdict, "not checked" in reason), ("refuse", True))
    verdict, _ = run("rm -f .env", role="worker", cwd=shared)
    check("Claude Code's cwd at the shared checkout counts too", verdict, "refuse")
    for cmd in ('cd "$WT" && rm -f .env', 'rm -f "$WT/.env"', 'ln -sf "$SHARED_REPO_PATH/.env" "$WT/.env"',
                'cd "$WT" || exit 1\nrm -f .env', 'set -e\ncd "$WT"\nrm -f .env',
                'cd /tmp/issuelooper-x/wrk-3-12-wt && ln -sf "$SHARED_REPO_PATH/.env" .env && uv run app',
                'rm -rf "$SHARED_REPO_PATH/.ux/abc123/live-1"', 'rm -rf "$ISSUELOOPER_TMP/tst-8021-tree"',
                "cat > $ISSUELOOPER_TMP/body.md <<'MD'\nThen rm -f .env in your worktree.\nMD\nwc -l $ISSUELOOPER_TMP/body.md",
                "python3 -c 'import os; print(1)' > /dev/null 2>&1", 'cp "$SHARED_REPO_PATH/.env.example" "$WT/.env"',
                "ls -la .env", "uv run pytest -q 2>&1 | tail -5"):
        verdict, _ = run(cmd, role="worker", host=AGY)
        check(f"allowed: {cmd!r}", verdict, "allow")
    verdict, _ = run("rm -f .env", role="worker", cwd="/tmp/issuelooper-x/wrk-3-12-wt")
    check("a bare rm in Claude Code's worktree cwd is allowed", verdict, "allow")
    verdict, _ = run("rm -f .env", role="", host=AGY)
    check("a session with no role (the owner's own) is not bound", verdict, "allow")


def test_worker_pr_create_is_gated_and_signed() -> None:
    def body_file(text: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(text)
            return fh.name

    good = body_file("<!-- worker -->\nFixes #12\n- **Sweep SHA:** abc1234\n"
                     "`45 green: unit 22 · e2e 23`\n- **Fail-before:** tests/unit/test_app.py 7 -> 8\n")
    unsigned = body_file("Fixes #12\n- **Sweep SHA:** abc1234\n- **Fail-before:** tests/unit/test_app.py 7 -> 8\n")
    no_fb = body_file("<!-- worker -->\nFixes #12\n- **Sweep SHA:** abc1234\n")
    try:
        verdict, _ = run(f"gh pr create --title t --body-file {good}", role="worker")
        check("a signed worker PR with Sweep SHA and Fail-before is allowed", verdict, "allow")
        verdict, _ = run(f"gh pr create --title t --body-file {unsigned}", role="worker")
        check("an unsigned worker PR is refused", verdict, "refuse")
        verdict, reason = run(f"gh pr create --title t --body-file {no_fb}", role="worker")
        check("a worker PR without Fail-before is refused", verdict, "refuse")
        check("the refusal names Fail-before", "Fail-before" in reason, True)
    finally:
        for f in (good, unsigned, no_fb):
            pathlib.Path(f).unlink(missing_ok=True)


def test_rebased_pr_merges_once_resweept() -> None:
    """The merger rebases a PR, sweeps the new head, updates the body.

    Gate 2 then sees a record for the new head and the merge goes through; the
    stale body (old Sweep SHA) is still refused. Gate 3 measures from the new
    fork point, where the reproducing case is still new."""
    base = make_commit({RUNNER: "# runner\n", "tests/unit/test_app.py": "def test_a():\n    pass\n"})
    main2 = make_commit({RUNNER: "# runner\n", "tests/unit/test_app.py": "def test_a():\n    pass\n",
                         "src.py": "y = 2\n"}, parent=base)
    old_head = make_commit({RUNNER: "# runner\n",
                            "tests/unit/test_app.py": "def test_a():\n    pass\n\ndef test_b():\n    pass\n"},
                           parent=base)
    new_head = make_commit({RUNNER: "# runner\n", "src.py": "y = 2\n",
                            "tests/unit/test_app.py": "def test_a():\n    pass\n\ndef test_b():\n    pass\n"},
                           parent=main2)
    line = "46 green: unit 23 · e2e 23"
    files = [{"path": "tests/unit/test_app.py", "changeType": "MODIFIED", "additions": 3}]

    def body(sha: str) -> str:
        return (f"<!-- worker -->\nFixes #12\n- **Sweep SHA:** {sha}\n`{line}`\n"
                "- **Fail-before:** tests/unit/test_app.py 1 -> 2\n")

    write_sweep_record(new_head, line)
    env, _ = with_fake_gh(prs={"60": {"headRefOid": new_head, "baseRefOid": main2,
                                      "body": body(old_head), "files": files}})
    verdict, reason = run("gh pr merge 60 --squash", role="merger", extra_env=env)
    check("a rebased PR whose body still names the old sweep is refused", verdict, "refuse")
    check("the refusal says the head moved", "does not match Sweep SHA" in reason, True)

    env, _ = with_fake_gh(prs={"61": {"headRefOid": new_head, "baseRefOid": main2,
                                      "body": body(new_head), "files": files}})
    verdict, reason = run("gh pr merge 61 --squash", role="merger", extra_env=env)
    check("a rebased PR re-swept at its new head merges", verdict, "allow")
    rec = json.loads((TEST_SWEEP_RECORDS / f"{new_head}.json").read_text())
    (TEST_SWEEP_RECORDS / f"{new_head}.json").write_text(json.dumps({**rec, "kind": "affected", "tests": ["t.py"]}))
    verdict, reason = run("gh pr merge 61 --squash", role="merger", extra_env={**env, "POOL_MERGE_TRAIN": "0"})
    check("an affected record with the merge train off: refused, a full sweep asked for",
          (verdict, "--full" in reason), ("refuse", True))
    verdict, _ = run("gh pr merge 61 --squash", role="merger", extra_env={**env, "POOL_MERGE_TRAIN": "1"})
    check("with the train on (it checks the whole suite on the merged tree): it is enough", verdict, "allow")
    (TEST_SWEEP_RECORDS / f"{new_head}.json").unlink()

    # Gate 3 still binds after the rebase: if main already gained the same case
    # (another PR landed it), the rebased head adds lines but no case.
    main3 = make_commit({RUNNER: "# runner\n",
                         "tests/unit/test_app.py": "def test_a():\n    pass\n\ndef test_b():\n    pass\n"},
                        parent=main2)
    same = make_commit({RUNNER: "# runner\n",
                        "tests/unit/test_app.py": "def test_a():\n    pass\n\ndef test_b():\n    assert 1\n"},
                       parent=main3)
    write_sweep_record(same, line)
    env, _ = with_fake_gh(prs={"62": {"headRefOid": same, "baseRefOid": main3,
                                      "body": body(same), "files": files}})
    verdict, reason = run("gh pr merge 62 --squash", role="merger", extra_env=env)
    check("a rebase that leaves no new case is still refused by Gate 3", verdict, "refuse")
    check("the refusal is the case count", "but no case" in reason, True)
    (TEST_SWEEP_RECORDS / f"{same}.json").unlink()



def test_pool_merger_lands_only_its_own_pr() -> None:
    """ccpool.py names the merger's PR in $MERGE_PR; other merges are refused."""
    base = make_commit({RUNNER: "# runner\n", "tests/unit/test_app.py": "def test_a():\n    pass\n"})
    head = make_commit({RUNNER: "# runner\n",
                        "tests/unit/test_app.py": "def test_a():\n    pass\n\ndef test_b():\n    pass\n"},
                       parent=base)
    line = "46 green: unit 23 · e2e 23"
    files = [{"path": "tests/unit/test_app.py", "changeType": "MODIFIED", "additions": 3}]
    body = (f"<!-- worker -->\nFixes #12\n- **Sweep SHA:** {head}\n`{line}`\n"
            "- **Fail-before:** tests/unit/test_app.py 1 -> 2\n")
    write_sweep_record(head, line)
    pr = {"headRefOid": head, "baseRefOid": base, "body": body, "files": files}
    env, _ = with_fake_gh(prs={"70": pr, "71": pr})
    verdict, reason = run("gh pr merge 71 --squash", role="merger", extra_env={**env, "MERGE_PR": "70"})
    check("a pool merger may not merge another merger's PR", verdict, "refuse")
    check("the refusal names both PRs", "#70" in reason and "#71" in reason, True)
    verdict, _ = run("gh api -X PUT repos/o/r/pulls/71/merge", role="merger", extra_env={**env, "MERGE_PR": "70"})
    check("nor through the REST spelling", verdict, "refuse")
    verdict, _ = run("gh pr merge 70 --squash", role="merger", extra_env={**env, "MERGE_PR": "70"})
    check("its own PR merges through the usual gates", verdict, "allow")
    verdict, _ = run("gh pr merge 71 --squash", role="merger", extra_env=env)
    check("without $MERGE_PR any PR is the merger's", verdict, "allow")
    session = {**env, "MERGE_PR": "70", "POOL_UNIT": "mrg:70@bbbbbbbbbbbb@cccccccccccc"}
    verdict, reason = run("gh pr merge 70 --squash", role="merger", extra_env=session)
    check("a merger session does not merge, not even its own PR", verdict, "refuse")
    check("it is told the supervisor merges", "supervisor merges" in reason, True)
    supervisor = {**env, "MERGE_PR": "70", "POOL_UNIT": "merge:70@bbbbbbbbbbbb@cccccccccccc"}
    verdict, _ = run("gh pr merge 70 --squash", role="merger", extra_env=supervisor)
    check("the supervisor's merge unit merges through the gates", verdict, "allow")
    (TEST_SWEEP_RECORDS / f"{head}.json").unlink()


def test_monitor_keeps_the_moderator_labels() -> None:
    """The monitor owns triage and priority; the guard must let it."""
    env, _ = with_fake_gh()
    allowed = {
        "triage a new issue": "gh issue edit 50 --add-label P1,type:bug,stage:triage",
        "re-prioritize": "gh issue edit 12 --add-label P0 --remove-label P2",
        "hold low-value work": "gh issue edit 30 --add-label hold:monitor",
        "lift its own hold": "gh issue edit 30 --remove-label hold:monitor",
        "surface an owner decision": "gh issue edit 31 --add-label stage:owner --remove-label stage:triage",
        "record a dependency":
            "gh api -X POST repos/o/r/issues/12/dependencies/blocked_by -F issue_id=99",
        "mark needs:live": "gh issue edit 33 --add-label needs:live",
        "make a part a sub-issue of its parent":
            "gh api -X POST repos/o/r/issues/12/sub_issues -F sub_issue_id=4242",
        "remove a dependency it no longer needs":
            "gh api -X DELETE repos/o/r/issues/12/dependencies/blocked_by/4242",
        "file a part of a split issue":
            "gh issue create --title 'Places card (part of #12)' --label type:feature-request --label P1 "
            "--label stage:triage --body '<!-- monitor -->\n<!-- part of #12 -->\nThe card.'",
        "close a duplicate UX proposal":
            "gh issue close 301 --reason 'not planned' --comment '<!-- monitor -->\nDuplicate of #238.'",
    }
    for what, cmd in allowed.items():
        verdict, reason = run(cmd, role="monitor", extra_env=env)
        check(f"the monitor may {what} ({reason[:80]})", verdict, "allow")
    verdict, reason = run(
        "gh issue close 41 --reason 'not planned' --comment '<!-- monitor -->\nLow value for now.'",
        role="monitor", extra_env=env)
    check(f"the monitor may close low-value work with a signed reason ({reason[:80]})", verdict, "allow")

def test_review_gate() -> None:
    """Gate 5: a merge needs a passing review record for the PR head."""
    records = pathlib.Path(tempfile.mkdtemp(prefix="role-guard-review-"))
    head = "0123456789abcdef0123456789abcdef01234567"  # the fake gh's default PR head
    env, _ = with_fake_gh(linked={"50": []})
    env.update(REVIEW_GATE="on", REVIEW_DIR=str(records))

    def merge():
        return run("gh pr merge 50 --squash", role="merger", extra_env=env)

    verdict, reason = merge()
    check("no review record: the merge is refused", verdict, "refuse")
    check("and the refusal says how to review", 'review_gate.py" --pr 50' in reason, True)
    for v, want in (("pass", "allow"), ("skipped", "allow"), ("override", "allow"), ("error", "refuse"),
                    ("cap", "refuse"), ("owner", "refuse"), ("reject", "refuse")):
        (records / f"{head}.json").write_text(json.dumps({"verdict": v, "reason": "Invented data."}))
        verdict, reason = merge()
        check(f"a {v} record: {want}", verdict, want)
    check("a reject says it was handed back already", "already commented on the PR" in reason, True)
    (records / f"{head}.json").unlink()
    (records / f"{'9' * 40}.json").write_text(json.dumps({"verdict": "pass"}))
    check("a pass for another head does not count", merge()[0], "refuse")
    env["REVIEW_GATE"] = "1"
    check("REVIEW_GATE=1 enforces the gate", merge()[0], "refuse")
    for off in ("off", "0"):
        env["REVIEW_GATE"] = off
        check(f"REVIEW_GATE={off} skips the gate", merge()[0], "allow")


def write_devcheck_record(sha: str, status: str = "pass", key: str | None = None,
                          steps: list | None = None, rows: list | None = None, skips: dict | None = None) -> None:
    """The record devcheck.py would have written at `sha`."""
    if steps is None:
        steps = [{"name": "smoke", "rc": 0, "log": "/tmp/smoke.log"},
                 {"name": "script 1", "rc": 0 if status == "pass" else 1, "log": "/tmp/script-1.log"}]
    TEST_DEVCHECK_RECORDS.mkdir(parents=True, exist_ok=True)
    rec = {"sha": sha, "status": status, "key": key, "steps": steps}
    if rows is not None:
        rec.update(rows=rows, skips=skips or {})
    (TEST_DEVCHECK_RECORDS / f"{sha}.json").write_text(json.dumps(rec))


CHECKLIST_ENV = {
    "ACCEPT_CHECKLIST": json.dumps({"viewports": ["1280x800", "320x640"], "languages": ["en", "ja"],
                                    "always": ["keyboard: Tab, Enter, Escape"],
                                    "triggers": {"src/static/*": ["viewports", "languages"]}}),
    "ACCEPT_PATHS": "src/",
}


def test_gate_7_holds_the_live_check_to_the_checklist() -> None:
    """Most reopened issues were partial implementations in a few rows: 320 px, Escape,
    the second language. Gate 7 refuses a live check that skipped a row the change triggers."""
    head = "6" * 40
    on = {"POOL_DEVCHECK": "1", "DEVCHECK_PATHS": "src/", **CHECKLIST_ENV}
    ui = [{"path": "src/static/js/sheet.js", "changeType": "MODIFIED"}]
    body = (f"<!-- worker -->\nFixes #90\n- **Sweep SHA:** {head}\n- **Sweep:** waived - owner ruling\n"
            f"- **Fail-before:** waived - owner ruling\n- **Live check:** {head} pass\n")

    def merge(files):
        env, _ = with_fake_gh(prs={"95": {"headRefOid": head, "body": body, "files": files}})
        return run("gh pr merge 95 --squash", role="merger", extra_env={**env, **on})

    write_devcheck_record(head, rows=["viewport:1280x800", "lang:en", "always:keyboard-tab-enter"])
    verdict, reason = merge(ui)
    check("a UI change whose check skipped 320 px and Japanese is refused", verdict, "refuse")
    check("the refusal names the missing rows and how to declare or skip them",
          all(x in reason for x in ("viewport:320x640", "lang:ja", "# accept-rows:", "# accept-skip:")), True)
    check("and not the rows it ran", "`lang:en`" in reason, False)
    write_devcheck_record(head, rows=["viewport:1280x800", "viewport:320x640", "lang:en", "always:keyboard-tab-enter"],
                          skips={"lang:ja": "the change has no text"})
    check("every row run, or skipped with a reason: merged", merge(ui)[0], "allow")
    write_devcheck_record(head, rows=["always:keyboard-tab-enter"])
    check("a change to other product code needs only the always rows",
          merge([{"path": "src/data.json", "changeType": "MODIFIED"}])[0], "allow")
    write_devcheck_record(head)
    check("a record from before the checklist (no rows) is not held to it", merge(ui)[0], "allow")
    # Run 10b: 96 % of live-check records skipped the voice row as "no microphone".
    on["ACCEPT_CHECKLIST"] = json.dumps({**json.loads(CHECKLIST_ENV["ACCEPT_CHECKLIST"]), "no_skip": ["lang:ja"]})
    write_devcheck_record(head, rows=["viewport:1280x800", "viewport:320x640", "lang:en", "always:keyboard-tab-enter"],
                          skips={"lang:ja": "no Japanese keyboard in headless Chromium"})
    verdict, reason = merge(ui)
    check("a skip of a row the project lists in no_skip does not count, and the refusal says how to run it",
          (verdict, "`[accept] no_skip`" in reason, "project.md" in reason), ("refuse", True, True))
    on["ACCEPT_CHECKLIST"] = CHECKLIST_ENV["ACCEPT_CHECKLIST"]
    (TEST_DEVCHECK_RECORDS / f"{head}.json").unlink()


def test_devcheck_gate() -> None:
    """Gate 7: a change a user can see was used in the real environment before its PR."""
    head = "7" * 40
    on = {"POOL_DEVCHECK": "1", "DEVCHECK_PATHS": "src/"}
    src = [{"path": "src/app.js", "changeType": "MODIFIED"}]

    def body(live: str = "", issue: str = "Fixes #90") -> str:
        return (f"<!-- worker -->\n{issue}\n- **Sweep SHA:** {head}\n- **Sweep:** waived - owner ruling\n"
                f"- **Fail-before:** waived - owner ruling\n{live}")

    def merge(pr: dict, labels: dict | None = None, linked: dict | None = None, extra: dict | None = None):
        env, _ = with_fake_gh(labels=labels, linked=linked, prs={"95": {"headRefOid": head, **pr}})
        return run("gh pr merge 95 --squash", role="merger", extra_env={**env, **on, **(extra or {})})

    verdict, _ = merge({"body": body(), "files": src}, extra={"POOL_DEVCHECK": "0"})
    check("devcheck off: no live check asked", verdict, "allow")
    verdict, reason = merge({"body": body(), "files": src})
    check("a change under the watched paths without a live check is refused", verdict, "refuse")
    check("the refusal is Gate 7 and says how", ("Gate 7" in reason, "devcheck.py" in reason), (True, True))
    check("and does not read as a sweep problem (the supervisor would re-sweep)", "sweep" in reason.lower(), False)
    verdict, _ = merge({"body": body(), "files": [{"path": "docs/notes.md", "changeType": "MODIFIED"}]})
    check("a change outside the paths, for an unlabelled issue, needs none", verdict, "allow")
    verdict, _ = merge({"body": body("- **Live check:** waived - no visible change\n"), "files": src})
    check("a path-only change may waive it as invisible", verdict, "allow")

    labelled = {"90": ["P1", "owner-request"]}
    verdict, reason = merge({"body": body("- **Live check:** waived - no visible change\n")},
                            labels=labelled, linked={"95": ["90"]})
    check("an owner-request issue cannot be waived as invisible", verdict, "refuse")
    check("the refusal names the owner ruling", "owner ruling" in reason, True)
    verdict, _ = merge({"body": body("- **Live check:** waived - owner ruling\n")},
                       labels=labelled, linked={"95": ["90"]})
    check("the owner's ruling waives it", verdict, "allow")
    verdict, _ = merge({"body": body(issue="Refs #91"), "headRefName": "issue-91"},
                       labels={"91": ["needs:live"]})
    check("a needs:live issue found through its branch and Refs is in scope", verdict, "refuse")

    write_devcheck_record(head)
    verdict, _ = merge({"body": body(f"- **Live check:** {head[:12]} pass\n"), "files": src})
    check("a passing record at the head merges", verdict, "allow")
    verdict, _ = merge({"body": body(f"Live check: 73c1601 pass\n- **Live check:** {head} pass\n"), "files": src})
    check("a stale plain line above the current one: the last line is read (PR #75)", verdict, "allow")
    write_devcheck_record(head, status="fail")
    verdict, reason = merge({"body": body(f"- **Live check:** {head} pass\n"), "files": src})
    check("a failed check does not", (verdict, "failed" in reason), ("refuse", True))
    write_devcheck_record(head, steps=[{"name": "smoke", "rc": 0}])
    verdict, reason = merge({"body": body(f"- **Live check:** {head} pass\n"), "files": src})
    check("nor one that ran only the project's smoke", (verdict, "no script" in reason), ("refuse", True))
    (TEST_DEVCHECK_RECORDS / f"{head}.json").unlink()
    verdict, reason = merge({"body": body(f"- **Live check:** {head} pass\n"), "files": src})
    check("nor a line with no record behind it", (verdict, "no record" in reason), ("refuse", True))


def test_a_product_change_does_not_close_its_issue_on_merge() -> None:
    """With the acceptance check on, a PR that changes [accept] paths says Refs #N: its
    issue closes on the tester's accept-verified line, not on the merge."""
    head = "5" * 40
    body = (f"<!-- worker -->\nFixes #90\n- **Sweep SHA:** {head}\n- **Sweep:** waived - owner ruling\n"
            "- **Fail-before:** waived - owner ruling\n")

    def merge(files, extra=None, text=body):
        env, _ = with_fake_gh(prs={"95": {"headRefOid": head, "body": text, "files": files}}, linked={"95": ["90"]})
        return run("gh pr merge 95 --squash", role="merger",
                   extra_env={**env, "ACCEPT_PATHS": "src/", **(extra or {})})

    verdict, reason = merge([{"path": "src/data.json", "changeType": "MODIFIED"}])
    check("a product change that would close its issue is refused", (verdict, "Refs #90" in reason), ("refuse", True))
    check("docs only: closes on merge as before", merge([{"path": "docs/a.md", "changeType": "MODIFIED"}])[0], "allow")
    check("POOL_ACCEPT=0: as before", merge([{"path": "src/data.json", "changeType": "MODIFIED"}],
                                            {"POOL_ACCEPT": "0"})[0], "allow")
    env, _ = with_fake_gh(labels={"90": ["stage:accept", "P1"]},
                          comments={"90": ["<!-- tester -->\nlooks fine"]})
    verdict, reason = run("gh issue close 90", role="monitor", extra_env=env)
    check("an issue at stage:accept does not close without the tester's accept-verified line",
          (verdict, "accept-verified" in reason), ("refuse", True))
    env, _ = with_fake_gh(labels={"90": ["stage:accept"]}, comments={"90": [
        "<!-- tester -->\naccept-verified: script at 320 px"]})
    check("with it, it may", run("gh issue close 90", role="monitor", extra_env=env)[0], "allow")


def test_gate_4_follows_the_accept_mode() -> None:
    """[accept] mode: "gate" (Refs, close after acceptance), "after" (Fixes, checked after
    the close) or "off" (Fixes). With acceptance and live checks off, needs:live issues
    still sat at stage:owner because their PRs said Refs."""
    head = "6" * 40
    fixes = (f"<!-- worker -->\nFixes #90\n- **Sweep SHA:** {head}\n- **Sweep:** waived - owner ruling\n"
             "- **Fail-before:** waived - owner ruling\n")
    refs = fixes.replace("Fixes #90", "Refs #90")

    def merge(mode, text, files, linked, live="0", labels=None):
        env, _ = with_fake_gh(prs={"96": {"headRefOid": head, "body": text, "files": files, "headRefName": "issue-90"}},
                              linked={"96": linked}, labels=labels or {"90": ["P1"]})
        return run("gh pr merge 96 --squash", role="merger",
                   extra_env={**env, "ACCEPT_PATHS": "src/", "POOL_ACCEPT_MODE": mode, "POOL_LIVE": live})
    src = [{"path": "src/data.json", "changeType": "MODIFIED"}]
    check("gate: a product change says Refs", merge("gate", fixes, src, ["90"])[0], "refuse")
    check("after: a product change that closes its issue merges", merge("after", fixes, src, ["90"])[0], "allow")
    verdict, reason = merge("after", refs, src, [])
    check("after: a product change that does not close its issue is refused, asking for Fixes",
          (verdict, "Fixes #90" in reason), ("refuse", True))
    check("after: a docs change may say Refs", merge("after", refs, [{"path": "docs/a.md", "changeType": "MODIFIED"}],
                                                     [])[0], "allow")
    check("off: Fixes merges, and Refs is no one's business", (merge("off", fixes, src, ["90"])[0],
                                                               merge("off", refs, src, [])[0]), ("allow", "allow"))
    live_issue = {"90": ["P1", "needs:live"]}
    check("gate with live checks on: a needs:live issue is not closed by the merge",
          merge("gate", fixes, [{"path": "docs/a.md", "changeType": "MODIFIED"}], ["90"], "1", live_issue)[0], "refuse")
    check("after with live checks on: it is (the check comes after the close)",
          merge("after", fixes, src, ["90"], "1", live_issue)[0], "allow")
    env, _ = with_fake_gh()
    tree = make_clean_repo()
    (tree / "src").mkdir()
    (tree / "src" / "data.json").write_text("{}")
    subprocess.check_call(["git", "-C", str(tree), "add", "-A"])
    subprocess.check_call(["git", "-C", str(tree), "commit", "-qm", "change"], stdout=subprocess.DEVNULL)
    after = {**env, "ACCEPT_PATHS": "src/", "POOL_ACCEPT_MODE": "after"}
    verdict, reason = run(f"cd {tree} && gh pr create --title t --body '{refs}'", role="worker", extra_env=after)
    check("before the PR exists too: after mode, a product change with Refs is refused",
          (verdict, "Fixes #90" in reason), ("refuse", True))
    verdict, _ = run(f"cd {tree} && gh pr create --title t --body '{fixes}'", role="worker", extra_env=after)
    check("and with Fixes it opens", verdict, "allow")


def test_only_an_acceptance_chore_adds_tests_under_tests_accept() -> None:
    """When workers put tests of their own issues in tests/accept, it grew to most of the
    suite and the guard slowed every deploy."""
    head = "4" * 40
    body = (f"<!-- worker -->\nFixes #90\n- **Sweep SHA:** {head}\n- **Sweep:** waived - owner ruling\n"
            "- **Fail-before:** waived - owner ruling\n")

    def merge(files, bodies=None):
        env, _ = with_fake_gh(prs={"95": {"headRefOid": head, "body": body, "files": files}}, linked={"95": ["90"]},
                              bodies=bodies)
        return run("gh pr merge 95 --squash", role="merger", extra_env=env)

    added = [{"path": "tests/accept/fixture_90.json", "changeType": "ADDED"}]  # any file (Gate 1 reads .py)
    verdict, reason = merge(added)
    check("a worker's PR that adds a file under tests/accept/ is refused, and told where tests go",
          (verdict, "tests/e2e" in reason), ("refuse", True))
    check("the acceptance chore's PR may add them",
          merge(added, bodies={"90": "<!-- merger -->\n<!-- accept-chore -->\nThe tester verified #5"})[0], "allow")
    check("changing an existing guard test is not adding one",
          merge([{"path": "tests/accept/fixture_5.json", "changeType": "MODIFIED"}])[0], "allow")
    # A change to server-side history changed what an older guard assumed; its author replaces it.
    replaced = body + "- **Guard change:** tests/accept/fixture_90.json: #90 keeps history across a reload (R-HIST-1)\n"
    env, _ = with_fake_gh(prs={"95": {"headRefOid": head, "body": replaced, "files": added}}, linked={"95": ["90"]})
    check("a guard file the PR body names in a `Guard change:` line may be added",
          run("gh pr merge 95 --squash", role="merger", extra_env=env)[0], "allow")


def test_devcheck_survives_a_rebase_that_only_moves_the_branch() -> None:
    """Gate 7 accepts the check of an earlier head when the branch changes the same
    lines; a rebase that changed what it does needs a new check."""
    import importlib
    sys.path.insert(0, str(GUARD.parent))
    devcheck = importlib.import_module("devcheck")
    app = "".join(f"line {i}\n" for i in range(1, 30))
    base = make_commit({"src/app.js": app})
    main2 = make_commit({"src/app.js": app.replace("line 2\n", "line 2 (from main)\n")}, parent=base)
    old_head = make_commit({"src/app.js": app.replace("line 25\n", "line 25\nadded by the PR\n")}, parent=base)
    moved = make_commit({"src/app.js": app.replace("line 2\n", "line 2 (from main)\n")
                         .replace("line 25\n", "line 25\nadded by the PR\n")}, parent=main2)
    changed = make_commit({"src/app.js": app.replace("line 2\n", "line 2 (from main)\n")
                           .replace("line 25\n", "line 25\nadded differently\n")}, parent=main2)
    write_devcheck_record(old_head, key=devcheck.branch_key(os.getcwd(), base, old_head))
    on = {"POOL_DEVCHECK": "1", "DEVCHECK_PATHS": "src/"}

    def merge(head: str):
        b = (f"<!-- worker -->\nFixes #12\n- **Sweep SHA:** {head}\n- **Sweep:** waived - owner ruling\n"
             f"- **Fail-before:** waived - owner ruling\n- **Live check:** {old_head} pass\n")
        env, _ = with_fake_gh(prs={"96": {"headRefOid": head, "baseRefOid": main2, "body": b,
                                          "files": [{"path": "src/app.js", "changeType": "MODIFIED"}]}})
        return run("gh pr merge 96 --squash", role="merger", extra_env={**env, **on})

    check("the same change on a moved main keeps its live check", merge(moved)[0], "allow")
    verdict, reason = merge(changed)
    check("a branch that now changes something else does not", verdict, "refuse")
    check("and is told to check again with --rerun", "--rerun" in reason, True)
    (TEST_DEVCHECK_RECORDS / f"{old_head}.json").unlink()


def test_devcheck_at_pr_create() -> None:
    """The worker learns about Gate 7 when it opens the PR, not at the merge."""
    head = "8" * 40

    def create(live: str) -> tuple[str, str]:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("<!-- worker -->\nFixes #93\n- **Sweep SHA:** abc1234\n"
                     f"- **Fail-before:** tests/unit/test_app.py 7 -> 8\n{live}")
        env, _ = with_fake_gh(labels={"93": ["owner-request"]})
        try:
            return run(f"gh pr create --title t --body-file {fh.name}", role="worker",
                       extra_env={**env, **claim_env(93), "POOL_DEVCHECK": "1"})
        finally:
            pathlib.Path(fh.name).unlink(missing_ok=True)

    verdict, reason = create("")
    check("an owner-request PR without a live check is refused at create", verdict, "refuse")
    check("with the way to run it", "devcheck.py" in reason, True)
    write_devcheck_record(head)
    check("with a passing record it opens", create(f"- **Live check:** {head} pass\n")[0], "allow")
    write_devcheck_record(head, status="fail")
    check("with a failed one it does not", create(f"- **Live check:** {head} pass\n")[0], "refuse")
    (TEST_DEVCHECK_RECORDS / f"{head}.json").unlink()


def test_devcheck_at_pr_create_reads_the_worktree() -> None:
    """Run 8: `cd <worktree> && gh pr create` was diffed in the guard's own directory (for
    agy the shared checkout, on main), so four PRs changing src/ opened without a Live
    check line and were handed back at the merge."""
    with tempfile.TemporaryDirectory(prefix="rg-create-") as tmp:
        root = pathlib.Path(tmp)
        genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"}

        def git(*args: str, cwd: pathlib.Path = root / "clone") -> None:
            subprocess.run(["git", *args], cwd=cwd, env=genv, check=True, capture_output=True)

        git("init", "-q", "--bare", "-b", "main", str(root / "origin.git"), cwd=root)
        git("clone", "-q", str(root / "origin.git"), str(root / "clone"), cwd=root)
        git("checkout", "-q", "-b", "main")
        (root / "clone/README.md").write_text("x\n")
        git("add", "README.md")
        git("commit", "-qm", "base")
        git("push", "-q", "origin", "main")
        wt = root / "wt"
        git("worktree", "add", "-q", "-b", "issue-5", str(wt), "origin/main")
        (wt / "src").mkdir()
        (wt / "src/card.js").write_text("show()\n")
        git("add", "src/card.js", cwd=wt)
        git("commit", "-qm", "card", cwd=wt)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("<!-- worker -->\nFixes #5\n- **Sweep SHA:** abc1234\n"
                     "- **Fail-before:** tests/unit/test_app.py 7 -> 8\n")
        env, _ = with_fake_gh(labels={"5": ["P0"]})
        env = {**env, **claim_env(5), "POOL_DEVCHECK": "1", "DEVCHECK_PATHS": "src/", "SHARED_REPO_PATH": str(root / "clone")}
        try:
            verdict, reason = run(f"cd {wt} && git push -u origin HEAD:issue-5 && gh pr create --head issue-5 "
                                  f"--title t --body-file {fh.name}", role="worker", host=AGY, extra_env=env)
            check("agy, cd into the worktree: its change to src/ needs a live check", verdict, "refuse")
            check("and the refusal names the file", "src/card.js" in reason, True)
            verdict, _ = run(f'WT="{wt}"\ncd "$WT" || exit 1\ngh pr create --title t --body-file {fh.name}',
                             role="worker", host=AGY, extra_env=env)
            check("the worktree named by a variable set in the command", verdict, "refuse")
            git("push", "-q", "origin", "issue-5", cwd=wt)
            git("fetch", "-q", "origin")
            verdict, _ = run(f"gh pr create --head issue-5 --title t --body-file {fh.name}", role="worker",
                             host=AGY, extra_env=env)
            check("no worktree, --head: the branch on origin is read", verdict, "refuse")
            verdict, _ = run(f"gh pr create --title t --body-file {fh.name}", role="worker", host=AGY, extra_env=env)
            check("nothing to read the branch from: allowed, as before (the merge gate checks)", verdict, "allow")
        finally:
            pathlib.Path(fh.name).unlink(missing_ok=True)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all cases pass ({len(tests)} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
