# issuelooper

A swarm development framework: your personal software factory. You file issues,
answer the occasional question and review what ships; a team of AI coding agents
does the rest.

issuelooper develops a repository with many AI coding-agent sessions at once,
coordinated only through GitHub. Issues are the work queue, labels are the
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
| 6 | Nothing under the owner's protected paths changes (`[guard] protected_paths`, such as `["spec/"]`; off when empty). |

Each gate checks the PR's head, and a head swept on an older `main` says nothing
about the merged result: two PRs that do not conflict can still break `main`
together (one moves an import the other still uses). With `[sweep] fast_command`
set, the supervisor merges a PR whose head is behind `main` with the current
`main` in a worktree before the gates and runs that quick check there (the app
imports, the unit tests pass). A failure hands the PR back, unless `main` alone
fails the check too, in which case nobody is blamed and the gates decide.

The supervisor also handles what goes wrong in long unattended runs: workers that
run long get a wrap-up turn, timed-out work is saved to a branch for the next
worker, model quota errors are waited out with backoff, silent sessions are
resumed (sooner for models that step quickly; a reply that stops mid-stream counts
too), and a session's clocks stand still while the agent CLI retries a failed model
call (agy). Conflicting PRs that only add lines are rebased without an agent (once
per head; a head whose rebase failed goes to a merger), a file that keeps
conflicting gets an issue to split it, and a deploy that lost a race with another
`git fetch` is tried again. Issues blocked by an open issue (a `Blocked by:` line
or a GitHub dependency) wait. A worker's prompt carries its issue and a map of the
files that name the issue's backticked names and requirement ids (`[spec]
id_pattern`), so it reads less to find its way. When workers run out of issues,
testers make the next work: extra UX runs (never two at once) and, if enabled,
spec checks.

## Requirements

- A GitHub repository and the [GitHub CLI](https://cli.github.com/) (`gh`), signed in.
- Python 3.11 or later.
- `agy` or `claude` on the PATH, signed in.
- [herdr](https://herdr.dev), the terminal workspace manager the monitor and the
  aides run in (or set `[monitor] host = "broker"` for a headless monitor).
- A check suite that one command runs (`make verify`, say). The merge gates
  expect pytest for the per-issue tests.
- Linux or macOS. Worktrees and scratch files go under `/tmp`.
  See [macOS validation](docs/macos-verification.md) for the tested environment
  and remaining limits. Server examples also use `lsof`.

## Try a sample

[`examples/`](examples/) has two samples that one command turns into a repository of
your own, with issuelooper installed and a seed backlog filed:

- [`hello-world`](examples/hello-world/): a small Python CLI and six issues; see the
  loop work in 15-30 minutes.
- [`live-ai-concierge`](examples/live-ai-concierge/): the voice and text concierge
  the framework was developed and benchmarked on, from a first slice to the full spec
  in about six hours, with UX checks for the testers.

```bash
git clone https://github.com/kazunori279/issuelooper
issuelooper/examples/create.sh hello-world <you>/textstats-demo
```

## Quick start

```bash
git clone https://github.com/kazunori279/issuelooper
./issuelooper/install.sh path/to/your-repo     # copies the framework, creates labels, commits
cd path/to/your-repo
$EDITOR .issuelooper/config.toml .issuelooper/project.md .issuelooper/review_rules.md
ISSUELOOPER_CONFIG_OK=1 git commit -am "Configure issuelooper" && git push
gh issue create --title "..." --body "..."      # the backlog
herdr --session your-repo                       # then, in a herdr pane:
.claude/hooks/issuelooper start                 # the monitor, an aide and the supervisor
```

`start` opens a herdr tab named `issuelooper` and the loop runs from there. The
next section is the guide to working with it.

## Working with the loop

You work with the loop the way the agents do: through GitHub, plus two kinds of
agent session you talk to directly in herdr panes.

```
┌─ herdr tab "issuelooper" ───────────────┬──────────────────────────────┐
│ monitor (<repo>-monitor)                │ aide (<repo>-aide1)          │
│ the backlog's moderator; queue updates  │ your own session: research,  │
│ from the supervisor arrive here         │ questions, filing issues     │
├─────────────────────────────────────────┤                              │
│ status: `issuelooper watch`             ├──────────────────────────────┤
│ the dashboard, in the terminal          │ aide2: `issuelooper aide`    │
└─────────────────────────────────────────┴──────────────────────────────┘
```

The supervisor itself runs in the background; its log is
`.issuelooper/run/pool.out`.

### Talk to the monitor

Type into the monitor's pane as you would into any agent session. The monitor
triages new issues, sets priorities, holds or closes work, keeps dependencies,
closes verified live issues, and gathers the questions only you can answer. Use it
for decisions about the backlog:

- "Make #12 P0 and hold #15 until it lands."
- "What merged today, and what is stuck?"
- "Close #20 as not planned; #18 replaces it."

The supervisor sends the monitor a message (starting `Pool update:`) whenever the
queue changes and the monitor is idle. It waits while the pane is changing, so an
update does not land in the middle of what you are typing; a message you send
first is answered first. Keep long research out of the monitor: while it works on
your request it cannot act on queue updates. Use an aide for that.

From a terminal outside herdr: `issuelooper say "..."` (add `--wait` to wait and
print the answer), or `issuelooper attach` to open the herdr session.

### Use an aide for research and filing

An aide is your ad-hoc session (`CC_ROLE=aide`). Ask it anything about the code,
let it investigate a bug, or have it draft and file issues. `issuelooper aide`
opens another one; run as many as you like.

What an aide may do on GitHub is limited by the guard: it files issues (with
`stage:triage` and a priority) and comments, every write signed `<!-- aide -->`.
It never moves an issue's stage, merges or closes; the monitor triages what it
files and the workers pick it up.

### Open issues yourself

Anything you file on GitHub enters the queue; the monitor triages it.

| You want | Do this |
|---|---|
| Something built | File an issue. Add `owner-request` to keep it at least P1; the monitor does not change the priority you give it. |
| It done first | Give it `P0` (broken now, or the minimum viable product needs it). |
| Work paused | Add `on hold`. Only you remove it. |
| A redo | Reopen the issue and say in a comment what is still wrong. The next worker reads the newest comments first. |

### Answer the monitor's questions

When a decision is yours (what the product should do, a test that contradicts
the spec), the monitor moves the issue to `stage:owner` and ends its reply with
the question. Answer with a comment on the issue: the supervisor tells the
monitor, which acts on it. An issue the workers failed three times also waits at
`stage:owner`; any comment from you resets its attempts. So does an issue that
failed its live check twice: the monitor lists what each fix changed and what each
check saw, and asks whether the acceptance holds as written.

To see what waits for you: `gh issue list --label stage:owner`.

### Check what is going on

`issuelooper start` also serves a dashboard on `http://127.0.0.1:8780/` (the next
free port if that one is taken; `issuelooper dashboard` prints the URL). It
refreshes every 15 seconds and shows, from the top:

- **Health**: the supervisor, when it last read GitHub, the monitor's state, any
  quota pause, `main` and the deployed commit.
- **Needs you**: decisions waiting at `stage:owner`, stuck work, an issue whose
  sessions failed twice in a row, a failed deploy, a monitor at a prompt or gone,
  GitHub read errors, updates piling up for the monitor. Each line says what to do.
- **Agents**: every running session with its issue or PR, time against its
  timeout, seconds since its last output, the command it is running, and a link to
  its log; then the monitor and the aides.
- **Queue**: open issues per stage, holds, the owner's decisions, open PRs.
- **Progress** for the last hour and day: merges, hand-backs and why, issue cycle
  time, how sessions ended, tokens per role, and cost where the agent CLI reports
  one (Claude Code does; agy reports tokens only).
- **Over time**: agents at work by role, and open issues against merges per hour.
- **Recent**: the supervisor's last 30 events in plain words.

The status pane in herdr (`issuelooper watch`) shows the same in the terminal. The
dashboard reads only `.issuelooper/run/` and never calls GitHub; it listens on
127.0.0.1 only, so reach it from another machine with `ssh -L 8780:127.0.0.1:8780`.
Turn it off with `[dashboard] enabled = false`.

| To see | Look at |
|---|---|
| Sessions running, waiting, stuck | the dashboard, the status pane, or `issuelooper status` |
| What the supervisor would start next | `issuelooper plan` |
| The queue | GitHub labels: `gh issue list --label stage:doing` (in progress), `stage:pr` (PR open), `stage:owner` (your decision) |
| What one session did | `.issuelooper/run/pool/<seq>-<role>-<unit>/`: `prompt.txt`, `session.log` (readable), `session.jsonl` (raw) |
| The supervisor's decisions | `.issuelooper/run/pool/pool.jsonl` and `state.json` |

### Stop and restart

`issuelooper stop` stops the supervisor (and the worker, merger and tester
sessions it runs) and the dashboard, and closes the monitor's pane; aides stay open with their
conversations. `issuelooper stop --all` closes the aides too. A stopped worker's
unfinished work is pushed to its issue's branch and the issue goes back to the queue,
without counting a failed attempt. `issuelooper start` starts everything again; the
backlog lives on GitHub, so nothing is lost.

### Without herdr

`[monitor] host = "broker"` runs the monitor as a headless session (for a server
or CI). Talk to it with `issuelooper say`, and open an aide in any terminal with
`.claude/hooks/ccrole.sh aide`.

## Configuration

Everything project-specific lives in `.issuelooper/` in your repository:

| File | What it holds |
|---|---|
| `config.toml` | Agent CLI and model per role, where the monitor runs (herdr or headless), the sweep command and the quick merged-tree check, the deploy command, live checks, UX runs and spec checks on or off, review settings, pool limits, the owner's protected paths. Each key maps to an environment variable, and an exported variable wins. `python3 .claude/hooks/config.py check` shows the result. |
| `project.md` | What agents cannot guess: how to set up a worktree, where a new test goes, how to run the app on a given port, where the spec is, live mode, secrets. Every session reads it. |
| `review_rules.md` | Project rules the reviewer enforces on top of its two built-in checks. |
| `monitor_kickoff.md` | Optional: the monitor's first message, for example the priorities of a seed backlog. |

The samples' `issuelooper/` directories ([`hello-world`](examples/hello-world/issuelooper/),
[`live-ai-concierge`](examples/live-ai-concierge/issuelooper/)) are filled-in examples of these files.

## Agent CLIs

`[agents] cli` picks the CLI for every role, and `[agents.<role>]` overrides one
role (`worker`, `merger`, `tester`, `monitor`, `reviewer`, `aide`). The framework
was developed and run at length on `agy`. Claude Code 2.1.286 has been validated
on macOS with a headless monitor, workers, sweeps, the review gate and automatic
merges. Session resume and the Bash role guard were also checked with the real CLI.
See [the validation record](docs/macos-verification.md) for evidence and the
remaining limits, including mixed-CLI runs and live stall/quota recovery.

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
  .claude/hooks/               supervisor, guard, claims, sweep, review gate, herdr link, headless monitor host, dashboard, launcher (+ tests)
  .claude/skills/issuelooper-* what each role's agent reads
  .agents/                     the same hooks, skills and rules for agy
  .githooks/                   keep sessions out of .claude/ and off main
  .issuelooper/                config and project templates
examples/                      samples: an app, its configuration and a seed backlog each; create.sh
```

Tests: `for t in template/.claude/hooks/*_test.py; do python3 "$t"; done`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
