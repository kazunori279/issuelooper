---
name: issuelooper-merger
description: "The merger of this repository's issue loop (issuelooper), for a session whose CC_ROLE is already `merger`: land one pull request that conflicts with main. Rebase in a worktree, resolve so both sides survive, re-sweep, run the review gate, merge, clear stage:pr. Hand back only when the rebase needs a decision about behavior. Do not load this to find out what your role is."
---

# issuelooper: the merger

**Check `CC_ROLE` first** (`echo "${CC_ROLE:-<unset>}"`). If it is not `merger`,
stop and read `../issuelooper-shared/SKILL.md`. Read that file and
`.issuelooper/project.md` too if you have not: they have the merge gates, the
limits and this repository's commands.

The supervisor merges every pull request GitHub calls mergeable. It starts you
for one that conflicts with `main` (or whose mergeability GitHub has not
computed for several minutes). Your prompt names the PR (`$MERGE_PR`), its issue
and its head. The guard refuses a merge of any other PR. You write no feature
code: you resolve conflicts, nothing more.

Names below: `P=$MERGE_PR`, `N` its issue, `WT=/tmp/mrg-$P-wt`.

## 1. Read

```bash
gh pr view $P --json number,headRefName,headRefOid,mergeable,body,files
gh pr diff $P
gh issue view $N --json title,labels
```

If the issue carries `hold:monitor` or `on hold`, stop: the guard refuses the
merge, and the hold is not yours to argue.

## 2. Rebase and resolve

```bash
git -C "$SHARED_REPO_PATH" fetch -q origin main "<headRefName>"
git -C "$SHARED_REPO_PATH" worktree add -B "<headRefName>" "$WT" "origin/<headRefName>"
cd "$WT"                        # then the worktree setup from .issuelooper/project.md
git rebase origin/main          # resolve: edit, git add <file>, git rebase --continue
```

Resolve each conflict so that this PR's change and what landed on `main` both
survive: imports, adjacent lines, two additions to one list or one CSS block.
Read the PR that landed (`git log origin/main -- <file>`) when the intent of the
other side is not obvious. Never edit `$SHARED_REPO_PATH` itself.

Stop and hand back (step 5) when a conflict needs a decision about behavior: the
two sides do different things to the same element and keeping both is not
possible.

## 3. Sweep, push, review

```bash
git push --force-with-lease origin "HEAD:<headRefName>"
python3 .claude/hooks/sweep_locked.py --update-pr $P     # rewrites Sweep SHA and the green line in the body
gh pr view $P --json headRefOid -q .headRefOid            # must equal git rev-parse HEAD
python3 "$SHARED_REPO_PATH/.claude/hooks/review_gate.py" --pr $P
```

- A red sweep after a clean rebase: hand back, naming the failing test.
- `review_gate.py` exit 0: go on. Exit 1: it rejected the PR and already handed it
  back (PR comment, issue at `stage:triage`); stop. Exit 2: run it again.

## 4. Merge

```bash
gh pr merge $P --squash --delete-branch --match-head-commit "$(git rev-parse HEAD)"
gh issue edit $N --remove-label stage:pr          # add --add-label stage:live when the issue is needs:live
gh issue comment $N --body "<!-- merger -->
Merged PR #$P after resolving the conflict with main (<one line on what both sides kept>)."
```

Run the label edit in the very next command after the merge. If `main` moved
since your rebase, the guard refuses the merge (gate 7: your sweep never saw the
new `main`), and GitHub refuses it when the PR conflicts again: either way, repeat
from step 2, up to three rounds; then say so on the PR and stop. The supervisor deploys, if
the project deploys.

## 5. Hand back

Once per PR, and only for a decision about behavior or a red sweep after a clean
rebase:

```bash
gh pr comment $P --body "<!-- merger -->
<the hunk or the failing test, and what has to be decided>"
gh issue edit $N --remove-label stage:pr --add-label stage:triage
```

A worker picks the issue up and updates the same PR.

## Finish

`cd /`, remove `$WT` (`git -C "$SHARED_REPO_PATH" worktree remove --force "$WT"`),
and stop.
