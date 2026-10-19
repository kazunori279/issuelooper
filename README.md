# issuelooper

A framework for developing a repository with many AI coding-agent sessions at
once, coordinated only through GitHub. Issues are the work queue, labels are the
state, pull requests are the output, and signed comments are the conversation. A
deterministic supervisor (a program, not an agent) watches the repository, starts
a fresh agent session for each unit of work, and merges pull requests that pass
its gates.

It supports the Antigravity CLI (`agy`) and Claude Code (`claude`) as the agent
runtime, per role.

## How it works

```
             owner files issues
                     │
                     ▼
   ┌──────────── monitor (one long-lived session) ────────────┐
   │  triage · priorities · holds · dependencies · decisions  │
   └──────────────────────────────────────────────────────────┘
                     │ stage:triage
                     ▼
 supervisor (ccpool.py, polls GitHub every 15 s, one GraphQL call)
   ├─ worker   one per claimable issue: failing test → fix → sweep → PR
   ├─ merge    no agent: review gate + merge gates → squash-merge → deploy
   ├─ merger   one per conflicting PR the supervisor could not rebase
   └─ tester   live checks after merges; UX runs and spec checks of the app
```

| Role | Kind | Does |
|---|---|---|
| **Supervisor** | Program (`ccpool.py`) | Turns GitHub state into units of work, starts them, merges, deploys. |
| **Worker** | Agent session, one per issue | One issue: a failing test, the fix, a green sweep, a pull request. |
| **Merger** | Agent session, one per conflicting PR | Rebases, resolves so both sides survive, sweeps again, merges. |
| **Tester** | Agent session (optional) | Live checks against the real environment; UX runs that walk the deployed app and file what gets in a user's way; spec checks of the app against the spec, one requirement at a time. |
| **Reviewer** | One model call, no tools | Reads the PR's diff and rejects invented data shown as real, code that special-cases tests, and anything your project's rules forbid. |
| **Monitor** | One long-lived agent session | Triage, priorities, holds, dependencies, and the questions only the owner can answer (`stage:owner`). |

Every agent session gets its role from the environment (`CC_ROLE`, set by
`ccrole.sh`), and a hook on every shell command (`role_guard.py`) enforces what
that role may do on GitHub: which labels it may set, who may merge or close, and
that every write carries the role's marker. A pull request merges only when:

| Gate | Requires |
|---|---|
| 0 | The shared checkout is clean. |
| 1 | Every changed `.py` file parses and has no conflict markers. |
| 2 | The PR names the head it swept, and a green sweep record exists for it. |
| 3 | A test that failed before the fix passes after it (`Fail-before: <file> N -> M`), or a stated waiver. |
| 4 | An issue whose acceptance needs the live environment is not closed by the merge. |
| 5 | The reviewer passed this head. |

The supervisor also handles what goes wrong in long unattended runs: workers that
run long get a wrap-up turn, timed-out work is saved to a branch for the next
worker, model quota errors are waited out with backoff, silent sessions are
resumed (sooner for models that step quickly), conflicting PRs that only add
lines are rebased without an agent, and a file that keeps conflicting gets an
issue to split it. When workers run out of issues, testers make the next work:
extra UX runs and, if enabled, spec checks.

## Requirements

- A GitHub repository and the [GitHub CLI](https://cli.github.com/) (`gh`), signed in.
- Python 3.11 or later.
- `agy` or `claude` on the PATH, signed in.
- A check suite that one command runs (`make verify`, say). The merge gates
  expect pytest for the per-issue tests.
- Linux or macOS. Worktrees and scratch files go under `/tmp`.

## Quick start

```bash
git clone https://github.com/kazunori279/issuelooper
./issuelooper/install.sh path/to/your-repo     # copies the framework, creates labels, commits
cd path/to/your-repo
$EDITOR .issuelooper/config.toml .issuelooper/project.md .issuelooper/review_rules.md
ISSUELOOPER_CONFIG_OK=1 git commit -am "Configure issuelooper" && git push
gh issue create --title "..." --body "..."      # the backlog
.claude/hooks/issuelooper start                 # the monitor and the supervisor
.claude/hooks/issuelooper say "Start with #1."  # talk to the monitor
.claude/hooks/issuelooper status
.claude/hooks/issuelooper stop
```

## Configuration

Everything project-specific lives in `.issuelooper/` in your repository:

| File | What it holds |
|---|---|
| `config.toml` | Agent CLI and model per role, the sweep command, the deploy command, live checks, UX runs and spec checks on or off, review settings, pool limits. Each key maps to an environment variable, and an exported variable wins. `python3 .claude/hooks/config.py check` shows the result. |
| `project.md` | What agents cannot guess: how to set up a worktree, where a new test goes, how to run the app on a given port, where the spec is, live mode, secrets. Every session reads it. |
| `review_rules.md` | Project rules the reviewer enforces on top of its two built-in checks. |
| `monitor_kickoff.md` | Optional: the monitor's first message, for example the priorities of a seed backlog. |

[`examples/live-ai-concierge/`](examples/live-ai-concierge/) has the files of the
project the framework was developed on.

## Agent CLIs

`[agents] cli` picks the CLI for every role, and `[agents.<role>]` overrides one
role (`worker`, `merger`, `tester`, `monitor`, `reviewer`). The framework was
developed and run at length on `agy`. Claude Code support follows its documented
`stream-json` output and is newer: report what does not work.

Most of the cost is workers. A stronger model for the judgment roles (monitor,
tester, reviewer) costs a small fraction of running everything on it.

## Limits

- `role_guard.py` is not a security boundary. Every session uses the same GitHub
  login, and a session could set its own `CC_ROLE`; the guard stops sessions that
  never considered the question, not adversarial ones. Run agents with the
  permissions you would give an unattended script.
- Agent sessions run with their CLI's permission prompts off
  (`--dangerously-skip-permissions`), because the guard is the gate. Run the loop
  on a machine or VM dedicated to it.
- One loop per machine: worktree and lock paths under `/tmp` are not namespaced
  per repository.

## Layout

```
install.sh                     install or upgrade into a repository
template/                      what install.sh copies
  .claude/hooks/               supervisor, guard, claims, sweep, review gate, monitor host, launcher (+ tests)
  .claude/skills/issuelooper-* what each role's agent reads
  .agents/                     the same hooks, skills and rules for agy
  .githooks/                   keep sessions out of .claude/ and off main
  .issuelooper/                config and project templates
examples/                      configuration of real projects
```

Tests: `for t in template/.claude/hooks/*_test.py; do python3 "$t"; done`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
