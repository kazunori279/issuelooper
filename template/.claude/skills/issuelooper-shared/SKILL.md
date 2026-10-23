---
name: issuelooper-shared
description: "Shared reference for this repository's issue loop (issuelooper): the roles (worker, merger, tester, monitor, aide), how the supervisor ccpool.py runs them, the label scheme and who may set what, the merge gates a pull request must pass, comment signing, and the limits every session works under. Read it first, then .issuelooper/project.md, then the skill for your CC_ROLE."
---

# issuelooper: shared

Several agent sessions work this repository's GitHub issues together. GitHub is
the whole protocol between them: issues, comments, labels and pull requests. No
session messages another.

## Which role you are

Your role comes from `CC_ROLE`, never from a document:

```bash
echo "${CC_ROLE:-<unset>}"
```

Unset means stop and ask the owner. Then read `.issuelooper/project.md` (the
facts about this repository) and the skill for your role:

| `CC_ROLE` | Skill | Unit of work |
|---|---|---|
| `worker` | `issuelooper-worker` | one claimed issue, from trace to pull request |
| `merger` | `issuelooper-merger` | one pull request that conflicts with `main` |
| `tester` | `issuelooper-tester` | one live check, or one UX run on the deployed app |
| `monitor` | `issuelooper-monitor` | the backlog: triage, priorities, the owner's decisions |
| `aide` | this file | a session the owner started to file or comment; it never moves a stage |

## How the loop runs

One supervisor per repository, `.claude/hooks/ccpool.py`, reads GitHub every few
seconds and starts a fresh session for each unit of work the moment it appears.
Finish your unit and stop; nothing waits on a timer.

- **Claimable issues** (`stage:triage` or `stage:ready`, no open blocker in a
  `Blocked by:` line or a GitHub dependency, not held, not `ux:proposed`): the
  supervisor claims the issue (label `stage:doing` plus a signed claim comment,
  `claim.py`) and starts a worker. The worker opens a pull request and moves the
  issue to `stage:pr`. A worker that ends without a PR commits its work on
  branch `issue-<N>`; the supervisor pushes it and the next worker on the issue
  continues from it.
- **Pull requests at `stage:pr`**: when GitHub says the PR is mergeable, the
  supervisor runs the review gate (`review_gate.py`) and the merge gates
  (`role_guard.py`), squash-merges it, clears `stage:pr` (or sets `stage:live`
  for a `needs:live` issue) and deploys `main` when the project has a deploy
  command. When the PR conflicts with `main`, the supervisor first rebases it
  itself (when every conflict is two workers only adding lines at the same
  place); otherwise a merger session rebases and resolves it, and the supervisor merges
  the result. A source file that
  keeps conflicting, or grows past 900 lines, gets an issue to split it. A
  refusal over a missing sweep record is re-swept by the supervisor before
  anything is handed back. When the project sets a fast check
  (`sweep.fast_command`), a PR whose head is behind `main` is first merged with
  the current `main` and checked: two PRs that do not conflict can still break
  `main` together (a moved import, a renamed function). Either gate refusing, or
  that check failing, hands the PR back: a comment on the PR with the reason,
  and the issue back at `stage:triage`, where a worker picks it up and updates
  the same PR.
- **`stage:live` issues** (when live checks are on): a tester checks the merged
  change against the live environment and posts a `live-verified:` line; the
  monitor then closes it.
- **The deployed app** (when UX runs are on): a tester walks it as a user would
  and files what gets in a user's way.
- **An idle pool**: when no worker or merger has anything to do, the supervisor
  starts extra UX runs and, when spec checks are on, a tester that checks the app
  against the spec one requirement at a time. What they file is the next work.
- **New issues, priorities, holds, dependencies and the owner's decisions**: the
  monitor, one long-lived session. The supervisor sends it a message whenever
  the queue changed and it is idle.
- **Reopened issues** are open issues like any other. The newest comments say
  why it was reopened; read them first.

The supervisor's log is `$POOL_LOGDIR/pool.jsonl`, its current view
`$POOL_LOGDIR/state.json`. The supervisor, `ccrole.sh` and everything under
`.claude/`, `.agents/` and `.issuelooper/` belong to the owner: never edit them.

## The sweep

`python3 .claude/hooks/sweep_locked.py`, run inside a worktree, waits for a sweep
slot, runs the project's whole check suite (`sweep.command` in
`.issuelooper/config.toml`) and prints `git: <sha> (clean)`, a `green:` line
such as `47 green: unit 23 · e2e 24`, and `sweep record: <path>`. The record,
written only for a green run on a clean commit, is what the merge gate checks for
the PR's head. Any later commit or rebase needs a new sweep.

## The merge gates

A pull request merges only when all of these hold. Every merge is the
supervisor's, through these checks, so write the PR to pass them the first time.

| Gate | Requires |
|---|---|
| 0 | The shared checkout (`$SHARED_REPO_PATH`) is clean. Nobody edits it. |
| 1 | Every changed `.py` file parses and has no conflict markers. |
| 2 | The PR body's `- **Sweep SHA:** <sha>` equals the PR head, and a green sweep record exists for it. |
| 3 | `- **Fail-before:** tests/<suite>/test_<name>.py <N> -> <M>` with M > N, and the named file gains a `def test_`, or one waiver: `waived - needs: tester repro`, `waived - loop/process`, `waived - docs only`, `waived - owner ruling`. |
| 4 | A PR does not auto-close a `needs:live` issue: it says `Refs #N`, not `Fixes #N`. |
| 5 | The review gate passed this head (or the owner let this head through with `review-override: <sha>`; a review that fails, or a PR rejected again after its hand-backs, waits for the owner). A second model reads the diff for data that is made up and shown to the user as if real (when a source fails, finds nothing or leaves a field out), code that exists to pass tests or evaluators (special-casing sample or fixture inputs, test-mode branches, hidden duplicates for checks), and the project's own rules (`.issuelooper/review_rules.md`). Show an honest empty state or error instead of invented data. |
| 6 | Nothing the project lists as the owner's (`guard.protected_paths`, such as the spec) changes. A test that needs other data brings its own copy under the tests; an owner's file that looks wrong is a comment on the issue. |

## Labels

One label per axis. Two `stage:` labels on one issue is a bug.

| Label | Means | Added by | Cleared by |
|---|---|---|---|
| `stage:triage`, `stage:ready` | claimable by a worker | monitor, worker (own claim), tester and aide (`stage:triage` when filing) | monitor, worker (own claim) |
| `stage:doing` | a worker holds the claim | `claim.py` | `claim.py`, monitor |
| `stage:pr` | its PR is open and green | worker (own claim) | supervisor, merger, monitor |
| `stage:live` | merged; the live check is owed | supervisor, merger, monitor | monitor |
| `stage:owner` | waits for the owner's decision; only the owner answers it | anyone | anyone, once the owner answered |
| `needs:live` | acceptance needs the live environment | monitor, worker, tester, aide | monitor only |
| `P0`, `P1`, `P2` | priority (P2 is the default; P0 is broken now) | monitor; tester and aide when filing | monitor |
| `hold:monitor` | the monitor stopped this change from landing | monitor | monitor |
| `on hold` | the owner parked it | the owner | the owner |
| `owner-request` | the owner asked for it | the owner | nobody |
| `ux:proposed` | a proposal waiting for the owner's decision | the owner's tooling | the owner |

Ordering between issues is a GitHub issue dependency (`blocked_by`), never a
comment. Closing work that will not be done is
`gh issue close --reason "not planned"` with a comment naming what replaced it.

`role_guard.py` (a hook on every shell command) enforces who may set which
label, merge or close, and refuses a GitHub write from a session with no role.

## Signing

Every issue body, PR body and comment starts with your role's marker on its
first line: `<!-- worker -->`, `<!-- merger -->`, `<!-- tester -->`,
`<!-- monitor -->`, `<!-- aide -->`. One login serves every session, so the
marker is the only record of who spoke. The guard refuses an unsigned write.

## Limits for every session

- Write files only in your own worktree or scratch paths under `$ISSUELOOPER_TMP`,
  this repository's scratch directory (the tester also writes under `.ux/`, which
  git ignores). If it is unset, set it first:
  `export ISSUELOOPER_TMP=$(python3 "$SHARED_REPO_PATH/.claude/hooks/config.py" tmp); mkdir -p "$ISSUELOOPER_TMP"`.
  Never edit
  `$SHARED_REPO_PATH`, `.claude/`, `.agents/`, `.issuelooper/`, `CLAUDE.md` or
  `AGENTS.md`.
- Never print a secret. Credentials stay where `.issuelooper/project.md` says;
  only the tester's live check and a worker's own check of a `needs:live` change
  use them, through the app.
- No network beyond `gh` against this repository, installing from the lockfile,
  and those live checks.
- Never use a port the owner uses; take the one you are given.
- Stop every process you started before you finish.
- No `file:line`, no claim. Say what you ran and what you inferred.
- Keep GitHub writes few: one comment per state change, no progress comments.
  On a rate-limit refusal, read `gh api rate_limit` first: a full budget means
  the secondary limit, so switch to `gh api` REST instead of waiting.
