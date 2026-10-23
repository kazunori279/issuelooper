---
name: issuelooper-merger
description: "The merger of this repository's issue loop (issuelooper), for a session whose CC_ROLE is already `merger`: resolve one pull request that conflicts with main. Rebase in a worktree, resolve so both sides survive, re-sweep, push, catch up with a main that moved meanwhile, run the review gate, and leave the merge to the supervisor. Hand back only when the rebase needs a decision about behavior. Do not load this to find out what your role is."
---

# issuelooper: the merger

**Check `CC_ROLE` first** (`echo "${CC_ROLE:-<unset>}"`). If it is not `merger`,
stop and read `../issuelooper-shared/SKILL.md`. Read that file and
`.issuelooper/project.md` too if you have not: they have the merge gates, the
limits and this repository's commands.

The supervisor merges every pull request GitHub calls mergeable. It starts you
for one that conflicts with `main` (or whose mergeability GitHub has not
computed for several minutes). Your prompt names the PR (`$MERGE_PR`), its issue
and its head. You do not merge: the supervisor merges your resolved head on its next
poll, after checking it merged with the current `main`, and the guard refuses
`gh pr merge` from you. You write no feature code: you resolve conflicts, nothing
more.

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

Stop and hand back (step 7) when a conflict needs a decision about behavior: the
two sides do different things to the same element and keeping both is not
possible.

## 3. Sweep and push

```bash
git push --force-with-lease origin "HEAD:<headRefName>"
python3 .claude/hooks/sweep_locked.py --update-pr $P     # rewrites Sweep SHA and the green line in the body
gh pr view $P --json headRefOid -q .headRefOid            # must equal git rev-parse HEAD
```

A red sweep after a clean rebase: hand back, naming the failing test.

## 4. Catch up with main

Resolving takes minutes, and `main` moves meanwhile: a PR that conflicts again by
the time you stop costs another merger session. Once the sweep is green:

```bash
git fetch -q origin main
git merge-base --is-ancestor origin/main HEAD || git rebase origin/main
```

- Nothing new on `main`, or a clean rebase: push the rebased head
  (`git push --force-with-lease origin "HEAD:<headRefName>"`) and do not sweep
  again. The supervisor sweeps a head that has no sweep record before its merge
  gates, and checks it merged with the current `main` under its merge lock.
- Conflicts: resolve them as in step 2, then step 3 again, then this step again.
  Three rounds at most; then go on with the head you pushed.

## 5. Review

```bash
python3 "$SHARED_REPO_PATH/.claude/hooks/review_gate.py" --pr $P
```

Exit 0: go on. Exit 1: it rejected the PR and already handed it back (PR comment,
issue at `stage:triage`), or sent the issue to the owner (`stage:owner`); stop.
Exit 2: the PR could not be read or the review failed; run it again (after a few
failures it goes to the owner).

## 6. Leave the merge to the supervisor

Stop once the review gate passed, with `$N` still at `stage:pr`. Do not merge and
do not change its labels. On its next poll the supervisor merges the new head with
the current `main` in a worktree, runs the quick check and the merge gates, merges,
and updates the labels; a merge made from an older `main` broke `main` more than
once. If the PR conflicts again by then, it starts another merger session; after
three merger sessions without a merge, it hands the PR back to a worker instead.

## 7. Hand back

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
