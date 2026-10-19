#!/usr/bin/env python3
"""The `issuelooper` launcher, end to end in a scratch repository with a fake gh and a
fake agy on PATH: prerequisites, start, status, say, stop. Run: `python3 issuelooper_test.py`.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

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
        '[agents]\ncli = "agy"\n\n[env]\nPOOL_POLL_S = "1"\n'
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
        if not k.startswith(("CCROLE", "POOL_", "ISSUELOOPER", "SHARED_REPO"))
        and k != "CC_ROLE"
    }
    env.update(
        PATH=f"{bindir}{os.pathsep}{env['PATH']}",
        HOME=str(d),
        FAKE_LOG=str(d / "agy.log"),
        GH_REPO="o/r",
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


def test_launcher() -> None:
    d, repo, env = setup()
    (repo / "dirty.txt").write_text("x")
    r = run(repo, env, "start")
    check(
        "a dirty checkout is refused",
        (r.returncode, "uncommitted changes" in r.stderr),
        (1, True),
    )
    (repo / "dirty.txt").unlink()
    r = run(repo, dict(env, CCROLE_MONITOR_CLI="claude"), "start")
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
        "stop stops both",
        ("supervisor stopped" in r.stdout, "monitor stopped" in r.stdout),
        (True, True),
    )
    r = run(repo, env, "status")
    check(
        "and status says so",
        ("supervisor: not running" in r.stdout, "monitor: not running" in r.stdout),
        (True, True),
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
    print(f"all issuelooper cases pass ({len(tests)} groups, {CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
