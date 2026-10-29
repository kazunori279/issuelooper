# Configuration and CLI Reference

Everything project-specific in an `issuelooper` repository lives under `.issuelooper/`. The commented template copied by `install.sh` is [`template/.issuelooper/config.toml`](../template/.issuelooper/config.toml), and the two samples ([`hello-world`](../examples/hello-world/issuelooper/) and [`live-ai-concierge`](../examples/live-ai-concierge/issuelooper/)) show complete configurations for a small CLI and a full web/voice application.

---

## 1. Installing and upgrading (`install.sh`)

```bash
./install.sh <repo-dir> [--upgrade] [--no-labels] [--no-commit] [--force] [--chain-hooks|--keep-hooks]
```

`install.sh` copies the framework into `<repo-dir>`, creates the required GitHub labels, sets `core.hooksPath` to `.githooks`, and stages only the files it manages:

- **Safe file handling:** other files under `.claude/`, `.agents/`, `.githooks/`, and `.issuelooper/` (tracked or untracked) are left untouched. Untracked `CLAUDE.md` or `.gitignore` files that `install.sh` appends to are left unstaged. If a target path already holds a file that did not come from an earlier install, `install.sh` lists the conflict and exits without writing; pass `--force` to overwrite.
- **Upgrading (`--upgrade`):** replaces the framework hooks and skills under `.claude/` and `.agents/` while keeping your existing `.issuelooper/` configuration files.
- **Existing git hooks:** `.githooks/pre-commit` and `.githooks/pre-push` prevent agent sessions from editing `.claude/` or `.issuelooper/` (unless `ISSUELOOPER_CONFIG_OK=1` is set in the owner's commit command) and block pushes that would empty an issue branch or force-push `main`. If your repository already uses git hooks (such as Husky or `.git/hooks`), `install.sh` stops and names them:
  - `--chain-hooks` sets `core.hooksPath` to `.githooks` and records the previous hook directory in `git config issuelooper.chainedHooksPath` so `.githooks` runs your existing hooks after its own checks (other clones should run the same command or set that git config key).
  - `--keep-hooks` leaves `core.hooksPath` unchanged so you can invoke `.githooks/pre-commit` and `.githooks/pre-push` from your own hooks.

---

## 2. The four files in `.issuelooper/`

| File | Purpose | How to write a good one |
|---|---|---|
| **`config.toml`** | All supervisor, sweep, review, acceptance, and agent settings. Each key maps to an environment variable; an exported environment variable always wins over `config.toml`. | Start with `[agents]`, `[sweep] command`, and `[pool] max_sessions`. Run `python3 .claude/hooks/config.py check` to verify syntax and detect unknown keys. |
| **`project.md`** | Concise repository facts that every agent session reads after `issuelooper-shared`. | Keep it to concrete commands and paths, not long prose: how to initialize a fresh `git worktree` (for example `uv sync --frozen -q`), where unit vs browser tests live (`tests/<suite>/test_issue_<N>.py`), how to run one test file, how to start the app on a given port with databases/logs under `$ISSUELOOPER_TMP`, how to link `.env` using full paths (`ln -sf "$SHARED_REPO_PATH/.env" "$WT/.env"`), and which shared files need small hunks. |
| **`review_rules.md`** | Project-specific rules sent to the LLM diff reviewer (`review_gate.py`) alongside its two built-in checks (no fabricated user-visible data, no test-gaming logic). | State plain, testable rules with what to reject and what to do instead, plus 1–2 short reject/pass code examples. Document legitimate narrow exceptions (such as a demo mode). HTML comments are stripped before sending. Add an optional ```` ```prescan ```` block with regexes (one per line) to flag matching added lines as hints for the reviewer. |
| **`monitor_kickoff.md`** | *(Optional)* The initial prompt sent to the monitor when `issuelooper start` launches. | Use it to state initial milestone ordering, seed-backlog priorities, or policies the monitor should follow when triaging. If absent, the monitor runs a standard triage pass over the open queue. |

---

## 3. `config.toml` reference

Every key below sets the corresponding environment variable unless that variable is already exported in the shell.

### `[agents]`, `[agents.<role>]`, and `[agents.quota_groups]`

Configures which agent CLI (`"agy"` or `"claude"`) and model run each role (`worker`, `merger`, `tester`, `monitor`, `reviewer`, `aide`). Most token usage comes from workers; assigning a stronger reasoning model to judgment roles (`monitor`, `tester`, `reviewer`) costs a small fraction of running every role on that model.

| Key | Env var | Default | When to change |
|---|---|---|---|
| `cli` | `CCROLE_CLI` | `"agy"` | Set to `"claude"` to run Claude Code as the default runtime. |
| `model` | `CCROLE_MODEL` | `""` (CLI default) | Pin a specific default model across all roles. |
| `[agents.<role>] cli` | `CCROLE_<ROLE>_CLI` | inherits `cli` | Override the CLI for one role (`worker`, `merger`, `tester`, `monitor`, `reviewer`, `aide`). |
| `[agents.<role>] model` | `CCROLE_MODEL_<ROLE>` | inherits `model` | Use a faster model for `worker` and a stronger model for `monitor`, `tester`, or `reviewer`. |
| `[agents.quota_groups]` | `POOL_QUOTA_GROUPS` | per `<cli>/<model>` | Group model globs that share a rate-limit pool (for example `vertex-gemini = ["gemini-3.8-*", "gemini-4-*"]`) so a quota error on one pauses new sessions across the group. |

### `[monitor]` and `[dashboard]`

| Key | Env var | Default | Description |
|---|---|---|---|
| `[monitor] host` | `POOL_MONITOR_HOST` | `"herdr"` | `"herdr"` runs interactive monitor and aide panes in a `herdr` tab; `"broker"` runs a headless monitor reached via `issuelooper say` (for SSH servers or CI). |
| `[dashboard] enabled` | `ISSUELOOPER_DASHBOARD` | `true` | Serve the local web dashboard on `127.0.0.1` when `issuelooper start` runs. |
| `[dashboard] port` | `ISSUELOOPER_DASHBOARD_PORT` | `8780` | First port tried on `127.0.0.1` (falls back to the next free port up to `+9`). |

### `[sweep]`

Controls how worktrees run the test suite and how `main` is verified.

| Key | Env var | Default | Description |
|---|---|---|---|
| `command` | `SWEEP_COMMAND` | `"make verify"` | Full check suite command executed inside a worktree. |
| `slots` | `SWEEP_SLOTS` | `4` | Maximum concurrent sweeps across the machine (`main_check` uses its own separate lane). |
| `affected` | `SWEEP_AFFECTED` | `""` (always full) | When the merge train is on, PR branch sweeps run this command with `{tests}` replaced by touched test files (for example `"make lint && uv run pytest -q tests/unit {tests}"`). |
| `affected_map` | `SWEEP_AFFECTED_MAP` | `""` | Path to a file of `<glob>: <test file> ...` lines adding explicit path-to-test mappings. |
| `affected_full` | `SWEEP_AFFECTED_FULL` | `""` | Additional globs that force a full sweep (built-in defaults include `conftest.py`, `pyproject.toml`, lockfiles, `Makefile`, `package.json`, fixtures, `.issuelooper/`, `.claude/`). |
| `affected_exclude` | `SWEEP_AFFECTED_EXCLUDE` | `[]` | Test directories excluded from affected sweeps unless edited directly (set to `["tests/accept/"]` when the merge train runs the acceptance guard). |
| `affected_browser_cap` | `SWEEP_AFFECTED_BROWSER_CAP` | `10` | UI file edits (`*.js`, `*.html`, `*.css`) select all browser tests; if there are more than this many browser test files, the full sweep runs instead. |
| `format` | `SWEEP_FORMAT` | `""` | Formatter command (such as `"uv run ruff format src tests"`) run at the start of a worker or merger sweep on a clean tree; if it modifies files, the sweep stops so the agent commits them. |
| `rerun` | `SWEEP_RERUN` | `""` | Command with `{tests}` (pytest node IDs) used to rerun only failing tests during flake checks and re-sweeps (for example `"uv run pytest -q {tests}"`). |
| `link_files` | `SWEEP_LINK_FILES` | `[]` | Git-ignored files symlinked from the shared checkout into every sweep, quick-check, rerun, or guard worktree (such as `[".env"]`). |
| `env_id` | `SWEEP_ENV_ID` | `""` | Optional non-secret environment identifier written into sweep records; records are reused only when `env_id` matches. |
| `junit` | `SWEEP_JUNIT` | `false` | Write JUnit XML per pytest run alongside `$ISSUELOOPER_TMP/sweep/runs.jsonl` (used to measure per-directory test runtime for consolidation chores). |
| `fast_command` | `POOL_FAST_CHECK_CMD` | `""` | Quick check (timeout `POOL_FAST_CHECK_S` = 120 s) run on a PR merged with `main` before landing when `merge_train = false`. |
| `main_check` | `POOL_MAIN_CHECK` | `true` | Run the full suite on `main` in the background after `main` moves (when not already checked by a merge train); files a `P0` `main is red` issue on failure. |
| `record_dir` | `SWEEP_RECORD_DIR` | `$ISSUELOOPER_TMP/sweep` | Directory storing sweep records and `runs.jsonl`. |

### `[accept]` and `[accept.triggers]`

Configures post-merge acceptance (`"gate"`, `"after"`, or `"off"`), the shared worker/tester checklist, and `tests/accept/` regression guards.

| Key | Env var | Default | Description |
|---|---|---|---|
| `enabled` | `POOL_ACCEPT` | `true` | Enable acceptance checks when `paths` is non-empty. |
| `mode` | `POOL_ACCEPT_MODE` | `"gate"` | `"gate"` (PR says `Refs #N`; issue stays at `stage:accept` until verified), `"after"` (PR says `Fixes #N`; merge closes issue and independent testers verify on deployed build, reopening failures), or `"off"`. |
| `paths` | `ACCEPT_PATHS` | `[]` | Product code prefixes (such as `["src/"]`) that require acceptance. Empty means only `needs:live` issues get a post-merge live check. |
| `batch` | `POOL_ACCEPT_BATCH` | `6` | Maximum issues checked by a single acceptance tester unit. |
| `units` | `POOL_ACCEPT_UNITS` | `2` | Maximum concurrent acceptance tester units (a second starts only once a full batch is waiting). |
| `guard` | `ACCEPT_GUARD` | `""` | Command that runs regression guard tests (such as `"uv run pytest -q tests/accept"`) on each deploy and in the merge train with no model call. |
| `guard_timeout_s` | `POOL_ACCEPT_GUARD_S` | `1200` | Timeout in seconds for `guard`. |
| `author` | `POOL_AUTHOR_ACCEPT` | `false` | (`"gate"` mode only) Resume the author's conversation to accept its own issue on the deployed build, checked by independent testers. |
| `sample` | `POOL_ACCEPT_SAMPLE` | `4` | In `"after"` mode (or `"gate"` with `author = true`), independent testers check all `sample_labels` issues (plus `P1` in `"after"` mode) and `1` in `sample` of the rest (`0` checks only labeled/P1 issues). |
| `sample_labels` | `POOL_ACCEPT_SAMPLE_LABELS` | `["owner-request", "P0", "needs:live", "seed", "type:feature-request"]` | Labels (plus any `type:feature*` label) that are always checked by an independent tester. |
| `escalate_at` | `POOL_ACCEPT_ESCALATE_AT` | `0.05` | When more than this fraction of the last `window` samples fail, the sampling frequency doubles. |
| `window` | `POOL_ACCEPT_WINDOW` | `20` | Rolling sample window size for `escalate_at`. |
| `min_samples` | `POOL_ACCEPT_MIN_SAMPLES` | `3` | Minimum samples in `window` before escalation can trigger. |
| `neighbourhood_min` | `POOL_ACCEPT_NEIGHBOURHOOD_MIN` | `10` | Minutes an author acceptance session spends testing nearby behavior after a pass, filing up to 3 issues (`0` disables). |
| `author_wait_s` | `POOL_AUTHOR_WAIT_S` | `600` | Seconds `"gate"` mode with `author = true` waits for an author session before handing acceptance to a tester unit. |
| `chore_priority` | `POOL_ACCEPT_CHORE_PRIORITY` | `"P2"` | Priority given to the chore issue that converts verified scripts into `tests/accept/`. |
| `chore_batch` | `POOL_ACCEPT_CHORE_BATCH` | `5` | Number of verified scripts batched into one acceptance-test chore. |
| `chore_every_s` | `POOL_ACCEPT_CHORE_EVERY_S` | `3600` | Maximum seconds the oldest verified script waits before a chore is filed. |
| `chore_append` | `POOL_ACCEPT_CHORE_APPEND` | `true` | Append newly verified scripts to an existing unclaimed open chore instead of filing a second chore. |
| `viewports`, `languages`, `always`, `live`, `no_skip`, `[accept.triggers]` | `ACCEPT_CHECKLIST` | empty | Shared checklist rows (`viewport:<size>`, `lang:<code>`, `always:<first-3-words>`, `live:<first-3-words>`), unskippable row IDs/groups (`no_skip`), and path-glob triggers mapping changed files to required row groups. |

### `[deploy]`, `[live]`, and `[devcheck]`

| Key | Env var | Default | Description |
|---|---|---|---|
| `[deploy] command` | `POOL_DEPLOY_CMD` | `""` | Command run from the shared checkout whenever `main` advances. |
| `[deploy] timeout_s` | `POOL_DEPLOY_TIMEOUT_S` | `300` | Timeout in seconds for the deploy command. |
| `[live] enabled` | `POOL_LIVE` | `false` | Require post-merge live checks on `needs:live` issues. |
| `[live] required_files` | `POOL_REQUIRED_FILES` | `[".env"]` (when live is on) | Files that must exist in the shared checkout before any live check starts; missing files are flagged on the dashboard and to the monitor. |
| `[devcheck] enabled` | `POOL_DEVCHECK` | `false` | Require workers to run `.claude/hooks/devcheck.py` against real services before opening a PR (enforced by Gate 7). |
| `[devcheck] start` | `DEVCHECK_START` | `""` | Foreground command starting the app on `$DEVCHECK_PORT` with scratch data in `$ISSUELOOPER_TMP`. |
| `[devcheck] ready_path` | `DEVCHECK_READY_PATH` | `"/"` | HTTP path polled until it responds with status `< 500`. |
| `[devcheck] command` | `DEVCHECK_COMMAND` | `""` | End-to-end smoke command run against `$DEVCHECK_URL` before the worker's own replay scripts (keep this script under `[guard] protected_paths`). |
| `[devcheck] script_runner` | `DEVCHECK_SCRIPT_RUNNER` | `"python3"` | Runner used for worker replay scripts (for example `"uv run python"`). |
| `[devcheck] slots` | `DEVCHECK_SLOTS` | `2` | Maximum concurrent `devcheck` runs. |
| `[devcheck] port_base` | `DEVCHECK_PORT_BASE` | `8700` | Base port for `devcheck` slots (`port_base + slot`). |
| `[devcheck] step_timeout_s` | `DEVCHECK_STEP_TIMEOUT_S` | `180` | Timeout in seconds for each `devcheck` step. |
| `[devcheck] paths` | `DEVCHECK_PATHS` | `["src/"]` | Path prefixes whose PRs require a passing `devcheck` record. |
| `[devcheck] labels` | `DEVCHECK_LABELS` | `["owner-request", "needs:live", "ux:accepted"]` | Issue labels whose PRs require a passing `devcheck` record. |
| `[devcheck] record_dir` | `DEVCHECK_RECORD_DIR` | `$ISSUELOOPER_TMP/devcheck` | Directory storing `devcheck` records. |

### `[ux]`, `[spec]`, and `[explore]`

| Key | Env var | Default | Description |
|---|---|---|---|
| `[ux] enabled` | `POOL_UX` | `false` | Run periodic tester UX journeys across the deployed app. |
| `[ux] gap_minutes` | `POOL_UX_GAP_MIN` | `25` | Minimum minutes between scheduled UX runs. |
| `[ux] idle_gap_minutes` | `POOL_UX_IDLE_GAP_MIN` | `10` | Minimum minutes between idle-time UX runs when `[explore]` is off. |
| `[ux] journeys` | `POOL_UX_JOURNEYS` | `""` | Path to a repository journeys script (such as `"ux/journeys.py"`); when empty, the tester maintains its own script in `$ISSUELOOPER_TMP/ux/`. |
| `[spec] enabled` | `POOL_SPEC` | `false` | Run spec compliance checks when idle or half of `max_sessions` is free. |
| `[spec] gap_minutes` | `POOL_SPEC_GAP_MIN` | `45` | Minimum minutes between spec checks. |
| `[spec] id_pattern` | `POOL_MAP_ID_PATTERN` | `""` | Regex matching spec requirement IDs (such as `'\bREQ-\d+\b'`), used to map matching files into worker prompts. |
| `[explore] enabled` | `POOL_EXPLORE` | `false` | Use free agent slots to run focused explorers over the deployed app (replaces idle-time UX and spec runs). |
| `[explore] focuses` | `POOL_EXPLORE_FOCUSES` | `[]` | List of focus descriptions rotated across explorer sessions. |
| `[explore] max` | `POOL_EXPLORE_MAX` | `2` | Maximum concurrent explorer sessions. |
| `[explore] per_hour` | `POOL_EXPLORE_PER_HOUR` | `4` | Maximum explorer sessions started per hour. |
| `[explore] issues` | `POOL_EXPLORE_ISSUES` | `1` | Maximum findings filed per explorer run (plus 1 if it finds a blocker or major). |
| `[explore] queue_max` | `POOL_EXPLORE_QUEUE_MAX` | `8` | Pause explorers while this many claimable issues wait in the queue. |
| `[explore] blocked_max` | `POOL_EXPLORE_BLOCKED_MAX` | `10` | Pause explorers while this many issues are blocked. |
| `[explore] open_max` | `POOL_EXPLORE_OPEN_MAX` | `12` | Pause explorers while this many unmerged explorer-filed issues remain open (`0` = no limit). |

### `[areas]` and `[areas.<name>]`

Routes issues to persistent area conversations and serializes work on hot files. Leave `[areas.<name>]` tables out to disable area routing.

| Key | Env var | Default | Description |
|---|---|---|---|
| `refresh_after` | `POOL_AREA_REFRESH` | `6` | Start a fresh area conversation (seeded with the old conversation's notes) after this many issues. |
| `refresh_tokens` | `POOL_AREA_REFRESH_TOKENS` | `0` | Or refresh once the conversation context reaches this many tokens (`0` = disabled). |
| `conversations` | `POOL_AREA_CONVERSATIONS` | `3` | Maximum concurrent conversations per area. |
| `second_at` | `POOL_AREA_SECOND_AT` | `3` | Open conversation `k` (from 0) when `k * second_at` issues are waiting in the area. |
| `lend_at` | `POOL_AREA_LEND_AT` | `4` | When all conversations in an area are busy and this many issues wait, send extra non-hot issues to ordinary workers (`0` = always wait). |
| `lend_free` | `POOL_AREA_LEND_FREE` | `true` | When pool slots are free, send an issue waiting only on a busy area conversation (not a hot file) to an ordinary worker immediately. |
| `hot_lock` | `POOL_AREA_HOT_LOCK` | `true` | Allow only one session at a time to work an issue that names or touched a hot file, from start until its PR opens. |
| `hot_hold_max_s` | `POOL_AREA_HOT_HOLD_MAX_S` | `1800` | Maximum seconds a hot-file lock is held by one session. |
| `auto_hot_at` | `POOL_AREA_AUTO_HOT_AT` | `4` | Promote a file in an area to hot after it conflicts in this many PRs within `POOL_SPLIT_WINDOW_S` (`0` = disabled); also marks new split-target files hot and cools a split file that shrank to `<= 60%`. |
| `auto_hot_expire` | `POOL_AREA_AUTO_HOT_EXPIRE` | `30` | Expire an auto-hot file after this many conflict-free merges (`0` = never). |
| `shared` | `POOL_AREA_SHARED` | `[]` | Files touched across many areas (such as string tables or global stylesheets) that area routing should ignore. |
| `[areas.<name>]` (`paths`, `hot`, `words`, `ids`) | `POOL_AREAS` | none | Define an area's path globs, hot file globs (or `hot = true`), title keywords (`words`), and requirement ID prefixes (`ids`). |

### `[review]`, `[pool]`, `[guard]`, and `[env]`

| Section & Key | Env var | Default | Description |
|---|---|---|---|
| `[review] enabled` | `REVIEW_GATE` | `true` | Run the LLM diff reviewer (`review_gate.py`) before merging (Gate 5). |
| `[review] rules` | `REVIEW_RULES` | `".issuelooper/review_rules.md"` | Path to the project review rules file. |
| `[review] production_paths` | `REVIEW_PATHS` | `["src/"]` | Path prefixes reviewed by `review_gate.py`; diffs outside these paths skip Gate 5. |
| `[review] spec_paths` | `REVIEW_SPEC_PATHS` | `["spec/"]` | Specification paths scanned so identifiers defined in the spec are not mistaken for test-specific hooks (`[]` disables). |
| `[review] max_rejects` | `REVIEW_MAX_REJECTS` | `2` | Maximum review hand-backs before parking the issue at `stage:owner`. |
| `[review] fixtures` | `REVIEW_FIXTURES` | `[]` | Fixture file globs whose literal values are flagged if copied into production code. |
| `[pool] max_sessions` | `POOL_MAX_WORKERS` | `8` | Maximum concurrent agent sessions across all roles (`worker`, `merger`, `tester`, `explorer`). |
| `[pool] worker_timeout` | `POOL_TIMEOUT_WORKER` | `"45m"` (`"60m"` with `devcheck`) | Time limit per worker session before wrap-up and WIP branch rescue. |
| `[pool] merger_timeout` | `POOL_TIMEOUT_MERGER` | `"45m"` | Time limit per merger session. |
| `[pool] tester_timeout` | `POOL_TIMEOUT_TESTER` | `"60m"` | Time limit per tester or explorer session. |
| `[pool] merge_train` | `POOL_MERGE_TRAIN` | `true` | Batch gate-passing PRs onto `main` and run the full suite once per batch. |
| `[pool] train_size` | `POOL_TRAIN_SIZE` | `6` | Maximum PRs tested in a single merge train batch. |
| `[pool] train_wait_s` | `POOL_TRAIN_WAIT_S` | `0` | Seconds to wait after the first queued PR for more PRs to join the train (`0` starts immediately). |
| `[pool] tester_port_base` | `TST_PORT` | `8021`–`8099` (by path hash) | First port assigned to tester sessions (each tester steps by `100`). |
| `[pool] run_dir` | `ISSUELOOPER_RUN` | `".issuelooper/run"` | Directory for supervisor logs, PID files, and session logs (must be git-ignored). |
| `[pool] author_resolve` | `POOL_AUTHOR_RESOLVE` | `false` | Resume the PR author's conversation to resolve rebase conflicts or red rebases before falling back to a merger. |
| `[pool] author_resolve_max` | `POOL_AUTHOR_RESOLVE_MAX` | `2` | Maximum author conflict-resolution attempts before handing to a merger. |
| `[pool] author_guard` | `POOL_AUTHOR_GUARD` | `false` | Resume the PR author's conversation when a merge-train acceptance guard fails because of that PR. |
| `[pool] edge_flag_at` | `POOL_EDGE_FLAG_AT` | `8` | Alert the monitor to new dependencies on unbuilt blockers while this many claimable issues are blocked (`0` disables). |
| `[pool] hub_at` | `POOL_HUB_AT` | `3` | Promote an issue to a hub when this many open issues depend on it (`0` disables). |
| `[pool] hub_timeout` | `POOL_HUB_TIMEOUT` | `1.5` | Timeout multiplier for hub issues. |
| `[pool] starve_overlap` | `POOL_STARVE_OVERLAP` | `true` | When a starving `P0`/`P1` PR holds other merges on the same files, hold only PRs whose hunks fall within 10 lines (`POOL_STARVE_MARGIN`) of its hunks. |
| `[pool] starve_hold_s` | `POOL_STARVE_HOLD_S` | `3600` | Maximum seconds a starving PR holds competing merges. |
| `[pool] prune_other_roots` | `POOL_PRUNE_OTHER_ROOTS` | `false` | On start, also prune 2-day-stale scratch roots belonging to other repositories in `/tmp`. |
| `[guard] secret_urls` | `GUARD_SECRET_URLS` | `[]` | URL substrings that return secrets; unfiltered fetches by agents are refused. |
| `[guard] baseline_tests` | `GUARD_BASELINE_TESTS` | install-commit tests | Test paths/globs whose test functions may not be deleted unless the issue carries `owner-request` (`"off"` disables). |
| `[guard] protected_paths` | `GUARD_PROTECTED_PATHS` | `[]` | Owner-only paths (such as `["spec/"]`) that Gate 6 forbids PRs from modifying. |
| `[guard] background_check` | `GUARD_BACKGROUND` | `true` | Refuse background shell commands (`&`, `nohup`, `setsid -f`) that do not redirect stdout, stderr, and stdin. |
| `[env] <NAME> = "<val>"` | `<NAME>` | none | Pass any additional environment variable directly to the supervisor and hooks (for example `POOL_POLL_S = "15"`). |

---

## 4. CLI commands and observability

### `.claude/hooks/issuelooper` subcommands

| Command | What it does |
|---|---|
| `issuelooper start` | Verifies prerequisites (`gh`, Python 3.11+, configured agent CLIs, `herdr`, git-ignored run dir), opens the monitor and aide panes (or starts the headless broker), sends the monitor kickoff prompt, starts `ccpool.py`, and launches the dashboard. |
| `issuelooper status` | Prints running processes, monitor and aide state, and the supervisor's current summary. |
| `issuelooper watch [SECS]` | Renders the live terminal dashboard, refreshing every `SECS` seconds (default `10`). |
| `issuelooper dashboard` | Prints the local web dashboard URL (`http://127.0.0.1:8780/`), starting the server if needed. |
| `issuelooper plan` | Prints the work units `ccpool.py` would start right now without starting them. |
| `issuelooper say "TEXT" [--wait]` | Sends a message to the monitor from any terminal (`--wait` blocks and prints the monitor's reply). |
| `issuelooper aide [N]` | Opens an additional interactive aide session (`aide2`, `aide3`, ...). |
| `issuelooper attach` | Attaches your terminal to the `herdr` session in which the loop is running. |
| `issuelooper stop [--all]` | Stops the supervisor, active worker/merger/tester sessions, main checks, and the dashboard, and closes the monitor pane (saving unfinished worker changes to WIP branches without counting a failed attempt). `--all` also closes aide panes and prunes 2-day-stale scratch directories. |

### `config.py` helper commands

```bash
python3 .claude/hooks/config.py check      # Validate .issuelooper/config.toml and list unrecognized keys
python3 .claude/hooks/config.py env        # Print resolved `export NAME=value` lines for the shell
python3 .claude/hooks/config.py get NAME   # Print a single resolved environment variable
python3 .claude/hooks/config.py tmp        # Print this repository's $ISSUELOOPER_TMP directory
```

### Running headless without `herdr`

For remote servers or CI environments without `herdr`, set in `.issuelooper/config.toml`:

```toml
[monitor]
host = "broker"
```

Then run `.claude/hooks/issuelooper start` from any shell. Communicate with the headless monitor using `.claude/hooks/issuelooper say "..." --wait`, and open an interactive aide in any terminal with `.claude/hooks/ccrole.sh aide`.

### Where logs and records live

The web dashboard (`http://127.0.0.1:8780/`) and `issuelooper watch` read exclusively from local files under `.issuelooper/run/` and never call GitHub directly. They display seven sections: **Health**, **Needs you**, **Agents**, **Queue**, **Progress** (merges, hand-backs, cycle time, UX scores, token/cost totals, and post-merge acceptance stats), **Over time**, and **Recent** (the last 30 supervisor events).

| What you want to inspect | Path |
|---|---|
| Supervisor stdout/stderr log | `.issuelooper/run/pool.out` |
| Structured supervisor event log & state | `.issuelooper/run/pool/pool.jsonl` and `.issuelooper/run/pool/state.json` |
| One agent session's prompt & transcript | `.issuelooper/run/pool/<seq>-<role>-<unit>/` (`prompt.txt`, readable `session.log`, raw `session.jsonl`) |
| Every sweep run (green, red, or killed) | `$ISSUELOOPER_TMP/sweep/runs.jsonl` (SHA, caller, wait time, steps, outcome, first `E` line of failing tests, changed files, plus JUnit XML when `[sweep] junit = true`) |
| Scratch worktrees & gate records | `$ISSUELOOPER_TMP/` (`/tmp/issuelooper-<repo>-<hash>/`: `sweep/`, `review/`, `devcheck/`, `ux/`) |

---

## 5. Repository layout and running framework tests

```text
install.sh                     Installer and upgrader for target repositories
template/                      Files copied into a target repository by install.sh
  .claude/hooks/               Supervisor (ccpool.py), role_guard.py, claim.py, sweep_run.py,
                               devcheck.py, review_gate.py, herdr_link.py, broker.py,
                               dash_model.py, dashboard.py, issuelooper CLI, and *_test.py
  .claude/skills/issuelooper-* Role instructions read by monitor, worker, merger, tester, aide
  .agents/                     Matching hooks, skills, and rules for the agy CLI
  .githooks/                   pre-commit and pre-push hooks protecting .claude/ and main
  .issuelooper/                Starter templates for config.toml, project.md, review_rules.md
  docs/agent-framework.md      Short overview read by agents inside an installed repo
examples/                      Runnable sample projects (hello-world, live-ai-concierge) and create.sh
docs/                          Architecture guide, configuration reference, and macOS validation
```

To run the framework's own test suite from the `issuelooper` repository root:

```bash
for t in template/.claude/hooks/*_test.py; do python3 "$t" || exit; done
```
