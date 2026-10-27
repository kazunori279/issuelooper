#!/usr/bin/env python3
"""Tests for config.py. Run: python3 config_test.py"""

import atexit
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import json

# Every temporary file this file and what it runs make goes under one directory,
# removed at exit: the suite left hundreds of directories in /tmp, and a run's /tmp
# ran out of inodes. Once per process: a test file another one imports keeps it.
if "IL_TEST_TMP" not in os.environ:
    os.environ["IL_TEST_TMP"] = os.environ["TMPDIR"] = tempfile.mkdtemp(prefix="il-test-")
    os.environ["ISSUELOOPER_TMP"] = os.path.join(os.environ["IL_TEST_TMP"], "scratch")
    atexit.register(shutil.rmtree, os.environ["IL_TEST_TMP"], True)
tempfile.tempdir = os.environ["IL_TEST_TMP"]

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
    check("named for the repository, in the temporary directory",
          (str(a).startswith(tempfile.gettempdir() + "/issuelooper-app-"), a != b), (True, True))
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

# The acceptance checklist: one JSON variable (its rows hold commas), read back as rows.
CHECKLIST = """
[accept]
paths = ["src/"]
batch = 4
viewports = ["1280x800", "320x640"]
languages = ["en", "ja"]
always = ["reload mid-task and after", "keyboard: Tab, Enter, Escape"]
live = ["voice and typed interruption"]
[accept.triggers]
"src/static/*" = ["viewports", "languages"]
"src/live.py" = ["live"]
"""
saved = dict(os.environ)
try:
    import tomllib
    values, unknown = config.env_from(tomllib.loads(CHECKLIST))
    check("accept keys", (values.get("ACCEPT_PATHS"), values.get("POOL_ACCEPT_BATCH"), unknown), ("src/", "4", []))
    os.environ.update(ACCEPT_CHECKLIST=values["ACCEPT_CHECKLIST"], ACCEPT_PATHS="src/")
    check("rows with ids, commas kept", [r[0] for r in config.checklist()["rows"]],
          ["viewport:1280x800", "viewport:320x640", "lang:en", "lang:ja", "always:reload-mid-task",
           "always:keyboard-tab-enter", "live:voice-and-typed"])
    check("a UI file needs the viewports, the languages and the always rows",
          [r[0] for r in config.required_rows(["src/static/js/app.js"])],
          ["viewport:1280x800", "viewport:320x640", "lang:en", "lang:ja", "always:reload-mid-task",
           "always:keyboard-tab-enter"])
    check("the live module its live rows; other product code the always rows; docs none",
          ([r[0] for r in config.required_rows(["src/live.py"]) if r[1] == "live"],
           [r[1] for r in config.required_rows(["src/db.py"])], config.required_rows(["docs/a.md"])),
          (["live:voice-and-typed"], ["always", "always"], []))
    check("the prompt text lists ids and triggers",
          ("always:keyboard-tab-enter" in config.checklist_text(), "src/live.py -> live" in config.checklist_text()),
          (True, True))
    raw = json.loads(values["ACCEPT_CHECKLIST"])
    raw["no_skip"] = ["live:voice-and-typed", "Keyboard: Tab, Enter, Escape", "languages"]
    os.environ["ACCEPT_CHECKLIST"] = json.dumps(raw)
    check("no_skip names rows by id, text or group", sorted(config.checklist()["no_skip"]),
          ["always:keyboard-tab-enter", "lang:en", "lang:ja", "live:voice-and-typed"])
    os.environ.pop("ACCEPT_CHECKLIST")
    check("no checklist: no rows, no text", (config.required_rows(["src/static/a.js"]), config.checklist_text()),
          ([], ""))
    # [sweep] link_files: run 11's env-dependent test was red where .env was linked, green elsewhere.
    import tempfile
    shared, tree = pathlib.Path(tempfile.mkdtemp()), pathlib.Path(tempfile.mkdtemp())
    (shared / ".env").write_text("KEY=1\n")
    os.environ["SHARED_REPO_PATH"] = str(shared)
    os.environ["SWEEP_LINK_FILES"] = ".env, missing.txt"
    check("link_files: linked into a tree that lacks them, a missing one skipped",
          (config.link_shared_files(tree), (tree / ".env").read_text()), ([".env"], "KEY=1\n"))
    check("not twice, and never into the shared checkout itself",
          (config.link_shared_files(tree), config.link_shared_files(shared)), ([], []))
    os.environ["SWEEP_LINK_FILES"] = ""
    check("off by default", config.link_shared_files(pathlib.Path(tempfile.mkdtemp())), [])
finally:
    os.environ.clear()
    os.environ.update(saved)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("all config cases pass")
