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

The keys and the variables they set are in KEYS below; `[env]` passes any other
variable through as written.
"""

from __future__ import annotations

import os
import pathlib
import shlex
import sys

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
    ("sweep", "fast_command"): "POOL_FAST_CHECK_CMD",
    ("deploy", "command"): "POOL_DEPLOY_CMD",
    ("deploy", "timeout_s"): "POOL_DEPLOY_TIMEOUT_S",
    ("live", "enabled"): "POOL_LIVE",
    ("ux", "enabled"): "POOL_UX",
    ("ux", "gap_minutes"): "POOL_UX_GAP_MIN",
    ("ux", "idle_gap_minutes"): "POOL_UX_IDLE_GAP_MIN",
    ("spec", "enabled"): "POOL_SPEC",
    ("spec", "gap_minutes"): "POOL_SPEC_GAP_MIN",
    ("review", "enabled"): "REVIEW_GATE",
    ("review", "rules"): "REVIEW_RULES",
    ("review", "production_paths"): "REVIEW_PATHS",
    ("review", "max_rejects"): "REVIEW_MAX_REJECTS",
    ("review", "fixtures"): "REVIEW_FIXTURES",
    ("pool", "max_sessions"): "POOL_MAX_WORKERS",
    ("pool", "worker_timeout"): "POOL_TIMEOUT_WORKER",
    ("pool", "merger_timeout"): "POOL_TIMEOUT_MERGER",
    ("pool", "tester_timeout"): "POOL_TIMEOUT_TESTER",
    ("pool", "tester_port_base"): "TST_PORT",
    ("pool", "run_dir"): "ISSUELOOPER_RUN",
    ("guard", "secret_urls"): "GUARD_SECRET_URLS",
    ("guard", "protected_paths"): "GUARD_PROTECTED_PATHS",
}
# [agents.<role>] cli / model -> CCROLE_<ROLE>_CLI / CCROLE_MODEL_<ROLE>.
ROLE_KEYS = {"cli": "CCROLE_{ROLE}_CLI", "model": "CCROLE_MODEL_{ROLE}"}


def repo_root() -> pathlib.Path:
    """The shared checkout: $SHARED_REPO_PATH, else the repository holding this file."""
    shared = os.environ.get("SHARED_REPO_PATH", "").strip()
    return pathlib.Path(shared) if shared else HERE.parents[1]


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
    for table, body in cfg.items():
        if table == "env":
            for name, v in (body or {}).items():
                out[str(name)] = _text(v)
            continue
        if not isinstance(body, dict):
            unknown.append(table)
            continue
        for key, v in body.items():
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
    return out, unknown


def apply(root: pathlib.Path | None = None) -> dict[str, str]:
    """Set every variable the config names that the environment does not already have.
    Returns what it set. A missing or unreadable file sets nothing."""
    try:
        values, _ = env_from(load(root))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"issuelooper config: {exc}", file=sys.stderr)
        return {}
    set_now = {}
    for name, v in values.items():
        if name not in os.environ:
            os.environ[name] = v
            set_now[name] = v
    return set_now


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "env":
        values, _ = env_from(load())
        for name, v in values.items():
            print(f"export {name}={shlex.quote(os.environ.get(name, v))}")
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
