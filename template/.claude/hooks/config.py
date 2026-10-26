#!/usr/bin/env python3
"""issuelooper settings: `.issuelooper/config.toml`, under the environment.

Every script in this directory reads its settings from environment variables
(`POOL_*`, `SWEEP_*`, `REVIEW_*`, `CCROLE_*` ...). The config file is the place a
project writes them down once: `apply()` turns its keys into those variables and
sets each one only when the environment does not already have it, so an exported
variable always wins over the file.

    python3 config.py env        print `export NAME=value` lines for a shell
    python3 config.py get NAME   print one variable's value after the file is applied
    python3 config.py check      parse the file and list unknown keys
    python3 config.py tmp        print this repository's scratch directory

Scratch files (worktrees, sweep and review records, a tester's tree) live under one
directory per repository, $ISSUELOOPER_TMP, by default
/tmp/issuelooper-<repository directory name>-<8 hex digits of its path's hash> ($TMPDIR
instead of /tmp when it is set);
apply() and `env` export it. Fixed names such as /tmp/mrg-121-auto collided across
repositories and runs: a leftover from another loop failed one rebase eight times.

The keys and the variables they set are in KEYS below; `[env]` passes any other
variable through as written. The acceptance checklist (`[accept]` viewports, languages,
always, live and triggers) is one JSON variable, $ACCEPT_CHECKLIST, since its rows
contain commas; checklist() reads it back.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import pathlib
import re
import shlex
import sys
import tempfile

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_REL = ".issuelooper/config.toml"
ROLES = ("worker", "merger", "tester", "monitor", "reviewer", "aide")

# (table, key) -> environment variable. Booleans become "1"/"0", lists are joined
# with commas, everything else is str().
KEYS = {
    ("agents", "cli"): "CCROLE_CLI",
    ("agents", "model"): "CCROLE_MODEL",
    ("monitor", "host"): "POOL_MONITOR_HOST",
    ("dashboard", "enabled"): "ISSUELOOPER_DASHBOARD",
    ("dashboard", "port"): "ISSUELOOPER_DASHBOARD_PORT",
    ("sweep", "command"): "SWEEP_COMMAND",
    ("sweep", "slots"): "SWEEP_SLOTS",
    ("sweep", "record_dir"): "SWEEP_RECORD_DIR",
    ("sweep", "junit"): "SWEEP_JUNIT",
    ("sweep", "rerun"): "SWEEP_RERUN",
    ("sweep", "format"): "SWEEP_FORMAT",
    ("sweep", "affected"): "SWEEP_AFFECTED",
    ("sweep", "affected_map"): "SWEEP_AFFECTED_MAP",
    ("sweep", "affected_full"): "SWEEP_AFFECTED_FULL",
    ("sweep", "fast_command"): "POOL_FAST_CHECK_CMD",
    ("sweep", "main_check"): "POOL_MAIN_CHECK",
    ("deploy", "command"): "POOL_DEPLOY_CMD",
    ("deploy", "timeout_s"): "POOL_DEPLOY_TIMEOUT_S",
    ("live", "enabled"): "POOL_LIVE",
    ("live", "required_files"): "POOL_REQUIRED_FILES",
    ("devcheck", "enabled"): "POOL_DEVCHECK",
    ("devcheck", "start"): "DEVCHECK_START",
    ("devcheck", "ready_path"): "DEVCHECK_READY_PATH",
    ("devcheck", "command"): "DEVCHECK_COMMAND",
    ("devcheck", "script_runner"): "DEVCHECK_SCRIPT_RUNNER",
    ("devcheck", "slots"): "DEVCHECK_SLOTS",
    ("devcheck", "port_base"): "DEVCHECK_PORT_BASE",
    ("devcheck", "step_timeout_s"): "DEVCHECK_STEP_TIMEOUT_S",
    ("devcheck", "record_dir"): "DEVCHECK_RECORD_DIR",
    ("devcheck", "paths"): "DEVCHECK_PATHS",
    ("devcheck", "labels"): "DEVCHECK_LABELS",
    ("ux", "enabled"): "POOL_UX",
    ("ux", "gap_minutes"): "POOL_UX_GAP_MIN",
    ("ux", "idle_gap_minutes"): "POOL_UX_IDLE_GAP_MIN",
    ("ux", "journeys"): "POOL_UX_JOURNEYS",
    ("spec", "enabled"): "POOL_SPEC",
    ("spec", "gap_minutes"): "POOL_SPEC_GAP_MIN",
    ("spec", "id_pattern"): "POOL_MAP_ID_PATTERN",
    ("review", "enabled"): "REVIEW_GATE",
    ("review", "rules"): "REVIEW_RULES",
    ("review", "production_paths"): "REVIEW_PATHS",
    ("review", "max_rejects"): "REVIEW_MAX_REJECTS",
    ("review", "fixtures"): "REVIEW_FIXTURES",
    ("pool", "max_sessions"): "POOL_MAX_WORKERS",
    ("pool", "merge_train"): "POOL_MERGE_TRAIN",
    ("pool", "train_size"): "POOL_TRAIN_SIZE",
    ("pool", "worker_timeout"): "POOL_TIMEOUT_WORKER",
    ("pool", "merger_timeout"): "POOL_TIMEOUT_MERGER",
    ("pool", "tester_timeout"): "POOL_TIMEOUT_TESTER",
    ("pool", "tester_port_base"): "TST_PORT",
    ("pool", "run_dir"): "ISSUELOOPER_RUN",
    ("guard", "secret_urls"): "GUARD_SECRET_URLS",
    ("guard", "protected_paths"): "GUARD_PROTECTED_PATHS",
    ("accept", "enabled"): "POOL_ACCEPT",
    ("accept", "paths"): "ACCEPT_PATHS",
    ("accept", "batch"): "POOL_ACCEPT_BATCH",
    ("accept", "guard"): "ACCEPT_GUARD",
    ("accept", "guard_timeout_s"): "POOL_ACCEPT_GUARD_S",
    ("accept", "units"): "POOL_ACCEPT_UNITS",
}
# [accept] keys that make up the checklist, carried together in $ACCEPT_CHECKLIST.
CHECKLIST_KEYS = ("viewports", "languages", "always", "live", "triggers")
# Each row group and the prefix of its rows' ids.
CHECKLIST_GROUPS = (("viewports", "viewport"), ("languages", "lang"), ("always", "always"), ("live", "live"))
# [agents.<role>] cli / model -> CCROLE_<ROLE>_CLI / CCROLE_MODEL_<ROLE>.
ROLE_KEYS = {"cli": "CCROLE_{ROLE}_CLI", "model": "CCROLE_MODEL_{ROLE}"}


def repo_root() -> pathlib.Path:
    """The shared checkout: $SHARED_REPO_PATH, else the repository holding this file."""
    shared = os.environ.get("SHARED_REPO_PATH", "").strip()
    return pathlib.Path(shared) if shared else HERE.parents[1]


def _path_hash(root: pathlib.Path | None) -> tuple[str, int]:
    r = (root or repo_root()).resolve()
    return r.name, int(hashlib.sha1(str(r).encode()).hexdigest()[:8], 16)


def scratch_root(root: pathlib.Path | None = None) -> pathlib.Path:
    """This repository's scratch directory: $ISSUELOOPER_TMP, else one named for the
    repository's path in the temporary directory ($TMPDIR, normally /tmp; apply()
    exports the result, so every process of a loop agrees). Not created here."""
    v = os.environ.get("ISSUELOOPER_TMP", "").strip()
    if v:
        return pathlib.Path(v)
    name, h = _path_hash(root)
    return pathlib.Path(tempfile.gettempdir()) / f"issuelooper-{name}-{h:08x}"


def record_dirs(kind: str) -> list[pathlib.Path]:
    """Where the sweep ("sweep", $SWEEP_RECORD_DIR), review ("review", $REVIEW_DIR) or
    live check ("devcheck", $DEVCHECK_RECORD_DIR) records go: the first is written; readers also look in the rest, the one directory
    all repositories shared before scratch_root, so records made before an upgrade
    still count."""
    name = {"sweep": "SWEEP_RECORD_DIR", "review": "REVIEW_DIR", "devcheck": "DEVCHECK_RECORD_DIR"}[kind]
    if os.environ.get(name, "").strip():
        return [pathlib.Path(os.environ[name].strip())]
    return [scratch_root() / kind, pathlib.Path(f"/tmp/issuelooper-{kind}")]


def tester_port_base(root: pathlib.Path | None = None) -> int:
    """The first tester port when TST_PORT is not set: 8021-8099, picked by the
    repository's path, so two loops on one machine rarely start on the same port
    (testers step by POOL_TESTER_PORT_STEP, 100, from it)."""
    return 8021 + 2 * (_path_hash(root)[1] % 40)


def load(root: pathlib.Path | None = None) -> dict:
    """The parsed config file, or {} when there is none."""
    path = (root or repo_root()) / CONFIG_REL
    if not path.is_file():
        return {}
    if tomllib is None:
        raise RuntimeError(f"{path}: reading it needs Python 3.11 or later (tomllib)")
    with open(path, "rb") as f:
        return tomllib.load(f)


def _text(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return str(v)


def env_from(cfg: dict) -> tuple[dict[str, str], list[str]]:
    """The variables the config sets, and the keys it did not recognize."""
    out: dict[str, str] = {}
    unknown: list[str] = []
    checklist_raw: dict = {}
    for table, body in cfg.items():
        if table == "env":
            for name, v in (body or {}).items():
                out[str(name)] = _text(v)
            continue
        if not isinstance(body, dict):
            unknown.append(table)
            continue
        for key, v in body.items():
            if table == "accept" and key in CHECKLIST_KEYS:
                checklist_raw[key] = v
                continue
            if table == "agents" and isinstance(v, dict):
                if key not in ROLES:
                    unknown.append(f"agents.{key}")
                    continue
                for rk, rv in v.items():
                    if rk in ROLE_KEYS:
                        out[ROLE_KEYS[rk].format(ROLE=key.upper())] = _text(rv)
                    else:
                        unknown.append(f"agents.{key}.{rk}")
                continue
            name = KEYS.get((table, key))
            if name is None:
                unknown.append(f"{table}.{key}")
            else:
                out[name] = _text(v)
    if checklist_raw:
        out["ACCEPT_CHECKLIST"] = json.dumps(checklist_raw, ensure_ascii=False)
    return out, unknown


def path_matches(path: str, pattern: str) -> bool:
    """A repository path against a glob of [accept] (or a directory ending in /): `*`
    also crosses `/`, so `src/static/*` covers `src/static/js/app.js`."""
    pattern = pattern.strip()
    if not pattern:
        return False
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatchcase(path, pattern)


def accept_paths() -> list[str]:
    """[accept] paths: the product code whose changes get an acceptance check."""
    return [p.strip() for p in os.environ.get("ACCEPT_PATHS", "").split(",") if p.strip()]


def checklist() -> dict:
    """The acceptance checklist the worker's live check and the tester share:
    {"rows": [(id, group, text)], "triggers": [(glob, [groups])]}. A row's id is its
    group's prefix and its value (`viewport:320x640`, `lang:ja`) or the first three
    words of its text (`always:reload-mid-task-and`): what a script declares."""
    try:
        raw = json.loads(os.environ.get("ACCEPT_CHECKLIST") or "{}")
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for group, prefix in CHECKLIST_GROUPS:
        items = raw.get(group) or []
        for text in items if isinstance(items, list) else [items]:
            text = str(text).strip()
            if not text:
                continue
            slug = text if group in ("viewports", "languages") else "-".join(re.findall(r"[a-z0-9]+", text.lower())[:3])
            rid, k = f"{prefix}:{slug}", 2
            while rid in seen:
                rid, k = f"{prefix}:{slug}-{k}", k + 1
            seen.add(rid)
            rows.append((rid, group, text))
    trig = raw.get("triggers") or {}
    triggers = [(str(g), [str(x) for x in (v if isinstance(v, list) else [v])])
                for g, v in (trig.items() if isinstance(trig, dict) else [])]
    return {"rows": rows, "triggers": triggers}


def required_rows(paths) -> list[tuple[str, str, str]]:
    """The checklist rows a change to these paths needs: the groups its paths trigger,
    and the `always` rows once it touches a trigger or [accept] paths."""
    cl, groups, product = checklist(), set(), accept_paths()
    for p in paths:
        hit = False
        for glob, gs in cl["triggers"]:
            if path_matches(p, glob):
                groups.update(gs)
                hit = True
        if hit or any(path_matches(p, q) for q in product):
            groups.add("always")
    return [r for r in cl["rows"] if r[1] in groups]


def checklist_text() -> str:
    """The checklist as the worker's and the tester's prompts show it ('' without one)."""
    cl = checklist()
    if not cl["rows"]:
        return ""
    lines = [f"    {rid:<28} {text}" for rid, _, text in cl["rows"]]
    text = "Checklist rows (id, then what to check):\n" + "\n".join(lines)
    if cl["triggers"]:
        text += ("\nA change to these paths needs these groups, and the always rows:\n"
                 + "\n".join(f"    {g} -> {', '.join(gs)}" for g, gs in cl["triggers"]))
    if accept_paths():
        text += f"\nA change to {', '.join(accept_paths())} needs the always rows."
    return text


def apply(root: pathlib.Path | None = None) -> dict[str, str]:
    """Set every variable the config names that the environment does not already have.
    Returns what it set. A missing or unreadable file sets nothing but ISSUELOOPER_TMP."""
    try:
        values, _ = env_from(load(root))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"issuelooper config: {exc}", file=sys.stderr)
        values = {}
    set_now = {}
    for name, v in values.items():
        if name not in os.environ:
            os.environ[name] = v
            set_now[name] = v
    if not os.environ.get("ISSUELOOPER_TMP", "").strip():
        os.environ["ISSUELOOPER_TMP"] = set_now["ISSUELOOPER_TMP"] = str(scratch_root(root))
    return set_now


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "env":
        values, _ = env_from(load())
        for name, v in values.items():
            print(f"export {name}={shlex.quote(os.environ.get(name, v))}")
        if "ISSUELOOPER_TMP" not in values:
            print(f"export ISSUELOOPER_TMP={shlex.quote(str(scratch_root()))}")
        return 0
    if cmd == "tmp":
        print(scratch_root())
        return 0
    if cmd == "get" and len(argv) == 3:
        apply()
        print(os.environ.get(argv[2], ""))
        return 0
    if cmd == "check":
        path = repo_root() / CONFIG_REL
        values, unknown = env_from(load())
        print(f"{path}: {'found' if path.is_file() else 'missing'}")
        for name, v in values.items():
            print(f"  {name}={v}")
        for k in unknown:
            print(f"  unknown key: {k}")
        return 1 if unknown else 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
