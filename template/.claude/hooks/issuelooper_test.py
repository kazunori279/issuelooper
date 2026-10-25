#!/usr/bin/env python3
"""The `issuelooper` launcher, end to end in a scratch repository with a fake gh and a
fake agy on PATH: prerequisites, start, status, say, stop, for the headless broker host and
for the herdr host (with a fake herdr). Run: `python3 issuelooper_test.py`.
"""

from __future__ import annotations

import atexit
import json
import os
import pathlib
import shutil
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
FAILURES: list[str] = []
CHECKS = 0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


FAKE_GH = r"""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    sys.exit(0)
if args[:2] == ["api", "graphql"]:
    print(json.dumps({"data": {"repository": {"open": {"nodes": []}, "closed": {"nodes": []},
                     "pullRequests": {"nodes": []}, "main": {"target": {"oid": "a" * 40}}}}}))
    sys.exit(0)
sys.exit(0)
"""

FAKE_AGY = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "role": os.environ.get("CC_ROLE")}) + "\n")
print(json.dumps({"event": "init", "conversation_id": "conv-1"}), flush=True)
for line in sys.stdin:
    text = json.loads(line)["message"]["content"]
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"said": text}) + "\n")
    print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok: " + text[:40]}}),
          flush=True)
"""


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def setup():
    d = pathlib.Path(tempfile.mkdtemp(prefix="issuelooper-test-"))
    repo = d / "repo"
    (repo / ".claude").mkdir(parents=True)
    shutil.copytree(
        HERE,
        repo / ".claude" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__", "*_test.py"),
    )
    (repo / ".issuelooper").mkdir()
    (repo / ".issuelooper" / "config.toml").write_text(
        '[agents]\ncli = "agy"\n\n[monitor]\nhost = "broker"\n\n[env]\nPOOL_POLL_S = "1"\n'
    )
    (repo / ".issuelooper" / "project.md").write_text("# Project\n")
    (repo / ".gitignore").write_text(".issuelooper/run/\n")
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", ".", cwd=repo)
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "-m",
        "base",
        cwd=repo,
    )
    bindir = d / "bin"
    bindir.mkdir()
    for name, body in (("gh", FAKE_GH), ("agy", FAKE_AGY)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CCROLE", "POOL_", "ISSUELOOPER", "SHARED_REPO", "HERDR_"))
        and k != "CC_ROLE"
    }
    env.update(
        PATH=f"{bindir}{os.pathsep}{env['PATH']}",
        HOME=str(d),
        FAKE_LOG=str(d / "agy.log"),
        GH_REPO="o/r",
        ISSUELOOPER_DASHBOARD_PORT=str(free_port()),
    )
    return d, repo, env


def run(repo, env, *args, timeout=120):
    return subprocess.run(
        [str(repo / ".claude" / "hooks" / "issuelooper"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_broker_host() -> None:
    d, repo, env = setup()
    (repo / "dirty.txt").write_text("x")
    r = run(repo, env, "start")
    check(
        "a dirty checkout is refused",
        (r.returncode, "uncommitted changes" in r.stderr),
        (1, True),
    )
    (repo / "dirty.txt").unlink()
    # Keep this check independent of whether the developer has Claude installed.
    missing_bin = d / "missing-bin"
    missing_bin.mkdir()
    for name in ("bash", "python3", "git", "dirname", "mkdir", "cat", "gh", "agy"):
        source = shutil.which(name, path=env["PATH"])
        if source:
            (missing_bin / name).symlink_to(source)
    r = run(repo, dict(env, PATH=str(missing_bin), CCROLE_MONITOR_CLI="claude"), "start")
    check(
        "a CLI that is not on PATH is named",
        (r.returncode, "'claude' is not on PATH (used by: monitor)" in r.stderr),
        (1, True),
    )
    r = run(repo, env, "start")
    try:
        check("start succeeds", (r.returncode, r.stderr), (0, ""))
        run_dir = repo / ".issuelooper" / "run"
        check(
            "pid files and logs in the run dir",
            (
                (run_dir / "pool.pid").exists(),
                (run_dir / "broker.pid").exists(),
                (run_dir / "logs").is_dir(),
            ),
            (True, True, True),
        )
        pool_pid = int((run_dir / "pool.pid").read_text())
        check("supervisor owns a detached session", os.getsid(pool_pid), pool_pid)
        log = []
        for _ in range(100):
            log = (
                [json.loads(x) for x in (d / "agy.log").read_text().splitlines()]
                if (d / "agy.log").exists()
                else []
            )
            if any("Pool update" in e.get("said", "") for e in log):
                break
            time.sleep(0.2)
        said = [e["said"] for e in log if "said" in e]
        check(
            "the monitor got the default kickoff first",
            said[:1],
            [
                "Use the issuelooper-shared skill, then work as the monitor per the issuelooper-monitor "
                "skill. Run its pass over the open queue now."
            ],
        )
        check(
            "then the supervisor's feed, with the built-in header",
            any(
                x.startswith(
                    "Pool update: the queue changed. Use the issuelooper-monitor skill"
                )
                for x in said
            ),
            True,
        )
        argv = next(e for e in log if "argv" in e)
        check(
            "the monitor runs as CC_ROLE=monitor with stream-json input",
            (argv["role"], "--input-format" in argv["argv"]),
            ("monitor", True),
        )
        check(
            "the run dir does not dirty the checkout",
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                capture_output=True,
                text=True,
            ).stdout,
            "",
        )
        port = (repo / ".issuelooper" / "run" / "dashboard.port").read_text().strip()
        import urllib.request
        page = ""
        for _ in range(50):
            try:
                page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
                break
            except OSError:
                time.sleep(0.2)
        check("start also serves the dashboard", ("<h1>issuelooper" in page, "Needs you" in page), (True, True))
        r = run(repo, env, "dashboard")
        check("`dashboard` prints its URL", r.stdout.strip(), f"http://127.0.0.1:{port}/")
        r = run(repo, env, "status")
        check(
            "status names both",
            ("supervisor: running" in r.stdout, "monitor: running on agy" in r.stdout),
            (True, True),
        )
        r = run(repo, env, "say", "hello there")
        check("say returns the monitor's answer", ("ok: hello there" in r.stdout, r.stderr), (True, ""))
        r = run(repo, env, "start")
        check(
            "a second start is refused",
            (r.returncode, "already running" in r.stderr),
            (1, True),
        )
    finally:
        r = run(repo, env, "stop")
    check(
        "stop stops all three",
        ("supervisor stopped" in r.stdout, "monitor stopped" in r.stdout, "dashboard stopped" in r.stdout),
        (True, True, True),
    )
    r = run(repo, env, "status")
    check(
        "and status says so",
        ("supervisor: not running" in r.stdout, "monitor: not running" in r.stdout),
        (True, True),
    )


# herdr, faked: a session with panes; `pane run` of ccrole.sh puts an idle agent in the
# pane; every prompt is recorded. State lives in $FAKE_HERDR_STATE.
FAKE_HERDR = r"""#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_HERDR_STATE"]
try:
    st = json.load(open(path))
except (OSError, ValueError):
    st = {"n": 0, "panes": {}, "prompts": [], "runs": [], "closed": []}
args = sys.argv[1:]
out = {}
def new_pane():
    st["n"] += 1
    p = f"w1:p{st['n']}"
    st["panes"][p] = {"agent": None}
    return p
if args[:2] == ["tab", "create"]:
    out = {"tab": {"tab_id": "w1:t9"}, "root_pane": {"pane_id": new_pane()}}
elif args[:2] == ["pane", "split"]:
    out = {"pane": {"pane_id": new_pane()}}
elif args[:2] == ["pane", "run"]:
    st["runs"].append([args[2], args[3]])
    if "ccrole.sh" in args[3]:
        st["panes"][args[2]]["agent"] = {"agent_status": "idle", "role": args[3].split()[-1]}
elif args[:2] == ["pane", "close"]:
    st["closed"].append(args[2])
    st["panes"].pop(args[2], None)
elif args[:2] == ["agent", "get"]:
    t = args[2]
    pane = st["panes"].get(t) or next((v for v in st["panes"].values() if (v.get("agent") or {}).get("name") == t), None)
    if not pane or not pane.get("agent"):
        sys.stderr.write(json.dumps({"error": {"message": "no agent"}}))
        sys.exit(1)
    out = {"agent": pane["agent"]}
elif args[:2] == ["agent", "rename"]:
    st["panes"][args[2]]["agent"]["name"] = args[3]
    out = {"agent": st["panes"][args[2]]["agent"]}
elif args[:2] == ["agent", "prompt"]:
    st["prompts"].append([args[2], args[3]])
elif args[:2] == ["agent", "read"]:
    json.dump(st, open(path, "w"))
    print("> ")
    sys.exit(0)
json.dump(st, open(path, "w"))
print(json.dumps({"result": out}))
"""


def test_herdr_host() -> None:
    d, repo, env = setup()
    cfg = repo / ".issuelooper" / "config.toml"
    cfg.write_text(cfg.read_text().replace('host = "broker"', 'host = "herdr"'))
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "herdr", cwd=repo)
    (d / "bin" / "herdr").write_text(FAKE_HERDR)
    (d / "bin" / "herdr").chmod(0o755)
    statefile = d / "herdr.json"
    env.update(FAKE_HERDR_STATE=str(statefile), POOL_HERDR_QUIET_S="0.1", POOL_MONITOR_MIN_GAP_S="0")

    r = run(repo, env, "start")
    check("outside herdr, start says how to open it",
          (r.returncode, "run `issuelooper start` from a herdr pane" in r.stderr), (1, True))

    henv = dict(env, HERDR_ENV="1", HERDR_SOCKET_PATH=str(d / "herdr.sock"), HERDR_SESSION="t",
                HERDR_WORKSPACE_ID="w1")
    r = run(repo, henv, "start")
    try:
        check("start succeeds inside herdr", (r.returncode, r.stderr), (0, ""))
        st = json.loads(statefile.read_text())
        rec = json.loads((repo / ".issuelooper" / "run" / "herdr.json").read_text())
        check("the run dir records the session and panes",
              (rec["socket"], rec["monitor_name"], rec["aides"]),
              (str(d / "herdr.sock"), "repo-monitor", ["w1:p2"]))
        roles = {p: (v.get("agent") or {}).get("role") for p, v in st["panes"].items()}
        check("monitor and aide started through ccrole.sh in their panes",
              (roles.get("w1:p1"), roles.get("w1:p2")), ("monitor", "aide"))
        check("the status pane runs watch", any(c.endswith(" watch") for _, c in st["runs"]), True)
        check("the monitor got the kickoff first",
              st["prompts"][0], ["repo-monitor", "Use the issuelooper-shared skill, then work as the monitor "
                                 "per the issuelooper-monitor skill. Run its pass over the open queue now."])
        for _ in range(100):
            st = json.loads(statefile.read_text())
            if any("Pool update" in t for _, t in st["prompts"]):
                break
            time.sleep(0.2)
        check("the supervisor's feed reaches the monitor's pane",
              [p for p, t in st["prompts"] if "Pool update" in t][:1], ["w1:p1"])
        r = run(repo, henv, "say", "hello there")
        st = json.loads(statefile.read_text())
        check("say prompts the monitor", (r.returncode, st["prompts"][-1]), (0, ["w1:p1", "hello there"]))
        r = run(repo, dict(env, HERDR_ENV=""), "status")
        check("status works from a plain terminal, through the recorded socket",
              ("supervisor: running" in r.stdout, "monitor: idle (repo-monitor" in r.stdout), (True, True))
        r = run(repo, henv, "aide")
        rec = json.loads((repo / ".issuelooper" / "run" / "herdr.json").read_text())
        check("aide opens a second aide", (r.returncode, "repo-aide2" in r.stdout, len(rec["aides"])), (0, True, 2))
        check("the checkout stays clean",
              subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout, "")
    finally:
        r = run(repo, henv, "stop", "--all")
    st = json.loads(statefile.read_text())
    check("stop --all stops the supervisor and closes every pane it opened",
          ("supervisor stopped" in r.stdout, sorted(st["closed"])),
          (True, ["w1:p1", "w1:p2", "w1:p3", "w1:p4"]))


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all issuelooper cases pass ({len(tests)} groups, {CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
