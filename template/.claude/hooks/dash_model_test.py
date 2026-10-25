#!/usr/bin/env python3
"""Tests for dash_model.py and dashboard.py on a fixture run directory.
Run: python3 dash_model_test.py"""

from __future__ import annotations

import atexit
import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

# Every temporary file this file and what it runs make goes under one directory,
# removed at exit: the suite left hundreds of directories in /tmp, and a run's /tmp
# ran out of inodes. Once per process: a test file another one imports keeps it.
if "IL_TEST_TMP" not in os.environ:
    os.environ["IL_TEST_TMP"] = os.environ["TMPDIR"] = tempfile.mkdtemp(prefix="il-test-")
    os.environ["ISSUELOOPER_TMP"] = os.path.join(os.environ["IL_TEST_TMP"], "scratch")
    atexit.register(shutil.rmtree, os.environ["IL_TEST_TMP"], True)
tempfile.tempdir = os.environ["IL_TEST_TMP"]

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import dash_model as dm  # noqa: E402
import dashboard  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0
NOW = 1_800_000_000.0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


def fixture() -> pathlib.Path:
    run = pathlib.Path(tempfile.mkdtemp(prefix="dash-test-"))
    pool = run / "pool"
    pool.mkdir(parents=True)
    t = NOW - 3000
    rec = [
        {"t": t, "kind": "supervisor_start"},
        {"t": t + 10, "kind": "queue", "open": 3, "untriaged": 1},
        {
            "t": t + 20,
            "kind": "start",
            "session": 1,
            "role": "worker",
            "unit": "wrk:4@2026-01-01T00:00:00Z",
        },
        {
            "t": t + 30,
            "kind": "start",
            "session": 2,
            "role": "worker",
            "unit": "wrk:5@x",
        },
        {
            "t": t + 600,
            "kind": "end",
            "session": 1,
            "role": "worker",
            "number": 4,
            "unit": "wrk:4@x",
            "outcome": "ok",
            "seconds": 580,
            "usage": {"input": 1000, "output": 100, "thinking": 50, "cost_usd": None},
        },
        {
            "t": t + 660,
            "kind": "handback",
            "pr": 9,
            "issue": 4,
            "by": "review",
            "reason": "invents a default city",
        },
        {
            "t": t + 661,
            "kind": "event",
            "text": "#4 was handed back from its pull request to stage:triage",
        },
        {
            "t": t + 700,
            "kind": "start",
            "session": 3,
            "role": "worker",
            "unit": "wrk:4@y",
        },
        {
            "t": t + 1500,
            "kind": "end",
            "session": 3,
            "role": "worker",
            "number": 4,
            "unit": "wrk:4@y",
            "outcome": "ok",
            "seconds": 800,
            "usage": {"input": 500, "output": 50, "cost_usd": 0.4},
        },
        {"t": t + 1600, "kind": "event", "text": "main moved aaaa -> bbbb (a merge)"},
        {"t": t + 1601, "kind": "event", "text": "#4 closed: Fix the count"},
        {
            "t": t + 1700,
            "kind": "end",
            "session": 2,
            "role": "worker",
            "number": 5,
            "unit": "wrk:5@x",
            "outcome": "timed out after 45m",
            "seconds": 2700,
        },
        {
            "t": t + 1710,
            "kind": "start",
            "session": 4,
            "role": "worker",
            "unit": "wrk:5@z",
        },
        {
            "t": t + 1800,
            "kind": "end",
            "session": 4,
            "role": "worker",
            "number": 5,
            "unit": "wrk:5@z",
            "outcome": "failed (exit 1)",
            "seconds": 90,
        },
        {"t": t + 1900, "kind": "deploy_failed", "sha": "bbbb"},
        {"t": NOW - 120, "kind": "poll_error", "error": "HTTP 502"},
        {
            "t": NOW - 60,
            "kind": "start",
            "session": 5,
            "role": "merger",
            "unit": "mrg:9@h@m",
        },
    ]
    (pool / "pool.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rec))
    sdir = pool / "0005-merger-mrg_9"
    sdir.mkdir()
    (sdir / "session.jsonl").write_text(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "state": "ACTIVE",
                    "tool_name": "run_command",
                    "tool_info": {
                        "parameters": {"CommandLine": "git rebase origin/main"}
                    },
                },
            }
        )
        + "\n"
    )
    (sdir / "session.log").write_text("line 1\nline 2\n")
    (sdir / "prompt.txt").write_text("Land PR #9")
    state = {
        "t": NOW - 5,
        "main": "b" * 40,
        "deployed": "",
        "running": {"mrg:9": "mrg:9@h@m"},
        "due_not_started": [],
        "stuck": ["wrk:7@q"],
        "paused": {},
        "queue": {
            "open": 2,
            "stages": {"stage:triage": 1, "stage:owner": 1},
            "owner": [{"number": 8, "title": "Which color?", "updated": "x"}],
            "prs": {"CONFLICTING": 1},
        },
        "sessions": [
            {
                "seq": 5,
                "role": "merger",
                "kind": "land",
                "number": 9,
                "unit": "mrg:9@h@m",
                "started": NOW - 60,
                "dir": str(sdir),
                "timeout_s": 2700,
                "cli": "agy",
            }
        ],
        "feed_pending": 2,
        "feed_pending_since": NOW - 1200,
        "main_check": {"sha": "b" * 40, "ok": False, "t": NOW - 30, "failing": ["tests/e2e/test_a.py::test_x"]},
        "missing_files": [".env"],
        "ux_scores": [{"t": NOW - 900, "sha": "a", "score": 38}, {"t": NOW - 600, "sha": "b", "score": None},
                      {"t": NOW - 300, "sha": "c", "score": 58}, {"t": NOW - 100, "sha": "d", "score": 78}],
    }
    (pool / "state.json").write_text(json.dumps(state))
    (run / "pool.pid").write_text(str(os.getpid()))
    review = run / "review"
    review.mkdir()
    (review / ("c" * 40 + ".json")).write_text(
        json.dumps(
            {
                "t": NOW - 100,
                "verdict": "reject",
                "usage": {"calls": 1, "input_tokens": 300, "output_tokens": 20},
            }
        )
    )
    os.environ.update(
        REVIEW_DIR=str(review), POOL_LOGDIR=str(pool), ISSUELOOPER_RUN=str(run)
    )
    return run


MON = {
    "host": "herdr",
    "state": "blocked",
    "name": "app-monitor",
    "pane": "w1:p1",
    "aides": [{"pane": "w1:p2", "state": "idle"}],
}


def test_model() -> None:
    run = fixture()
    m = dm.load(run, now=NOW, monitor=MON)
    check(
        "health",
        (
            m["health"]["supervisor"],
            round(m["health"]["last_poll_s"]),
            m["health"]["monitor"],
        ),
        (True, 5, "blocked"),
    )
    s = m["sessions"][0]
    check(
        "a running session: target, elapsed, current tool",
        (s["target"], round(s["elapsed_s"]), s["step"]),
        ("PR #9", 60, "run_command git rebase origin/main"),
    )
    p = m["progress"]["24h"]
    check(
        "merges, closes, hand-backs",
        (p["merges"], p["closed"], p["handed_back"]),
        (1, 1, 1),
    )
    check("outcomes", p["outcomes"], {"ok": 2, "timed out": 1, "failed": 1})
    check(
        "cycle time from the first worker start",
        p["cycle_min"]["median"],
        round((1601 - 20) / 60),
    )
    check(
        "tokens by role",
        p["tokens"]["worker"],
        {"input": 1500, "output": 200, "sessions": 2},
    )
    check(
        "cost only where the CLI reported one",
        (p["cost_usd"], p["cost_sessions_without_price"]),
        (0.4, 1),
    )
    check(
        "hand-back reasons",
        p["handback_reasons"],
        [("review: invents a default city", 1)],
    )
    check(
        "reviewer usage from its records",
        (p["reviewer"]["calls"], p["reviewer"]["verdicts"]),
        (1, {"reject": 1}),
    )
    whats = [a["what"] for a in m["attention"]]
    check(
        "attention covers each problem",
        [
            any("waiting at a question" in w for w in whats),
            any("Stuck: wrk:7@q" in w for w in whats),
            any("last deploy failed" in w for w in whats),
            any("#8 waits for your decision" in w for w in whats),
            any("#5 failed twice in a row (timed out, failed)" in w for w in whats),
            any("could not read GitHub" in w for w in whats),
            any("2 update(s) have waited" in w for w in whats),
            any("main is red at bbbbbbbb: tests/e2e/test_a.py::test_x" in w for w in whats),
            any("The shared checkout is missing .env" in w for w in whats),
        ],
        [True] * 9,
    )
    check(
        "red before yellow",
        [a["level"] for a in m["attention"]][:3],
        ["red", "red", "red"],
    )
    agents = dict(m["series"]["agents"])
    check(
        "agents over time: two workers at once early on",
        agents[round(NOW - 2700)],
        {"worker": 2},
    )
    check("recent, newest first, in words", m["recent"][0]["text"], "merger PR #9 started (session 5)")
    check("unit keys in words", [dm.unit_label(r) for r in (
        {"role": "worker", "unit": "wrk:7@2026"}, {"role": "auto", "unit": "merge:8@ab@cd"},
        {"role": "tester", "unit": "ux:abc:idle0"}, {"role": "tester", "unit": "tst:live12"})],
        ["worker #7", "auto-merge of PR #8", "tester UX run", "tester #12"])
    txt = dm.text(m)
    check(
        "terminal view",
        ("Needs attention:" in txt, "merger  PR #9" in txt, "Queue:" in txt, "main check RED bbbbbbbb" in txt),
        (True, True, True, True),
    )
    check("the recent UX scores", "UX score 38 → 58 → 78 (1 report(s) without a score)" in txt, True)


def test_empty_run_dir() -> None:
    run = pathlib.Path(tempfile.mkdtemp(prefix="dash-empty-"))
    os.environ.update(POOL_LOGDIR=str(run / "pool"), REVIEW_DIR=str(run / "none"))
    m = dm.load(run, now=NOW, monitor={"host": "", "state": "gone", "aides": []})
    check(
        "nothing yet: the supervisor is down, no crash",
        [a["what"] for a in m["attention"]],
        ["The supervisor is not running."],
    )
    check("and the page renders", "<h1>issuelooper" in dashboard.page(m), True)


def test_pages_and_server() -> None:
    run = fixture()
    m = dm.load(run, now=NOW, monitor=MON)
    html = dashboard.page(m)
    for part in (
        "Needs you",
        "Agents",
        "Queue",
        "Progress",
        "Over time",
        "Recent",
        "<svg",
        "/session/5",
    ):
        check(f"page has {part}", part in html, True)
    check("the Health line has main's last check", 'main check <b class="bad">red</b>' in html, True)
    check("Progress has the UX scores", "UX score 38 → 58 → 78" in html, True)
    check(
        "the cost note for a CLI with no price",
        "sessions on a CLI that reports no price" in html,
        True,
    )
    sp = dashboard.session_page(run, 5)
    check(
        "session page shows the log and the prompt",
        ("line 2" in sp, "Land PR #9" in sp),
        (True, True),
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", port), dashboard.make_handler(run))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/state.json", timeout=10
        ).read()
        data = json.loads(body)
        check(
            "the JSON endpoint", ("attention" in data, "sessions" in data), (True, True)
        )
        page = (
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10)
            .read()
            .decode()
        )
        check("the page over HTTP", "refreshes every 15 s" in page, True)
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all dash_model cases pass ({len(tests)} groups, {CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
