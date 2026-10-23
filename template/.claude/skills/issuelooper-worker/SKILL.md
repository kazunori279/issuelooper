---
name: issuelooper-worker
description: "The worker of this repository's issue loop (issuelooper), for a session whose CC_ROLE is already `worker`: one claimed issue from trace to pull request. Worktree from origin/main (or from the branch an earlier worker left), cause traced to a line, a failing pytest first (Fail-before), the fix, the sweep, the PR in the loop's format, stage:pr. Also covers updating a PR that came back, and releasing a claim. Do not load this to find out what your role is."
---

# issuelooper: the worker

**Check `CC_ROLE` first** (`echo "${CC_ROLE:-<unset>}"`). If it is not `worker`,
stop and read `../issuelooper-shared/SKILL.md`. Read that file and
`.issuelooper/project.md` too if you have not: they have the merge gates, the
limits and this repository's commands.

The supervisor already claimed one issue for you and your prompt names it, its
claim token and your session number. You take that issue to a pull request and
stop. You never touch another issue. You do not merge, close or deploy: the
supervisor merges your PR once the merge gates and the review gate pass, so the
PR body has to carry everything they check.

Names below: `K=$WRK_K`, `N` your issue, `T` your claim token,
`WT=$ISSUELOOPER_TMP/wrk-$K-$N-wt`, scratch files `$ISSUELOOPER_TMP/wrk-$K-$N-*`. Write nowhere else.

## 1. Read the issue

Your prompt quotes the issue as of the claim: its body and newest comments (a
report from whoever reopened it, a review-gate or merge-gate refusal, the owner's
answer), and a list of the files on `origin/main` that name its ids. Read those
and the spec sections it touches. Run `gh issue view $N --comments` only when the
prompt says older comments were left out and you need them, or to see a comment
written after the claim. An `owner-request` issue says what the owner wants as a
user: build that, not a control or a label that only suits a test.

If live checks are on and any acceptance criterion can only be observed in the
live environment (`.issuelooper/project.md`, *Live mode*), add `needs:live`
(`gh issue edit $N --add-label needs:live`) and write `Refs #N` instead of
`Fixes #N` in the PR. The merge gate refuses a PR that would close a
`needs:live` issue.

**Check a `needs:live` change in the live environment yourself, before the PR.**
The tester's live check after the merge is the gate, but a change it sends back
costs a whole round (one live issue once took six worker sessions this way). When
the project has `[devcheck]` on, that is step 4's live check; otherwise, in
your worktree, start the app in live mode on a free port, in the background, as
*Live mode* in `.issuelooper/project.md` says (it names where the credentials
come from), with its log at `$ISSUELOOPER_TMP/wrk-$K-$N-live.log`. Drive the issue's steps at
least twice (in each language the issue concerns), read the screenshots and the
server log, stop the server with `lsof -tiTCP:<port> -sTCP:LISTEN | xargs kill`, and remove any credential
link you made in the worktree before you commit. Say what the live environment
did under *Checked on screen*. Never print the credentials or commit them.

## 2. Worktree

```bash
git -C "$SHARED_REPO_PATH" fetch -q origin main
git -C "$SHARED_REPO_PATH" worktree prune
git -C "$SHARED_REPO_PATH" worktree add -B "issue-$N" "$WT" origin/main
cd "$WT" || exit 1  # then run the worktree setup from .issuelooper/project.md
```

- **Your prompt names an earlier worker's branch** (`origin/issue-$N`): start
  from it instead (`worktree add -B issue-$N "$WT" origin/issue-$N`), read its
  commits and the release comment on #N, and continue. Rebase onto
  `origin/main` before the sweep.
- **Your prompt names an open PR**: check out its branch the same way, read the
  newest comments on the PR, fix what they ask, and update that PR (step 6).
- Never edit `$SHARED_REPO_PATH` itself: Gate 0 refuses every merge while it is
  dirty, which stops everyone's work from landing.
- `already checked out at $ISSUELOOPER_TMP/wrk-J-N-wt`: an earlier attempt died. Remove it
  (`git -C "$SHARED_REPO_PATH" worktree remove --force $ISSUELOOPER_TMP/wrk-J-N-wt`) and retry.
- `cannot lock ref` or `index.lock exists`: another session is writing the
  shared `.git`. Wait five seconds and retry.

## 3. Trace the cause, then write the failing test

Find the line that is wrong, or the place a feature is missing, and decide the
approach. Read the way a careful engineer with a short budget does:

- Start from the file list in your prompt; search only when it does not lead you
  there, and search narrowly (`grep -n <name>` in the files or directory you
  expect, not `grep -rn` over the repository).
- Read each file once, whole or in the ranges you need, and keep what you learned
  in your notes. Do not reread a file you have read unless you changed it.
- Check `git status` and `git log` when you need them for a decision (before a
  commit, after a rebase), not as a habit between steps.
- To try a behavior, write the failing test and run it; do not probe with a string
  of one-line scripts.

Then write the test **in a new file of its own**, named
`test_issue_$N.py` in the suite `.issuelooper/project.md` names for this kind of
change. Never append to a shared test file: every worker appending to the same
file is what makes pull requests conflict. Use the suite's existing fixtures; add
to a conftest only what several tests need. The cases test the issue's
acceptance, not what the code does now. For a report of something a user saw, a
case reproduces what the user did and asserts the symptom the report names, so it
fails if that symptom comes back. Never change what an existing test expects (if
an expectation looks wrong, that is the owner's call: step 7).

Run the new file before the fix and keep the counts (the one-file command is in
`.issuelooper/project.md`):

```bash
pytest -q tests/<suite>/test_issue_$N.py 2>&1 | tail -3     # e.g. 2 failed
```

That gives the Fail-before line, the passing count before the fix and after it; a
new file counts from zero: `tests/<suite>/test_issue_$N.py 0 -> 2`. A PR that comes
back keeps its file and adds cases to it.

## 4. Fix

Make the change, run the test file again (all green) and the project's lint.
Keep the diff to the issue. Other sessions are editing the shared files right
now: small local hunks merge; a reformatted or reordered file conflicts with
everyone.

**A change a user can see** (an `owner-request` issue, a `UX:` title, anything in
the user interface, and any issue that was reopened because a user saw it still
broken): look at it before the PR, when `.issuelooper/project.md` says how. Start
the app from your worktree on a free port, in the background, as *Run the app on
a port* says, and wait until it answers. Write a short script that replays **the
exact steps** of the issue's reproduction, or of the report your prompt quotes,
step by step from a fresh start, not a shortcut to the end state. Save
screenshots under `$ISSUELOOPER_TMP/wrk-$K-$N-shots/`, before your change (on `origin/main`, from
a second worktree: `git -C "$SHARED_REPO_PATH" worktree add --detach $ISSUELOOPER_TMP/wrk-$K-$N-base-wt origin/main`;
never `git stash`, which the guard refuses) and after it, the last one
showing the state the issue is about. Open them and compare: the symptom is gone,
and nothing next to it broke (overlap, cut-off text, a control that moved). Fix
what you see, then stop the server by its port (`lsof -tiTCP:<port> -sTCP:LISTEN | xargs kill`, never
`pkill`). In the PR body under *Checked on screen*, list the steps you replayed,
what the last screenshot shows, and its path.

**The live check** (when `[devcheck]` is on in `.issuelooper/config.toml`): the
screenshots above come from a session that may use test doubles; a developer also
uses the branch the way it runs for users before asking for review. Do it for
every change a user can see, and for any PR the merge gate puts in scope (an issue
labelled as `[devcheck] labels` lists, or a change under `[devcheck] paths`). After
the commit in step 5, with your replay script written to run against
`$DEVCHECK_URL` and save its screenshots under `$DEVCHECK_OUT`:

```bash
python3 .claude/hooks/devcheck.py --script $ISSUELOOPER_TMP/wrk-$K-$N-live.py
```

It takes a slot, starts the app from your worktree in the real environment on its
own port, runs the project's smoke and then your script, stops the app and prints
`devcheck: <sha> pass` and `devcheck record: <path>`. Replay the issue's steps
from a fresh start, as above, and also the steps a user would take next around
the change (set a value, then use what depends on it; switch the language, then
ask again). Read the screenshots: a script that exits 0 says the steps ran, not
that the result is right. Run it again after any new commit; put the sha under
*Live check* in the PR body. A failure: read `app.log` and the step logs in the
directory it names, fix, commit, check again. A change no user can see waives it
(`- **Live check:** waived - no visible change`); an issue with one of the labels
cannot be waived that way.

Never show the user invented data. When a source fails, a lookup finds nothing
or a field is missing, show an honest empty state or an error, not a plausible
default. Never special-case the sample inputs or fixture values. The review gate
rejects both, and a reject costs you a round trip.

## 5. Commit, rebase, sweep

```bash
git add <the project paths you changed>     # never .claude/, .agents/ or .issuelooper/
git commit -q -m "<what changed and why> (#$N)"
git fetch -q origin main && git rebase origin/main
python3 .claude/hooks/sweep_locked.py
```

The sweep must print `git: <sha> (clean)`, the `green:` line and
`sweep record: <path>`. Commit first and sweep after. Red: fix, commit, sweep
again. A wait of a few minutes for a sweep slot is normal; never delete a lock.
Then the live check (step 4), at the same commit.

## 6. Push, open the PR, move the issue

```bash
python3 .claude/hooks/claim.py check $N --token $T     # exit 4: another session holds it; stop, no PR
git push --force -u origin "HEAD:issue-$N"
gh pr create --base main --head "issue-$N" --title "<the symptom, short> (#$N)" --body-file $ISSUELOOPER_TMP/wrk-$K-$N-pr.md
gh issue edit $N --add-label stage:pr --remove-label stage:doing,stage:triage,stage:ready,stage:owner
```

The PR body:

```
<!-- worker -->
Fixes #N                         <- Refs #N if the issue carries needs:live

**Cause:** <file:line and one or two sentences>
**Change:** <what you changed and why>

- **Base:** <the origin/main sha you rebased onto>
- **Sweep SHA:** <the sha on the sweep's git: line; equals git rev-parse HEAD>
- **Fail-before:** tests/<suite>/test_issue_N.py 0 -> 2
- **Live check:** <sha> pass      <- with [devcheck] on: the sha devcheck.py printed, or a waiver

<the sweep's green: line, exactly as printed>

**Red before the fix:** <test name and the failing assertion>
**Checked on screen:** <what the screenshots showed; "not visible" for a change no user sees>
**Could not settle:** <anything needing the live environment or a decision; "nothing" if so>
```

Paste the `green:` line from the sweep on its own line, exactly as printed, and
the full Sweep SHA: the merge gates refuse a body without them, and a refusal
sends the PR back to triage. If no case can be written, waive Fail-before with
exactly one of the four waivers in the shared skill and say why under *Could not
settle*.

**Updating a PR that came back** (your prompt names it): push the branch
(`--force-with-lease`), run `python3 .claude/hooks/sweep_locked.py --update-pr <PR>`
instead of the plain sweep (it rewrites Sweep SHA and the green line in the
body), and with `[devcheck]` on `python3 .claude/hooks/devcheck.py --script <...> --update-pr <PR>`
(it rewrites the Live check line), fix the body if a gate named something in it,
and move the issue to `stage:pr` as above. Never open a second PR.

That is the whole GitHub footprint of a normal unit: the PR and one label edit.

## 7. When you cannot finish

Commit what you have on `issue-$N` (the supervisor pushes it for the next
worker). Say once on the issue what blocked you and what is left
(`gh issue comment $N --body-file $ISSUELOOPER_TMP/wrk-$K-$N-body.md`, body starting
`<!-- worker -->`), then give the claim back:

```bash
python3 .claude/hooks/claim.py release $N --token $T --reason "<one line>"                  # back to the pool
python3 .claude/hooks/claim.py release $N --token $T --reason "<one line>" --to stage:owner # needs a decision
```

- The acceptance contradicts the spec or an existing test: `--to stage:owner`.
- It needs another open issue first: record the dependency
  (`gh api -X POST "repos/{owner}/{repo}/issues/$N/dependencies/blocked_by" -F issue_id=<id of M>`,
  where `gh api repos/{owner}/{repo}/issues/M -q .id` gives the id), then release.
- An environment problem: release to the pool.

A release counts as a failed attempt; after three, the issue goes to the owner
until they comment. If the session times out, the supervisor releases the claim
and saves your worktree to `issue-$N` itself.

## 8. Finish

`cd /`, remove your worktree
(`git -C "$SHARED_REPO_PATH" worktree remove --force "$WT"`), stop anything you
started. Your last line is `WORKER-DONE pr #<number>` or `WORKER-RELEASED`.

Budget: near the end of your time the supervisor interrupts you with a wrap-up
turn and a few minutes to push a green PR or commit and release; at the timeout
the session is killed. If two thirds of your time pass without a green sweep,
commit and release with what you learned.
