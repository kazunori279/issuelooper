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
    check("bool", values.get("POOL_LIVE"), "1")
    check("spec", (values.get("POOL_SPEC"), values.get("POOL_SPEC_GAP_MIN")), ("1", "30"))
    check("list", values.get("REVIEW_PATHS"), "src/,static/")
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

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("all config cases pass")
