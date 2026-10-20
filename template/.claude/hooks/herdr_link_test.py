#!/usr/bin/env python3
"""Tests for herdr_link.py with a fake herdr on PATH. Run: python3 herdr_link_test.py"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import herdr_link  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


# A pane's agent becomes ready a few `agent get` calls after `pane run`; every call is
# recorded with the environment herdr would use to find its session.
FAKE = r"""#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_STATE"]
st = json.load(open(path))
args = sys.argv[1:]
st["calls"].append({"args": args, "socket": os.environ.get("HERDR_SOCKET_PATH", "")})
out = {}
if args[:2] == ["pane", "run"]:
    st["ran"] = args[3]
elif args[:2] == ["agent", "get"]:
    st["gets"] = st.get("gets", 0) + 1
    if not st.get("ran") or st["gets"] < 3:
        json.dump(st, open(path, "w"))
        sys.stderr.write(json.dumps({"error": {"code": "agent_not_found", "message": "no agent in pane " + args[2]}}))
        sys.exit(1)
    out = {"agent": {"agent": "agy", "agent_status": "idle", "pane_id": args[2]}}
elif args[:2] == ["agent", "rename"]:
    out = {"agent": {"name": args[3]}}
elif args[:2] == ["tab", "create"]:
    out = {"tab": {"tab_id": "w1:t2"}, "root_pane": {"pane_id": "w1:p5"}}
elif args[:2] == ["pane", "split"]:
    out = {"pane": {"pane_id": "w1:p6"}}
elif args[:2] == ["agent", "read"]:
    json.dump(st, open(path, "w"))
    print("recent output")
    sys.exit(0)
json.dump(st, open(path, "w"))
print(json.dumps({"id": "x", "result": out}))
"""


def setup() -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="herdr-link-test-"))
    fake = d / "herdr"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    (d / "state.json").write_text(json.dumps({"calls": []}))
    os.environ.update(HERDR_BIN=str(fake), FAKE_STATE=str(d / "state.json"))
    return d


def calls(d: pathlib.Path) -> list[dict]:
    return json.loads((d / "state.json").read_text())["calls"]


def test_agent_names() -> None:
    check(
        "repo and role", herdr_link.agent_name("/x/my-app", "monitor"), "my-app-monitor"
    )
    check(
        "numbered aide", herdr_link.agent_name("/x/my-app", "aide", 2), "my-app-aide2"
    )
    check(
        "odd characters",
        herdr_link.agent_name("/x/My_App.v2", "aide"),
        "my-app-v2-aide",
    )
    check(
        "starts with a letter",
        herdr_link.agent_name("/x/2026-tool", "monitor"),
        "r2026-tool-monitor",
    )
    long = herdr_link.agent_name("/x/" + "a" * 60, "monitor", 12)
    check(
        "at most 32 characters",
        (len(long) <= 32, long.endswith("-monitor12")),
        (True, True),
    )


def test_start_role_waits_and_names() -> None:
    d = setup()
    herdr_link.time.sleep = lambda s: None
    a = herdr_link.start_role("monitor", "app-monitor", "w1:p5", timeout_s=30)
    cs = [c["args"] for c in calls(d)]
    check(
        "runs ccrole.sh with the role in the pane",
        (cs[0][:3], cs[0][3].endswith("ccrole.sh monitor")),
        (["pane", "run", "w1:p5"], True),
    )
    check(
        "polls until herdr sees the agent",
        sum(1 for c in cs if c[:2] == ["agent", "get"]),
        3,
    )
    check("then names it", cs[-1], ["agent", "rename", "w1:p5", "app-monitor"])
    check("returns the agent", a.get("agent"), "agy")


def test_errors_carry_herdrs_message() -> None:
    setup()
    try:
        herdr_link.call("agent", "get", "w9:p9")
        got = ""
    except herdr_link.HerdrError as exc:
        got = str(exc)
    check(
        "the JSON error's message, not the raw JSON",
        got,
        "herdr agent get: no agent in pane w9:p9",
    )
    check("status of a pane with no agent", herdr_link.status("w9:p9"), "gone")
    os.environ["HERDR_BIN"] = "/nonexistent/herdr"
    try:
        herdr_link.run("agent", "list")
        got = ""
    except herdr_link.HerdrError as exc:
        got = str(exc)
    check("a missing binary is a HerdrError", got, "/nonexistent/herdr is not on PATH")


def test_layout_calls() -> None:
    d = setup()
    tab, pane = herdr_link.new_tab("w1", "/repo", "issuelooper", {"CC_X": "1"})
    right = herdr_link.split(pane, "right", "/repo", {"CC_X": "1"})
    check("ids from herdr's answers", (tab, pane, right), ("w1:t2", "w1:p5", "w1:p6"))
    cs = [c["args"] for c in calls(d)]
    check(
        "tab in the given workspace, with env",
        ("--workspace" in cs[0] and "w1" in cs[0], "CC_X=1" in cs[0]),
        (True, True),
    )
    check("splits never take focus", "--no-focus" in cs[1], True)


def test_target_env_from_run_dir() -> None:
    d = setup()
    run = d / "run"
    run.mkdir()
    (run / "herdr.json").write_text(json.dumps({"socket": "/tmp/s/herdr.sock"}))
    saved = os.environ.pop("HERDR_SOCKET_PATH", None)
    os.environ["ISSUELOOPER_RUN"] = str(run)
    try:
        herdr_link.target_env()
        check(
            "outside herdr, the recorded session is used",
            os.environ.get("HERDR_SOCKET_PATH"),
            "/tmp/s/herdr.sock",
        )
        os.environ["HERDR_SOCKET_PATH"] = "/inside/herdr.sock"
        herdr_link.target_env()
        check(
            "inside herdr, the pane's own session wins",
            os.environ["HERDR_SOCKET_PATH"],
            "/inside/herdr.sock",
        )
    finally:
        os.environ.pop("ISSUELOOPER_RUN", None)
        os.environ.pop("HERDR_SOCKET_PATH", None)
        if saved is not None:
            os.environ["HERDR_SOCKET_PATH"] = saved


def test_monitor_link_quiet_check() -> None:
    states = iter(["idle", "idle"])
    snaps = iter(["> ", "> typed"])
    herdr_link.status, real_status = (lambda t: next(states)), herdr_link.status
    herdr_link.snapshot, real_snap = (lambda t: next(snaps)), herdr_link.snapshot
    try:
        m = herdr_link.Monitor("w1:p5", quiet_s=3, sleep=lambda s: None)
        check("a pane that changed while idle counts as busy", m.status()["busy"], True)
        states, snaps = iter(["working"]), iter([])
        check(
            "working is busy without a snapshot",
            m.status(),
            {"busy": True, "done": 1, "state": "working"},
        )
        states, snaps = iter(["done", "done"]), iter(["> ", "> "])
        check("done and quiet: ready", m.status()["busy"], False)
    finally:
        herdr_link.status, herdr_link.snapshot = real_status, real_snap


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all herdr_link cases pass ({len(tests)} groups, {CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
