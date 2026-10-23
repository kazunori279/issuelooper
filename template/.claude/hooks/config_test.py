#!/usr/bin/env python3
"""Tests for config.py. Run: python3 config_test.py"""

import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


SAMPLE = """
[agents]
cli = "claude"
[agents.tester]
model = "big-model"
[agents.monitor]
cli = "agy"
[sweep]
command = "make verify"
slots = 2
main_check = false
[live]
enabled = true
[devcheck]
enabled = true
paths = ["src/", "static/"]
labels = ["owner-request", "needs:live"]
[ux]
journeys = "ux/journeys.py"
[spec]
enabled = true
gap_minutes = 30
[review]
production_paths = ["src/", "static/"]
[env]
POOL_POLL_S = "30"
[pool]
nonsense = 1
"""

with tempfile.TemporaryDirectory() as d:
    root = pathlib.Path(d)
    (root / ".issuelooper").mkdir()
    (root / config.CONFIG_REL).write_text(SAMPLE)

    values, unknown = config.env_from(config.load(root))
    check("cli", values.get("CCROLE_CLI"), "claude")
    check("role model", values.get("CCROLE_MODEL_TESTER"), "big-model")
    check("role cli", values.get("CCROLE_MONITOR_CLI"), "agy")
    check("int", values.get("SWEEP_SLOTS"), "2")
    check("main check off", values.get("POOL_MAIN_CHECK"), "0")
    check("journeys", values.get("POOL_UX_JOURNEYS"), "ux/journeys.py")
    check("bool", values.get("POOL_LIVE"), "1")
    check("spec", (values.get("POOL_SPEC"), values.get("POOL_SPEC_GAP_MIN")), ("1", "30"))
    check("list", values.get("REVIEW_PATHS"), "src/,static/")
    check("devcheck", (values.get("POOL_DEVCHECK"), values.get("DEVCHECK_PATHS"), values.get("DEVCHECK_LABELS")),
          ("1", "src/,static/", "owner-request,needs:live"))
    check("env passthrough", values.get("POOL_POLL_S"), "30")
    check("unknown", unknown, ["pool.nonsense"])

    # The environment wins over the file.
    saved = dict(os.environ)
    try:
        os.environ["SWEEP_SLOTS"] = "9"
        os.environ.pop("SWEEP_COMMAND", None)
        set_now = config.apply(root)
        check("env wins", os.environ["SWEEP_SLOTS"], "9")
        check("file fills", os.environ.get("SWEEP_COMMAND"), "make verify")
        check("reports what it set", "SWEEP_SLOTS" in set_now, False)
    finally:
        os.environ.clear()
        os.environ.update(saved)

    # No file: nothing set, no error.
    check(
        "no file", config.env_from(config.load(pathlib.Path(d) / "nowhere")), ({}, [])
    )

    # The CLI prints shell-quoted exports.
    out = subprocess.run(
        [sys.executable, str(HERE / "config.py"), "env"],
        env={
            **{k: v for k, v in os.environ.items() if k not in config.KEYS.values()},
            "SHARED_REPO_PATH": d,
        },
        capture_output=True,
        text=True,
    ).stdout
    check("env cli", "export SWEEP_COMMAND='make verify'" in out, True)

# One scratch directory per repository (#5), and a tester port base picked by its path.
saved = dict(os.environ)
try:
    os.environ.pop("ISSUELOOPER_TMP", None)
    a = config.scratch_root(pathlib.Path("/srv/x/app"))
    b = config.scratch_root(pathlib.Path("/srv/y/app"))
    check("named for the repository, under /tmp", (str(a).startswith("/tmp/issuelooper-app-"), a != b), (True, True))
    check("the same path, the same directory", config.scratch_root(pathlib.Path("/srv/x/app")), a)
    ports = {config.tester_port_base(pathlib.Path(f"/srv/r{k}")) for k in range(50)}
    check("tester ports 8021-8099, varying by repository", (min(ports) >= 8021, max(ports) <= 8099, len(ports) > 10),
          (True, True, True))
    os.environ.pop("SWEEP_RECORD_DIR", None)
    os.environ["ISSUELOOPER_TMP"] = "/tmp/mine"
    check("ISSUELOOPER_TMP names it", str(config.scratch_root()), "/tmp/mine")
    check("records go under it; the old shared directory is read too",
          [str(d) for d in config.record_dirs("sweep")], ["/tmp/mine/sweep", "/tmp/issuelooper-sweep"])
    os.environ["SWEEP_RECORD_DIR"] = "/x/records"
    check("an explicit record directory is the only one", [str(d) for d in config.record_dirs("sweep")], ["/x/records"])
    os.environ.pop("ISSUELOOPER_TMP", None)
    with tempfile.TemporaryDirectory() as d:
        set_now = config.apply(pathlib.Path(d))
        check("apply() exports it, even with no config file",
              (set_now.get("ISSUELOOPER_TMP"), os.environ.get("ISSUELOOPER_TMP")),
              (str(config.scratch_root(pathlib.Path(d))),) * 2)
finally:
    os.environ.clear()
    os.environ.update(saved)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("all config cases pass")
