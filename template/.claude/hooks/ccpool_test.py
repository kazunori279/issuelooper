#!/usr/bin/env python3
"""Cases for ccpool.py. Run it: `python3 ccpool_test.py`.

Two layers. The scheduling cases build the GitHub state by hand and drive
Pool.step() with sessions not started, finishing them by hand, so every rule
(one session per unit, caps, nothing twice, slots freed at once) is checked
without timing. The end-to-end case runs the real supervisor loop against a fake
`gh` (claim_test.py's REST fake plus a GraphQL answer from the same state) and a
fake ccrole.sh that records what each session was started with. The session cases
run a fake CLI per schema (agy, and Claude Code from its documented format).
"""

from __future__ import annotations

import atexit
import json
import os
import pathlib
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

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
# The repository's scratch directory, for every case and every process they start.
SCRATCH = os.path.realpath(tempfile.mkdtemp(prefix="ccpool-scratch-"))
os.environ["ISSUELOOPER_TMP"] = SCRATCH
import agent_cli  # noqa: E402
import ccpool  # noqa: E402
import claim_test  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0
MAIN = "a" * 40
NOW = 2_000_000_000.0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


def iss(n, labels, body="", updated="2026-10-09T10:00:00Z", title=None):
    return {
        "number": n,
        "title": title or f"issue {n}",
        "body": body,
        "updated": updated,
        "labels": sorted(labels),
    }


def pr(n, issue, head="b" * 40, mergeable="MERGEABLE", draft=False):
    return {
        "number": n,
        "branch": f"issue-{issue}",
        "head": head,
        "body": f"Fixes #{issue}",
        "draft": draft,
        "mergeable": mergeable,
    }


def closed(n, labels=(), reason="COMPLETED", at="2026-10-09T11:00:00Z"):
    return {
        "number": n,
        "title": f"issue {n}",
        "closed": at,
        "reason": reason,
        "labels": sorted(labels),
    }


def state(open_=(), closed_=(), prs=(), main=MAIN):
    return {
        "open": list(open_),
        "closed": list(closed_),
        "prs": list(prs),
        "main": main,
    }


class Rig:
    """A Pool whose fetcher returns `self.st` and whose sessions never start."""

    def __init__(self, st, **cfg):
        self.st = st
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-test-"))
        base = dict(
            logdir=self.dir / "pool",
            caps={r: 0 for r in ccpool.ROLES},
            monitor_min_gap_s=0,
        )
        base.update(cfg)
        self.pool = ccpool.Pool(
            ccpool.Config(**base), fetcher=lambda: self.st, spawn=False
        )
        self.now = NOW
        self.pool.clock = lambda: self.now

    def step(self, dt=1.0):
        self.now += dt
        return self.pool.step(self.now)

    def running(self, role=None):
        return sorted(
            u.key for u in self.pool.book.running.values() if role in (None, u.role)
        )

    def finish(self, key, outcome="ok"):
        w = next(w for w in self.pool.sessions.values() if w.unit.key == key)
        self.pool.finished(w, outcome)


def keys(units):
    return [u.key for u in units]


def test_one_worker_per_claimable_issue() -> None:
    h = Rig(state([iss(n, ["stage:triage", "P1"]) for n in range(1, 13)]))
    started = h.step()
    check("12 claimable issues start 12 workers (no cap)", len(started), 12)
    check("all of them workers", {u.role for u in started}, {"worker"})
    check("a second poll of the same state starts nothing", h.step(), [])


def test_cap_and_instant_refill() -> None:
    issues = [
        iss(1, ["stage:ready", "P2"]),
        iss(2, ["stage:triage", "P0"]),
        iss(3, ["stage:triage", "P1"]),
        iss(4, ["stage:triage"]),
        iss(5, ["stage:ready", "P1"]),
    ]
    h = Rig(state(issues), caps={"worker": 2, "merger": 0, "tester": 0})
    started = h.step()
    check(
        "cap 2 starts the two highest priorities", [u.number for u in started], [2, 3]
    )
    check("nothing more while the cap is full", h.step(), [])
    h.finish(started[0].key)
    # #2's worker opened a (draft) PR, so #2 is at stage:pr with a pull request.
    h.st = state([i for i in issues if i["number"] != 2] + [iss(2, ["stage:pr", "P0"])],
                 prs=[pr(9, 2, draft=True)])
    check(
        "a finished session frees its slot on the next poll",
        [u.number for u in h.step(0)],
        [5],
    )


def test_global_cap_orders_roles() -> None:
    st = state(
        [
            iss(1, ["stage:triage", "P0"]),
            iss(2, []),
            iss(3, ["stage:pr", "P1"]),
            iss(4, ["stage:pr"]),
        ],
        [closed(9)],
        [pr(20, 3), pr(21, 4, mergeable="CONFLICTING")],
    )
    h = Rig(st, global_cap=2, ux=True)
    rebased = f"rebase:21@{'b' * 12}@{MAIN[:12]}"
    h.pool.book.tries[rebased] = 1  # the supervisor's rebase of #21 already failed
    started = h.step()
    check(
        "global cap 2: the auto-merge does not count; merger, then the UX run (workers last)",
        [(u.role, u.kind) for u in started],
        [("auto", "merge"), ("merger", "land"), ("tester", "ux")],
    )
    h2 = Rig(st, ux=True)
    h2.pool.book.tries[rebased] = 1
    check(
        "uncapped, everything due starts; closed issues start nothing",
        sorted((u.role, u.kind) for u in h2.step()),
        [
            ("auto", "merge"),
            ("merger", "land"),
            ("tester", "ux"),
            ("worker", "implement"),
        ],
    )
    h3 = Rig(
        state(
            prs=[pr(30 + n, n) for n in range(1, 7)],
            open_=[iss(n, ["stage:pr"]) for n in range(1, 7)],
        ),
        auto_cap=4,
    )
    check("POOL_CAP_AUTO caps the auto-merges", len(h3.step()), 4)


def test_what_is_not_claimable() -> None:
    st = state(
        [
            iss(1, ["stage:triage", "P1", "on hold"]),
            iss(2, ["stage:ready", "P1", "hold:monitor"]),
            iss(3, ["stage:triage", "P0"], body="Blocked by: #4"),
            iss(4, ["stage:doing", "P0"], updated="2099-01-01T00:00:00Z"),
            iss(5, ["stage:owner", "P1"]),
            iss(6, ["stage:triage", "P2"], body="Blocked by: #99"),
            {**iss(7, ["stage:triage", "P0"]), "blocked_by": 1},  # a GitHub dependency
        ]
    )
    h = Rig(st)
    check(
        "held, blocked (a line or a GitHub dependency), in flight and owner issues start nothing; "
        "a closed blocker does not block",
        keys(h.step()),
        ["wrk:6@2026-10-09T10:00:00Z"],
    )


def test_merges_one_per_pr() -> None:
    st = state(
        [
            iss(1, ["stage:pr"]),
            iss(2, ["stage:pr"]),
            iss(3, ["stage:triage"]),
            iss(4, ["stage:pr", "on hold"]),
            iss(5, ["stage:pr"]),
            iss(6, ["stage:pr", "needs:live"]),
            iss(7, ["stage:pr"]),
        ],
        prs=[
            pr(11, 1),
            pr(12, 2, mergeable="CONFLICTING"),
            pr(13, 3),
            pr(14, 4),
            pr(15, 5, draft=True),
            pr(16, 6),
            pr(17, 7, mergeable="UNKNOWN"),
        ],
    )
    h = Rig(st, unknown_s=300, live=True)
    started = h.step()
    check(
        "MERGEABLE: the supervisor merges; CONFLICTING: the supervisor's rebase first; UNKNOWN: wait",
        sorted((u.role, u.kind, u.number) for u in started if u.role != "worker"),
        [("auto", "merge", 11), ("auto", "merge", 16), ("auto", "rebase", 12)],
    )
    r12 = next(u for u in started if u.number == 12)
    check("the rebase unit knows the branch", r12.info["branch"], "issue-2")
    h.finish(r12.key, "left to a merger (a hunk changes existing lines)")
    check(
        "after one rebase attempt at this head and main, a merger session",
        [(u.role, u.number) for u in h.step()],
        [("merger", 12)],
    )
    check(
        "the handed-back PR's issue goes to a worker",
        [u.number for u in started if u.role == "worker"],
        [3],
    )
    m16 = next(u for u in started if u.number == 16)
    check(
        "the merge unit names its issue, head and needs:live",
        (m16.info["issue"], m16.info["needs_live"]),
        (6, True),
    )
    check("no second unit for the same PR", h.step(), [])
    h.finish(next(k for k in h.running("merger")), "ok")
    h.finish(
        next(u.key for u in started if u.number == 11), "failed (exit 1)"
    )
    check("a failed auto-merge is retried once", [u.number for u in h.step()], [11])
    h.finish(
        next(k for k in h.running("auto") if k.startswith("merge:11")),
        "failed (exit 1)",
    )
    check("then not again for the same head and main", [u.number for u in h.step()], [])
    check(
        "and the monitor hears it is stuck",
        any("stuck: auto unit merge PR #11" in e for e in h.pool.events),
        True,
    )
    h.st = state(st["open"], prs=st["prs"], main="c" * 40)
    check(
        "main moving gives the PR a fresh attempt; #12's head already failed a rebase, so a merger",
        sorted((u.kind, u.number) for u in h.step()),
        [("land", 12), ("merge", 11)],
    )
    check(
        "UNKNOWN for POOL_UNKNOWN_S: a merger session takes it",
        [(u.role, u.number) for u in h.step(400) if u.number == 17],
        [("merger", 17)],
    )


def test_merger_loop_goes_back_to_a_worker() -> None:
    """Main moved while each merger resolved the PR, and it conflicted again: after
    POOL_MERGER_MAX merger sessions it goes back to a worker, not to a fourth merger."""
    def conflicting(k):
        return state([iss(2, ["stage:pr"])], prs=[pr(12, 2, head=str(k) * 40, mergeable="CONFLICTING")],
                     main=chr(ord("c") + k) * 40)
    h = Rig(conflicting(1), merger_max=3)
    for k in range(1, 4):
        h.st = conflicting(k)
        h.pool.book.rebase_failed[12] = str(k) * 40  # the supervisor's rebase of this head failed
        started = h.step()
        check(f"conflict {k}: a merger session", [(u.role, u.kind) for u in started], [("merger", "land")])
        h.finish(started[0].key)
        check(f"a merger's end makes the supervisor poll fast (conflict {k})", h.pool.hurry_pr, 12)
    check("three merger sessions without a merge are counted", h.pool.book.merger_runs[12], 3)
    h.st = conflicting(4)
    h.pool.book.rebase_failed[12] = "4" * 40
    started = h.step()
    check("the fourth conflict: a hand-back (no agent), not a merger",
          [(u.role, u.kind, u.info.get("mergers")) for u in started], [("auto", "handback", 3)])
    h.finish(started[0].key, "handed back after repeated conflicts")
    check("the hand-back resets the count", h.pool.book.merger_runs[12], 0)
    h.pool.book.merger_runs[12] = 2
    h.st = state([iss(2, ["stage:pr"])], prs=[pr(12, 2, head="9" * 40)], main="f" * 40)
    started = h.step()
    check("the PR reads MERGEABLE: no more fast polls", (h.pool.hurry_pr, [u.kind for u in started]),
          (None, ["merge"]))
    h.finish(started[0].key)
    check("a merge resets the count", h.pool.book.merger_runs[12], 0)
    h.pool.book.merger_runs[12] = 0
    h.pool.finished(ccpool.Session(h.pool, ccpool.Unit("merger", "land", "mrg:12@q", "mrg:12", 12, (0, 12),
                                                        {"issue": 2}), 99), "quota error (exit 3)")
    check("a merger that ended on a quota error does not count", h.pool.book.merger_runs[12], 0)
    calls = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-handback-")) / "gh.log"
    gh = calls.parent / "gh"
    gh.write_text(f'#!/bin/sh\necho "$*" >> {calls}\n')
    gh.chmod(0o755)
    pool = ccpool.Pool(ccpool.Config(logdir=calls.parent / "pool", gh=str(gh)), fetcher=state, spawn=False)
    w = ccpool.Session(pool, ccpool.Unit("auto", "handback", "handback:12@x", "mrg:12", 12, (0, 12),
                                         {"issue": 2, "mergers": 3}), 1)
    w.dir.mkdir(parents=True)
    check("the hand-back unit's outcome", w.work(), "handed back after repeated conflicts")
    log = calls.read_text().splitlines()
    check("it asks a worker to rebase onto the current main, and moves #2 to stage:triage",
          (log[0].startswith("pr comment 12 --body <!-- merger -->"), "Rebase it onto the current origin/main" in calls.read_text(),
           log[-1]), (True, True, "issue edit 2 --remove-label stage:pr --add-label stage:triage"))


def test_pr_family_starts_afresh() -> None:
    """A PR handed back after three merger sessions kept its family's failures (four
    failed rebases, the hand-back, a merge), so the head the worker pushed next was
    neither rebased nor merged: "still due after 0 session(s) (family)"."""
    def at(head, mergeable="CONFLICTING", main="c" * 40):
        return state([iss(7, ["stage:pr", "P0"])], prs=[pr(103, 7, head=head * 40, mergeable=mergeable)], main=main)
    h = Rig(at("1"), family_max=6)
    h.finish(h.step(0)[0].key, "left to a merger (a hunk changes existing lines)")
    h.pool.book.family_starts["mrg:103"] = 6
    h.pool.book.tries[f"merge:103@{'1' * 12}@{'c' * 12}"] = 1  # an earlier failed merge of this head
    h.st = at("1", main="d" * 40)
    check("six failures in the family: nothing starts", h.step(), [])
    check("and the monitor hears which limit holds it",
          [e for e in h.pool.events if e.startswith("stuck:")],
          ["stuck: merger unit land PR #103 is still due, held: 6 failed session(s) of mrg:103 in all "
           "reached POOL_FAMILY_MAX (6)"])
    h.st = at("2", main="d" * 40)  # the worker pushed a new head
    started = h.step()
    check("a new head starts the family afresh: the rebase runs",
          ([u.kind for u in started], h.pool.book.family_starts["mrg:103"]), (["rebase"], 1))
    check("the old head's tries are gone", f"merge:103@{'1' * 12}@{'c' * 12}" in h.pool.book.tries, False)
    h.finish(started[0].key, "left to a merger (a hunk changes existing lines)")
    h.pool.book.merger_runs[103] = 3
    started = h.step()
    check("the merger loop still ends in a hand-back", [u.kind for u in started], ["handback"])
    h.finish(started[0].key, "handed back after repeated conflicts")
    check("the hand-back resets the family and the merger count",
          (h.pool.book.family_starts["mrg:103"], h.pool.book.merger_runs[103],
           any(k.startswith(("rebase:103@", "handback:103@")) for k in h.pool.book.tries)), (0, 0, False))
    h2 = Rig(state([iss(1, ["stage:pr"])], prs=[pr(11, 1)]), key_tries=1)
    u = h2.step()[0]
    h2.finish(u.key, "failed (exit 1)")
    h2.step()
    check("a key out of tries says so",
          [e for e in h2.pool.events if e.startswith("stuck:")],
          ["stuck: auto unit merge PR #11 is still due, held: 1 session(s) on this state reached POOL_KEY_TRIES (1)"])


def test_a_hold_spends_no_try() -> None:
    """Run 8: red-main holds counted as failures; PRs #82, #88 and #89 reached
    POOL_FAMILY_MAX and stayed stopped after main was green again."""
    h = Rig(state([iss(1, ["stage:pr"])], prs=[pr(11, 1)]), key_tries=2, family_max=3, hold_retry_s=60)
    for k in range(5):
        started = h.step()
        check(f"hold {k + 1}: the merge is due again", [u.number for u in started], [11])
        h.finish(started[0].key, "held (main dddddddd is red; the PR is not built on it)")
        check(f"hold {k + 1}: not before POOL_HOLD_RETRY_S", h.step(), [])
        h.now += 61
    check("no try, no family start spent, and nothing stuck",
          (h.pool.book.tries[started[0].key], h.pool.book.family_starts["mrg:11"],
           [e for e in h.pool.events if e.startswith("stuck:")]), (0, 0, []))


def test_a_handed_back_head_is_not_merged_again() -> None:
    """Run 9: PR #131 was handed back, and 2 s later a merge unit on the same head (main
    had moved) ran from a poll that still showed stage:pr: a second comment, and its issue
    counted as failed twice."""
    h = Rig(state([iss(1, ["stage:pr"])], prs=[pr(11, 1)], main="1" * 40), gh="true")
    started = h.step()
    w = next(w for w in h.pool.sessions.values() if w.unit.key == started[0].key)
    w.hand_back(11, 1, "**Not merged: it breaks main once merged.**")
    h.finish(started[0].key, "breaks main once merged (handed back)")
    h.st = state([iss(1, ["stage:pr"])], prs=[pr(11, 1)], main="2" * 40)
    check("main moved, the poll still says stage:pr: the same head is not merged again", h.step(), [])
    h.st = state([iss(1, ["stage:triage"])], prs=[pr(11, 1)], main="2" * 40)
    started = h.step()
    check("at stage:triage: a worker, as before", [(u.role, u.number) for u in started], [("worker", 1)])
    h.finish(started[0].key, "ok")
    h.st = state([iss(1, ["stage:pr"])], prs=[pr(11, 1)], main="3" * 40)
    check("moved back to stage:pr without a push: tried again", [u.number for u in h.step()], [11])
    h2 = Rig(state([iss(1, ["stage:pr"])], prs=[pr(11, 1)], main="1" * 40), gh="true")
    started = h2.step()
    w = next(w for w in h2.pool.sessions.values() if w.unit.key == started[0].key)
    w.hand_back(11, 1, "x")
    h2.finish(started[0].key, "breaks main once merged (handed back)")
    h2.st = state([iss(1, ["stage:pr"])], prs=[pr(11, 1, head="c" * 40)], main="1" * 40)
    check("a new head: merged as usual", [u.number for u in h2.step()], [11])


def test_no_merger_while_its_worker_runs() -> None:
    h = Rig(state([iss(1, ["stage:triage"])]))
    h.step()
    h.st = state([iss(1, ["stage:pr"])], prs=[pr(30, 1)])
    check("PR opened but its worker still finishing: no merge yet", h.step(), [])
    h.finish("wrk:1@2026-10-09T10:00:00Z")
    check(
        "the merge starts as soon as the worker ends",
        [u.role for u in h.step(0)],
        ["auto"],
    )


def test_clean_exit_never_restarts_on_a_stale_read() -> None:
    h = Rig(state([iss(1, ["stage:triage"]), iss(2, [])]))
    started = h.step()
    for u in started:
        h.finish(u.key)
    check("the same (stale) state after clean exits starts nothing", h.step(), [])
    h.st = state([iss(1, ["stage:triage"], updated="2026-10-09T12:00:00Z"), iss(2, [])])
    check(
        "a changed issue (released claim) is a new unit",
        keys(h.step()),
        ["wrk:1@2026-10-09T12:00:00Z"],
    )


def test_unclaimable_is_remembered() -> None:
    h = Rig(state([iss(1, ["stage:triage"])]))
    u = h.step()[0]
    h.finish(u.key, "unclaimable")
    check(
        "claim.py refused: not retried while the issue and main are unchanged",
        h.step(),
        [],
    )
    h.st = state([iss(1, ["stage:triage"])], main="e" * 40)
    check("a merge may have closed a native blocker: retried", len(h.step()), 1)
    h = Rig(state([iss(1, ["stage:triage"])]), unclaimable_retry_s=240)
    h.finish(h.step()[0].key, "unclaimable")
    check("nothing changed, within POOL_UNCLAIMABLE_RETRY_S: not retried", h.step(200), [])
    again = h.step(41)
    check("after it, retried (a refusal right after the issue was filed was GitHub's lag)",
          keys(again), ["wrk:1@2026-10-09T10:00:00Z"])
    h.finish(again[0].key, "unclaimable")
    check("refused again: held back for another while", h.step(60), [])


def test_ux_run_per_gap() -> None:
    h = Rig(state(main="1" * 40))
    check("POOL_UX off: no UX run", h.step(), [])
    h = Rig(state(main="1" * 40), ux=True, ux_gap_s=2700, ux_idle_gap_s=1e12)
    t1 = h.step()
    check(
        "no deploy command: a UX run on main's head",
        [(u.role, u.kind, u.info["sha"], u.info["deployed"]) for u in t1],
        [("tester", "ux", "1" * 40, False)],
    )
    check("and the tester is told so", "does not deploy" in ccpool.tester_prompt(t1[0], 8021, "30m"), True)
    h.finish(t1[0].key)
    h.st = state(main="2" * 40)
    check("a new sha within the gap starts none", h.step(60), [])
    t2 = h.step(2700)
    check(
        "after the gap, the newest sha gets the next run",
        [u.info["sha"] for u in t2],
        ["2" * 40],
    )
    check(
        "testers get distinct ports",
        (h.pool.take_port(), h.pool.take_port()),
        (8021, 8121),
    )
    h.pool.cfg.deploy_cmd = "./deploy.sh"
    check("with a deploy command, nothing deployed yet: no target", h.pool.ux_target(h.st), "")
    h.pool.own_deployed = "9" * 40
    check("then the last good deploy is the target", h.pool.ux_target(h.st), "9" * 40)


def test_ux_gap_settings() -> None:
    old = {k: os.environ.pop(k, None) for k in ("POOL_UX_GAP_MIN", "POOL_UX_GAP_S")}
    try:
        check("default 25 minutes", ccpool.ux_gap_s(), 1500)
        os.environ["POOL_UX_GAP_MIN"] = "10"
        check("POOL_UX_GAP_MIN in minutes", ccpool.ux_gap_s(), 600)
        os.environ["POOL_UX_GAP_S"] = "90"
        check("POOL_UX_GAP_S overrides it", ccpool.ux_gap_s(), 90)
        check("idle UX gap default 10 minutes", ccpool.gap_s("POOL_UX_IDLE_GAP", 10), 600)
        check("spec gap default 45 minutes", ccpool.Config.from_env().spec_gap_s, 2700)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_deploys_survive_a_restart() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-deploy-")) / "pool"
    d.mkdir()
    (d / "deploys.jsonl").write_text(
        json.dumps({"sha": "9" * 40, "ok": True}) + "\n" + json.dumps({"sha": "8" * 40, "ok": False}) + "\n"
    )
    h = Rig(state(main="8" * 40), logdir=d, ux=True, deploy_cmd="exit 1")
    started = h.step()
    check(
        "the last good deployment (not the failed one) gets the UX run",
        [u.info["sha"] for u in started],
        ["9" * 40],
    )
    check(
        "a sha this run already tried to deploy is not redeployed",
        h.pool.deploying,
        False,
    )


def test_reclaim_and_live() -> None:
    st = state(
        [
            iss(1, []),
            iss(3, ["stage:doing"], updated="2026-10-09T08:00:00Z"),
            iss(4, ["stage:live"]),
        ],
        [closed(5), closed(7, reason="NOT_PLANNED")],
    )
    h = Rig(st, live=True)
    h.now = -ccpool.age_s("2026-10-09T08:00:00Z", 0) + 2 * 3600
    got = sorted((u.role, u.kind, u.number) for u in h.step())
    check(
        "auto: stale reclaim; tester: stage:live; nothing for closed issues or the untriaged #1",
        got,
        [("auto", "reclaim", 3), ("tester", "live", 4)],
    )
    check(
        "the stale claim wakes the monitor",
        any("#3's claim went stale" in e for e in h.pool.events),
        True,
    )


def test_a_missing_env_file_is_noticed() -> None:
    """The shared checkout's .env disappeared; the live deploy and live checks stopped for
    hours and nobody was told."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-shared-"))
    old = os.environ.get("SHARED_REPO_PATH")
    os.environ["SHARED_REPO_PATH"] = str(root)
    try:
        h = Rig(state([iss(4, ["stage:live"])]), live=True, required_files=[".env"])
        started = h.step()
        check("no .env: the monitor hears it, and no live check starts",
              ([u.kind for u in started], [e for e in h.pool.events if "missing .env" in e] != []), ([], True))
        check("state.json carries it for the dashboard",
              json.loads((h.pool.cfg.logdir / "state.json").read_text())["missing_files"], [".env"])
        h.step()
        check("said once", sum("missing .env" in e for e in h.pool.events), 1)
        (root / ".env").write_text("KEY=1\n")
        started = h.step()
        check("back: the monitor hears it, and the live check starts",
              ([u.kind for u in started], any(".env is back" in e for e in h.pool.events)), (["live"], True))
    finally:
        if old is None:
            os.environ.pop("SHARED_REPO_PATH", None)
        else:
            os.environ["SHARED_REPO_PATH"] = old
    keep = {k: os.environ.pop(k, None) for k in ("POOL_LIVE", "POOL_REQUIRED_FILES")}
    try:
        check("live checks off: nothing required", ccpool.Config.from_env().required_files, [])
        os.environ["POOL_LIVE"] = "1"
        check("live checks on: .env", ccpool.Config.from_env().required_files, [".env"])
        os.environ["POOL_REQUIRED_FILES"] = ".env, secrets/key.json"
        check("POOL_REQUIRED_FILES names them", ccpool.Config.from_env().required_files, [".env", "secrets/key.json"])
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_live_prompt_says_how_to_report_a_check_that_could_not_run() -> None:
    t = ccpool.tester_prompt(ccpool.Unit("tester", "live", "k", "f", 4, (2,), {"title": "x"}), 8021, "60m")
    check("the live check prompt names live-check-blocked", "live-check-blocked:" in t, True)


def test_live_off() -> None:
    st = state(
        [iss(4, ["stage:live"]), iss(6, ["stage:pr", "needs:live"])],
        prs=[pr(16, 6)],
    )
    h = Rig(st)
    got = h.step()
    check("POOL_LIVE off: no live check, the merge still runs", [(u.kind, u.number) for u in got],
          [("merge", 16)])
    check("and the merge does not route to stage:live", got[0].info["needs_live"], False)
    claim = {"issue": 6, "token": "t", "from": "stage:triage", "title": "x", "needs_live": True}
    check("nor is the worker told to write Refs", "Refs #6" in ccpool.worker_prompt(claim, "1", "45m", "u"), False)
    check("with POOL_LIVE on it is", "Refs #6" in ccpool.worker_prompt(claim, "1", "45m", "u", live=True), True)


def test_crash_breaker() -> None:
    h = Rig(
        state([iss(n, ["stage:triage"]) for n in range(1, 5)]),
        crash_max=3,
        crash_pause_s=60,
        caps={"worker": 1, "merger": 0, "tester": 0},
    )
    for _ in range(3):
        u = h.step()[0]
        h.finish(u.key, "startup crash (exit 1)")
    check("three startup crashes in a row pause the role", h.step(), [])
    check(
        "and the monitor hears it",
        any("died at startup" in e for e in h.pool.events),
        True,
    )
    check("the pause ends by itself", len(h.step(61)), 1)


def test_idle_and_drained_events() -> None:
    h = Rig(state([iss(1, ["stage:owner"])]))
    h.step()
    check(
        "open issues and nothing to do is reported once",
        sum("pipeline idle" in e for e in h.pool.events),
        1,
    )
    h.step()
    check(
        "not repeated while it stays idle",
        sum("pipeline idle" in e for e in h.pool.events),
        1,
    )
    h.st = state([], [closed(1, ["stage:verified"])])
    h.pool.idle_flagged = False
    h.step()
    check(
        "an empty backlog is reported",
        any("backlog empty" in e for e in h.pool.events),
        True,
    )


def test_owner_questions_wait_for_the_owner() -> None:
    """The monitor settled two stage:owner questions from the spec before the owner did."""
    h = Rig(state([iss(124, ["stage:doing"])]))
    h.step()
    h.st = state([iss(124, ["stage:owner"])])
    h.step()
    h.st = state([{**iss(124, ["stage:owner"]), "comments": 3}])
    h.step()
    owner = [e for e in h.pool.events if e.startswith("#124") and "stage:owner" in e]
    check("the monitor is told the question is the owner's, and whose comment to act on",
          ("do not answer it yourself" in owner[0], "a comment with no role marker" in owner[1]), (True, True))
    check("and so is every queue update", "never the answer" in ccpool.FEED_HEADER, True)


FAKE_GH_PAGES = r"""#!/usr/bin/env python3
import json, os, sys
args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a and not a.startswith("query="))
with open(os.environ["FAKE_PAGES_LOG"], "a") as f:
    f.write(json.dumps({k: args.get(k) for k in ("ia", "pa", "first")}) + "\n")
first = args.get("first") == "true"
if not first and os.environ.get("FAKE_PAGES_FAIL"):
    print("HTTP 502", file=sys.stderr); sys.exit(1)
def page(items, after, size):
    start = int(after) if after not in (None, "null") else 0
    chunk = items[start:start + size]
    end = start + len(chunk)
    return {"pageInfo": {"hasNextPage": end < len(items), "endCursor": str(end) if chunk else after},
            "nodes": chunk}
lab = lambda names: {"nodes": [{"name": n} for n in names]}
issues = []
for n in range(1, 151):
    body = {150: "Blocked by: #3", 2: "Blocked by: #149"}.get(n, "")
    labels = ["stage:pr"] if n == 140 else ["stage:triage", "P1"]
    issues.append({"number": n, "title": f"issue {n}", "body": body, "updatedAt": "2026-10-09T10:00:00Z",
                   "labels": lab(labels), "comments": {"totalCount": 0}})
prs = [{"number": 1000 + k, "headRefName": f"issue-{200 + k}", "headRefOid": "b" * 40, "body": "",
        "isDraft": False, "mergeable": "MERGEABLE"} for k in range(69)]
prs.append({"number": 1140, "headRefName": "issue-140", "headRefOid": "c" * 40, "body": "Fixes #140",
            "isDraft": False, "mergeable": "MERGEABLE"})
repo = {"open": page(issues, args.get("ia"), 100), "pullRequests": page(prs, args.get("pa"), 60)}
if first:
    repo["closed"] = {"nodes": []}
    repo["main"] = {"target": {"oid": "a" * 40}}
print(json.dumps({"data": {"repository": repo}}))
"""


def test_fetch_reads_every_page() -> None:
    """Open issues past the first 100 and open PRs past the first 60 were never read."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-pages-"))
    gh = d / "gh"
    gh.write_text(FAKE_GH_PAGES)
    gh.chmod(0o755)
    os.environ["FAKE_PAGES_LOG"] = str(d / "calls")
    try:
        cfg = ccpool.Config(logdir=d / "pool", gh=str(gh), repo="o/r", caps={r: 0 for r in ccpool.ROLES})
        st = ccpool.fetch(cfg)
        calls = [json.loads(x) for x in (d / "calls").read_text().splitlines()]
        check("150 open issues and 70 open PRs, each once",
              (len(st["open"]), len({i["number"] for i in st["open"]}), len(st["prs"]),
               len({p["number"] for p in st["prs"]})), (150, 150, 70, 70))
        check("two pages; the second asks after each cursor; closed and main come with the first",
              calls, [{"ia": "null", "pa": "null", "first": "true"}, {"ia": "100", "pa": "60", "first": "false"}])
        check("main from the first page", st["main"], "a" * 40)
        check("the queue counts every open issue", ccpool.queue_snapshot(st)["open"], 150)
        due, _ = ccpool.plan(st, ccpool.Book(), cfg, NOW)
        workers = {u.number for u in due if u.role == "worker"}
        check("a Blocked by: across pages holds both ways (#150 by #3, #2 by #149)",
              (150 in workers, 2 in workers, 149 in workers, 3 in workers), (False, False, True, True))
        check("a PR on the second page, for an issue on the second page, is merged",
              [u.number for u in due if u.kind == "merge"], [1140])
        os.environ["FAKE_PAGES_FAIL"] = "1"
        try:
            ccpool.fetch(cfg)
            failed = False
        except RuntimeError:
            failed = True
        check("a page that fails fails the whole read: nothing is planned from a partial queue", failed, True)
        h = Rig(state(), gh=str(gh), repo="o/r")
        h.pool.fetcher = lambda: ccpool.fetch(h.pool.cfg)
        try:
            h.step()
            stepped = True
        except RuntimeError:
            stepped = False
        check("and the poll fails before planning (run() logs it as poll_error)",
              (stepped, (h.pool.cfg.logdir / "state.json").exists()), (False, False))
    finally:
        os.environ.pop("FAKE_PAGES_LOG", None)
        os.environ.pop("FAKE_PAGES_FAIL", None)


def test_old_holds_are_pointed_out() -> None:
    """The monitor kept its own hold:monitor on #54 for 3 h 40 min after its reason passed."""
    h = Rig(state([iss(54, ["stage:triage", "hold:monitor"]), iss(55, ["stage:triage", "hold:monitor"])]),
            hold_review_s=3600)
    h.step()
    check("a new hold: nothing said yet", [e for e in h.pool.events if "hold:monitor for" in e], [])
    h.st = state([iss(54, ["stage:triage", "hold:monitor"]), iss(55, ["stage:triage"])])
    h.step(3600)
    check("an hour later: the monitor is reminded once of the hold still there",
          [e.split(":")[0] for e in h.pool.events if "hold:monitor for" in e], ["#54 has been at hold"])
    h.step(600)
    check("once", sum("hold:monitor for" in e for e in h.pool.events), 1)
    check("and every queue update lists it", "Your holds older than 60 min: #54 (70 min)" in h.pool.summary(h.st),
          True)
    check("a lifted hold is forgotten", sorted(h.pool.book.holds), [54])


FAKE_GH_ISSUE = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(" ".join(a.replace("\n", "\\n") for a in sys.argv[1:]) + "\n")
if sys.argv[1:3] == ["issue", "view"]:
    view = os.environ.get("FAKE_GH_VIEW", "")
    if not view:
        sys.exit(1)
    print(view)
"""


def test_live_checks_settle_themselves() -> None:
    """The supervisor acts on the tester's verdict; the monitor closed 24 issues by
    hand in one run, a turn each."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-live-"))
    gh = d / "gh"
    gh.write_text(FAKE_GH_ISSUE)
    gh.chmod(0o755)
    loop = {"login": "loop-bot"}
    since = "2026-10-23T12:00:00Z"
    unit = ccpool.Unit("tester", "live", "live:9@x", "tst:9", 9, (0, 9), {"since": since, "title": "places"})

    def settle(comments, labels=("stage:live", "needs:live"), state_="OPEN", view=True):
        log = d / "gh.log"
        log.write_text("")
        pool = ccpool.Pool(ccpool.Config(logdir=d / "pool", gh=str(gh)), fetcher=state, spawn=False)
        os.environ.update(FAKE_GH_LOG=str(log), ISSUELOOPER_LOGIN="loop-bot", FAKE_GH_VIEW=json.dumps(
            {"state": state_, "labels": [{"name": x} for x in labels], "comments": comments}) if view else "")
        try:
            pool.settle_live(unit)
        finally:
            for k in ("FAKE_GH_LOG", "ISSUELOOPER_LOGIN", "FAKE_GH_VIEW"):
                os.environ.pop(k, None)
        return [ln for ln in log.read_text().splitlines() if not ln.startswith("issue view")], pool.events

    def tester(body, at="2026-10-23T12:30:00Z", author=loop):
        return {"author": author, "body": "<!-- tester -->\n" + body, "createdAt": at, "url": "https://x/c/1"}

    calls, events = settle([tester("Saw Otaru cafes.\nlive-verified: the app in live mode 2026-10-23")])
    check("a live-verified line closes the issue",
          [c.split(" --comment")[0] for c in calls],
          ["issue edit 9 --remove-label stage:live", "issue close 9 --reason completed"])
    check("and tells the monitor", events, ["#9: the live check passed; the supervisor closed it."])
    calls, events = settle([tester("live-check-blocked: .env is missing")])
    check("live-check-blocked goes to the owner",
          calls[0], "issue edit 9 --remove-label stage:live --add-label stage:owner")
    calls, events = settle([tester("Still Shibuya after setting Otaru.")])
    check("a first failure goes back to a worker",
          (calls[0], ccpool.LIVE_HANDBACK in calls[1]),
          ("issue edit 9 --remove-label stage:live --add-label stage:triage", True))
    first = {"author": loop, "body": f"<!-- merger -->\n{ccpool.LIVE_HANDBACK}\nBack", "createdAt": "2026-10-23T10:00:00Z"}
    calls, events = settle([first, tester("Still Shibuya.")])
    check("a second failure goes to the owner",
          calls[0], "issue edit 9 --remove-label stage:live --add-label stage:owner")
    ask = "live check of #9 finished: read the tester's comment"
    for what, comments, kw in (
        ("a verdict older than this entry into stage:live", [tester("live-verified: x", at="2026-10-23T11:00:00Z")], {}),
        ("a stranger's comment, marker and all", [tester("live-verified: x", author={"login": "someone"})], {}),
        ("gh that cannot read the issue", [], {"view": False}),
    ):
        calls, events = settle(comments, **kw)
        check(f"{what}: nothing changes and the monitor is asked", (calls, events[0].startswith(ask)), ([], True))
    calls, events = settle([tester("live-verified: x")], labels=("needs:live",))
    check("an issue already off stage:live is left alone", (calls, events), ([], []))
    # A GitHub App posts as `<slug>[bot]` in REST (ISSUELOOPER_LOGIN) and as `<slug>`
    # in GraphQL (gh issue view): the same account.
    log = d / "gh.log"
    log.write_text("")
    pool = ccpool.Pool(ccpool.Config(logdir=d / "pool", gh=str(gh)), fetcher=state, spawn=False)
    os.environ.update(FAKE_GH_LOG=str(log), ISSUELOOPER_LOGIN="Loop-Bot[bot]", FAKE_GH_VIEW=json.dumps(
        {"state": "OPEN", "labels": [{"name": "stage:live"}], "comments": [tester("live-verified: x")]}))
    claimlib_login = ccpool.claimlib._LOGIN[:]
    ccpool.claimlib._LOGIN.clear()
    try:
        pool.settle_live(unit)
    finally:
        for k in ("FAKE_GH_LOG", "ISSUELOOPER_LOGIN", "FAKE_GH_VIEW"):
            os.environ.pop(k, None)
        ccpool.claimlib._LOGIN[:] = claimlib_login
    check("a bot's two spellings of its login are one account",
          [ln.split(" --comment")[0] for ln in log.read_text().splitlines() if not ln.startswith("issue view")],
          ["issue edit 9 --remove-label stage:live", "issue close 9 --reason completed"])


def test_long_dependency_waits_are_pointed_out() -> None:
    """One P1 fix waited 6.7 hours on three dependencies and nobody looked at them again."""
    blocked = dict(iss(238, ["stage:triage", "P1"]), blocked_by=3)
    low = dict(iss(240, ["stage:triage", "P2"]), blocked_by=1)
    h = Rig(state([blocked, low]), blocked_review_s=3600)
    h.step()
    check("a new wait: nothing said", [e for e in h.pool.events if "open blocker" in e], [])
    h.step(3600)
    said = [e for e in h.pool.events if "open blocker" in e]
    check("an hour later the monitor is told once, about the P1 only",
          [e.split(" has")[0] for e in said], ["#238 (P1)"])
    h.step(600)
    check("once", sum("open blocker" in e for e in h.pool.events), 1)
    check("and every update lists it", "P0/P1 issues blocked over 60 min: #238 (70 min)" in h.pool.summary(h.st), True)
    h.st = state([iss(238, ["stage:triage", "P1"]), low])
    h.step()
    check("an unblocked issue is forgotten", sorted(h.pool.book.blocked), [])


def test_monitor_is_asked_to_split_an_issue_that_keeps_failing() -> None:
    """The monitor may split any issue, and in a 12-hour run split none; one took eleven
    worker sessions. Only the issue's own failures count: all seven requests in run 7
    counted claim failures, sessions that delivered a PR, and gate 7 or merger-loop
    hand-backs."""
    h = Rig(state([iss(9, ["stage:triage", "P0"]), iss(10, ["stage:pr", "P1"]),
                   iss(11, ["stage:triage", "P1"], body="<!-- monitor -->\n<!-- part of #9 -->\nPart"),
                   iss(12, ["stage:triage", "P1"])]))
    seq = iter(range(1, 100))

    def worker(n: int, outcome: str, claimed: bool = True, left_pr: bool = False) -> None:
        w = ccpool.Session(h.pool, ccpool.Unit("worker", "implement", f"wrk:{n}@{next(seq)}", f"wrk:{n}", n, (0, n)),
                           next(seq))
        w.dir.mkdir(parents=True)
        if claimed:
            (w.dir / "claim").write_text("{}")
        w.left_pr = left_pr
        h.pool.finished(w, outcome)

    for n in (9, 10, 11, 12):
        worker(n, "ok")
    h.step()
    check("one failed try is not enough to ask", [e for e in h.pool.events if "has failed" in e], [])
    for n in (9, 10, 11):
        worker(n, "timed out after 45m")
    for n, outcome, claimed, left_pr in ((12, "claim failed (exit 1)", False, False), (12, "ok", True, True),
                                         (12, "quota error (exit 3)", True, False),
                                         (12, "stopped with the supervisor", True, False)):
        worker(n, outcome, claimed, left_pr)
    h.step()
    said = [e for e in h.pool.events if "has failed" in e]
    check("after two, the monitor is asked about the claimable issue only (not one at stage:pr, not a part); "
          "a claim failure, a PR delivered, quota and a stop do not count",
          [e.split(":")[0] for e in said], ["#9 has failed 2 times and is not merged"])
    h.pool.failed_try(12)
    h.step()
    check("a hand-back for the issue's own failure counts", sum("#12 has failed 2 times" in e for e in h.pool.events), 1)
    h.step()
    check("once", sum("has failed" in e for e in h.pool.events), 2)
    check("a closed issue is forgotten", sorted(h.pool.book.failed_tries), [9, 10, 11, 12])
    off = Rig(state([iss(9, ["stage:triage"])]), decompose_after=0)
    off.pool.book.failed_tries[9] = 5
    off.step()
    check("POOL_DECOMPOSE_AFTER=0: never asked", [e for e in off.pool.events if "has failed" in e], [])
    # The review gate's reject of a diff it saw only in part is not the issue's failure.
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-reviewcut-"))
    old = os.environ.get("REVIEW_DIR")
    os.environ["REVIEW_DIR"] = str(d)
    try:
        (d / ("c" * 40 + ".json")).write_text(json.dumps({"verdict": "reject", "left_out": ["src/live.py (cut)"]}))
        (d / ("d" * 40 + ".json")).write_text(json.dumps({"verdict": "reject", "left_out": ["tests/test_a.py (cut)"]}))
        check("a production file cut is a review cut; a test cut is not",
              (ccpool.review_cut("c" * 40), ccpool.review_cut("d" * 40), ccpool.review_cut("e" * 40)), (True, False, False))
    finally:
        if old is None:
            os.environ.pop("REVIEW_DIR", None)
        else:
            os.environ["REVIEW_DIR"] = old


def test_new_ux_proposals_go_to_the_monitor_to_screen() -> None:
    """The owner rejected 94 of 127 proposals in one run, 59 as duplicates."""
    h = Rig(state([iss(1, ["stage:triage"])]))
    h.step()
    h.st = state([iss(1, ["stage:triage"]), iss(300, ["ux:proposed", "type:bug"], title="UX: near here")])
    h.step()
    check("a new proposal is named for screening, not for triage",
          ([e for e in h.pool.events if "#300" in e]),
          ["new UX proposal #300: UX: near here. Screen it before the owner reads it "
           "(issuelooper-monitor skill, Screen UX proposals)"])
    h.step()
    check("once", sum("#300" in e for e in h.pool.events), 1)
    off = Rig(state([]), ux_screen=False)
    off.step()
    off.st = state([iss(300, ["ux:proposed"])])
    off.step()
    check("POOL_UX_SCREEN=0: not named", [e for e in off.pool.events if "#300" in e], [])


def test_signature_moves_with_the_queue() -> None:
    a = state([iss(1, ["stage:triage"])])
    b = state([iss(1, ["stage:doing"])])
    check(
        "same state, same signature",
        ccpool.signature(a),
        ccpool.signature(json.loads(json.dumps(a))),
    )
    check("a label change moves it", ccpool.signature(a) != ccpool.signature(b), True)


def test_normalize_graphql() -> None:
    data = {
        "data": {
            "repository": {
                "open": {
                    "nodes": [
                        {
                            "number": 1,
                            "title": "t",
                            "body": None,
                            "updatedAt": "u",
                            "labels": {
                                "nodes": [{"name": "stage:triage"}, {"name": "P1"}]
                            },
                            "issueDependenciesSummary": {"blockedBy": 2},
                        }
                    ]
                },
                "closed": {
                    "nodes": [
                        {
                            "number": 2,
                            "title": "c",
                            "closedAt": "x",
                            "stateReason": "COMPLETED",
                            "labels": {"nodes": []},
                        }
                    ]
                },
                "pullRequests": {
                    "nodes": [
                        {
                            "number": 3,
                            "headRefName": "issue-1",
                            "headRefOid": "h",
                            "body": "",
                            "isDraft": False,
                            "mergeable": "MERGEABLE",
                        }
                    ]
                },
                "main": {"target": {"oid": "m"}},
            }
        }
    }
    st = ccpool.normalize(data)
    check("normalize: labels sorted", st["open"][0]["labels"], ["P1", "stage:triage"])
    check("normalize: PR to issue by branch", ccpool.pr_issue(st["prs"][0]), 1)
    check(
        "normalize: PR to issue by body",
        ccpool.pr_issue({"branch": "x", "body": "Refs #7"}),
        7,
    )
    check("normalize: main", st["main"], "m")
    check("normalize: open GitHub blockers", st["open"][0]["blocked_by"], 2)
    moved = {**st, "open": [{**st["open"][0], "blocked_by": 0}]}
    check("a blocker closing moves the signature", ccpool.signature(st) != ccpool.signature(moved), True)


class FakeBroker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def test_monitor_feed_only_when_idle() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-broker-"))
    sock = str(d / "broker.sock")
    said, status = [], {"busy": False, "done": 0}

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            req = json.loads(self.rfile.readline())
            if req["cmd"] == "say":
                said.append(req)
                resp = {"turn": len(said)}
            else:
                resp = dict(status)
            self.wfile.write((json.dumps(resp) + "\n").encode())

    srv = FakeBroker(sock, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    h = Rig(state([iss(1, ["stage:triage"])]), broker_sock=sock)
    h.step()
    h.st = state([iss(1, ["stage:triage"]), iss(2, [], title="new bug")])
    h.step()
    check("before the owner's kickoff (no finished turn) nothing is sent", said, [])
    status.update(done=1, busy=True)
    h.step()
    check("while the monitor is busy nothing is sent", said, [])
    status.update(busy=False)
    h.step()
    check("idle: one message", len(said), 1)
    check(
        "it carries the event and the queue summary",
        ("new issue #2: new bug" in said[0]["text"], "Queue:" in said[0]["text"]),
        (True, True),
    )
    check("tagged as the pool's feed", said[0]["tag"], "pool_feed")
    h.step()
    check("no change, no message", len(said), 1)
    h.st = state([iss(2, [], title="new bug")], [closed(1)], main="9" * 40)
    h.step()
    check(
        "a closure and a merge wake it again",
        ("#1 closed" in said[-1]["text"], "main moved" in said[-1]["text"]),
        (True, True),
    )
    check(
        "the untriaged issue is named as the monitor's",
        "Untriaged (yours): #2" in said[-1]["text"],
        True,
    )
    srv.shutdown()
    srv.server_close()


FAKE_HERDR = r"""#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_HERDR_STATE"]
st = json.load(open(path))
args = sys.argv[1:]
if args[:2] == ["agent", "get"]:
    if st["status"] == "gone":
        sys.stderr.write(json.dumps({"error": {"message": "no agent in pane"}}))
        sys.exit(1)
    print(json.dumps({"result": {"agent": {"agent_status": st["status"], "pane_id": args[2]}}}))
elif args[:2] == ["agent", "read"]:
    snaps = st.get("snaps") or ["> "]
    print(snaps.pop(0) if len(snaps) > 1 else snaps[0])
    st["snaps"] = snaps
elif args[:2] == ["agent", "prompt"]:
    st["prompts"].append({"target": args[2], "text": args[3]})
    print(json.dumps({"result": {}}))
json.dump(st, open(path, "w"))
"""


def kinds(h) -> list[str]:
    path = h.pool.cfg.logdir / "pool.jsonl"
    return [json.loads(x)["kind"] for x in path.read_text().splitlines()] if path.exists() else []


def test_monitor_feed_through_herdr() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-herdr-"))
    fake = d / "herdr"
    fake.write_text(FAKE_HERDR)
    fake.chmod(0o755)
    statefile = d / "state.json"

    def set_state(**kw):
        cur = json.loads(statefile.read_text()) if statefile.exists() else {"prompts": []}
        cur.update(kw)
        statefile.write_text(json.dumps(cur))

    def prompts():
        return json.loads(statefile.read_text())["prompts"]

    old = {k: os.environ.get(k) for k in ("HERDR_BIN", "FAKE_HERDR_STATE")}
    os.environ.update(HERDR_BIN=str(fake), FAKE_HERDR_STATE=str(statefile))
    try:
        set_state(status="working")
        h = Rig(state([iss(1, ["stage:triage"])]), herdr_monitor="w1:p1", herdr_quiet_s=0.01,
                broker_sock="/nonexistent.sock")
        h.step()
        h.st = state([iss(1, ["stage:triage"]), iss(2, [], title="new bug")])
        h.step()
        check("herdr wins over the broker socket; a working monitor gets nothing", prompts(), [])
        set_state(status="idle", snaps=["> half-typed", "> half-typed more"])
        h.step()
        check("idle, but the owner is typing in the pane: nothing yet", prompts(), [])
        set_state(snaps=["> "])
        h.step()
        sent = prompts()
        check("idle and quiet: one prompt to the monitor's pane",
              [(m["target"], "new issue #2: new bug" in m["text"]) for m in sent], [("w1:p1", True)])
        set_state(status="blocked")
        h.st = state([iss(2, [], title="new bug")], [closed(1)])
        h.step()
        check("a monitor at a question dialog gets nothing", len(prompts()), 1)
        check("and the pool log says so", "monitor_blocked" in kinds(h), True)
        set_state(status="gone")
        h.step()
        check("an exited monitor is logged too", "monitor_gone" in kinds(h), True)
        set_state(status="done")
        h.step()
        check("back to idle (done): the queued events go out", len(prompts()), 2)
        check("nothing was lost", "#1 closed" in prompts()[-1]["text"], True)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_state_for_the_dashboard() -> None:
    st = state(
        [iss(1, []), iss(2, ["stage:triage", "P1"]), iss(3, ["stage:owner"], title="pick a color"),
         iss(4, ["stage:pr", "hold:monitor"]), iss(5, ["ux:proposed"])],
        prs=[pr(9, 4), pr(10, 4, mergeable="CONFLICTING")],
    )
    q = ccpool.queue_snapshot(st)
    check("counts per stage, holds and proposals", q["stages"],
          {"untriaged": 1, "stage:triage": 1, "stage:owner": 1, "stage:pr": 1, "hold:monitor": 1, "ux:proposed": 1})
    check("the owner's decisions are listed", [(o["number"], o["title"]) for o in q["owner"]], [(3, "pick a color")])
    check("pull requests by mergeability", q["prs"], {"MERGEABLE": 1, "CONFLICTING": 1})
    h = Rig(st)
    started = h.step()
    data = json.loads((h.pool.cfg.logdir / "state.json").read_text())
    check("state.json carries the queue and the running sessions",
          (data["queue"]["open"], sorted(x["number"] for x in data["sessions"] if x["role"] == "worker")),
          (5, [2]))
    check("a queue record is logged when the counts change", kinds(h).count("queue"), 1)
    h.step()
    check("and not again while they stay", kinds(h).count("queue"), 1)
    w = next(w for w in h.pool.sessions.values() if w.unit.role == "worker")
    (w.dir).mkdir(parents=True, exist_ok=True)
    (w.dir / "session.jsonl").write_text(json.dumps(
        {"event": "result", "result": {"usage": {"input_tokens": 7, "output_tokens": 3}}}) + "\n")
    h.finish(w.unit.key)
    end = [json.loads(x) for x in (h.pool.cfg.logdir / "pool.jsonl").read_text().splitlines()
           if json.loads(x)["kind"] == "end"][-1]
    check("the end record names the issue and the tokens",
          (end["number"], end["usage"]["input"], end["usage"]["output"], end["cli"]), (2, 7, 3, "agy"))


def test_deploy_safety_net() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-netdeploy-"))
    st = state([iss(1, ["stage:pr"])], prs=[pr(5, 1, mergeable="CONFLICTING")], main="f" * 40)
    rebased = f"rebase:5@{'b' * 12}@{'f' * 12}"
    h0 = Rig(st)
    h0.pool.book.tries[rebased] = 1
    h0.step()
    h0.finish(h0.running("merger")[0])
    h0.step()
    check("no POOL_DEPLOY_CMD: no deploys", (h0.pool.deploying, h0.pool.deploy_tried), (False, set()))
    h = Rig(st, deploy_grace_s=300, deploy_cmd=f'echo "deployed $DEPLOY_SHA" >> {d}/calls')
    h.pool.book.tries[rebased] = 1  # the supervisor's rebase already failed
    h.step()
    check(
        "main undeployed but a merger session is running: wait for its merge",
        (d / "calls").exists(),
        False,
    )
    h.finish(h.running("merger")[0])
    h.step()
    for _ in range(50):
        if not h.pool.deploying and (d / "calls").exists():
            break
        time.sleep(0.1)
    check(
        "no merger running: the supervisor runs the deploy command with DEPLOY_SHA",
        (d / "calls").read_text(),
        f"deployed {'f' * 40}\n",
    )
    check("and remembers it", h.pool.own_deployed, "f" * 40)
    check(
        "in the run's deploy log",
        json.loads((h.dir / "pool" / "deploys.jsonl").read_text())["ok"],
        True,
    )
    h.step()
    time.sleep(0.3)
    check("never twice for one sha", (d / "calls").read_text().count("deployed"), 1)
    h2 = Rig(state(main="e" * 40), deploy_cmd="echo broken >&2; exit 3")
    h2.step()
    for _ in range(50):
        if not h2.pool.deploying:
            break
        time.sleep(0.1)
    check(
        "a failed deploy tells the monitor",
        any("deploy of eeeeeeeeeeee FAILED" in e and "broken" in e for e in h2.pool.events),
        True,
    )


def test_deploy_waits_for_mergers_at_most_the_grace() -> None:
    """Each merger deferred the deploy again as main kept moving: 24 minutes without one."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-gracedeploy-"))
    h = Rig(state(main="1" * 40), deploy_grace_s=300, deploy_cmd=f'echo "$DEPLOY_SHA" >> {d}/calls')
    h.pool.book.running["mrg:5"] = ccpool.Unit("merger", "land", "mrg:5@x", "mrg:5", 5, (0, 5), {})
    deployed_at = None
    for k in range(1, 9):  # main moves every 60 s; a merger runs all along
        h.st = state(main=str(k) * 40)
        h.step(60)
        if h.pool.deploy_tried and deployed_at is None:
            deployed_at = k
    check("deployed within the grace of the first undeployed main, mergers or not",
          (deployed_at, sorted(h.pool.deploy_tried)), (6, ["6" * 40]))


def test_deploy_retries_a_ref_lock() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-lockdeploy-"))
    # Fails once with git's ref-lock error, then deploys.
    cmd = (f"echo try >> {d}/calls; if [ $(wc -l < {d}/calls) -lt 2 ]; then "
           "echo \"error: cannot lock ref 'refs/remotes/origin/main': is at a but expected b\" >&2; exit 1; fi; "
           "echo deployed")
    pause = ccpool.DEPLOY_LOCK_PAUSE_S
    ccpool.DEPLOY_LOCK_PAUSE_S = 0
    try:
        h = Rig(state(main="e" * 40), deploy_cmd=cmd)
        h.pool.deploying = True
        h.pool.deploy("e" * 40)
        check("a ref-lock failure is tried again", (d / "calls").read_text().count("try"), 2)
        check("and the retry counts", h.pool.own_deployed, "e" * 40)
        (d / "calls").write_text("")
        h.pool.cfg.deploy_cmd = f"echo try >> {d}/calls; echo broken >&2; exit 1"
        h.pool.deploy("c" * 40)
        check("any other failure is not", (d / "calls").read_text().count("try"), 1)
        check("and the monitor hears where production stays",
              [e for e in h.pool.events if "FAILED" in e and "production stays on eeeeeeeeeeee" in e].__len__(), 1)
    finally:
        ccpool.DEPLOY_LOCK_PAUSE_S = pause


def test_live_rechecked_on_each_entry() -> None:
    """A re-entry into stage:live after a hand-back or a reopen gets a new check (a key
    of live:{n} alone ran once per supervisor)."""
    before = len(FAILURES)
    t = lambda hm: f"2026-10-09T{hm}:00Z"  # noqa: E731
    h = Rig(state([iss(4, ["stage:live"], updated=t("10:00"))]), live=True)
    first = h.step()
    check(
        "a stage:live issue gets a live check",
        [(u.kind, u.number) for u in first],
        [("live", 4)],
    )
    h.finish(first[0].key)
    check("not again for the same entry", h.step(), [])
    h.st = state([iss(4, ["stage:live"], updated=t("10:05"))])
    check("a comment (the tester's own) is no new entry", h.step(), [])
    h.st = state([iss(4, ["stage:triage"], updated=t("10:10"))])
    for u in h.step():
        h.finish(u.key)
    h.st = state([iss(4, ["stage:live"], updated=t("11:00"))], main="c" * 40)
    again = h.step()
    check(
        "handed back, reworked and merged again: checked again",
        [(u.kind, u.number) for u in again],
        [("live", 4)],
    )
    check(
        "the tester is told which entry counts",
        t("11:00") in ccpool.tester_prompt(again[0], 8021, "30m"),
        True,
    )
    h.finish(again[0].key)
    h.st = state([iss(4, ["stage:pr"], updated=t("10:30"))], main="c" * 40)
    h.step()
    h.st = state([iss(4, ["stage:live"], updated=t("11:20"))], main="d" * 40)
    check(
        "a stale read that drops the label, and a merge, start no second check",
        h.step(),
        [],
    )

    h2 = Rig(state([iss(5, ["stage:live"])]), live=True)
    h2.finish(h2.step()[0].key, "timed out after 30m")
    h2.finish(h2.step()[0].key, "failed (exit 1)")
    check("two unfinished checks: no third at the same main", h2.step(), [])
    check(
        "the monitor hears it is stuck",
        any("stuck: tester unit live #5" in e for e in h2.pool.events),
        True,
    )
    h2.st = state([iss(5, ["stage:live"])], main="c" * 40)
    check(
        "a check that never finished is tried again once main moves",
        [u.kind for u in h2.step()],
        ["live"],
    )
    assert len(FAILURES) == before, FAILURES[before:]


def test_ux_proposed_is_left_alone() -> None:
    """Issues labeled ux:proposed are proposals waiting for the owner."""
    before = len(FAILURES)
    h = Rig(state([iss(1, ["stage:triage"])]))
    for u in h.step():
        h.finish(u.key)
    ux = [
        iss(7, ["sev:minor", "ux:proposed"], title="UX: tiny"),
        iss(8, ["P1", "stage:triage", "ux:proposed"]),
    ]
    h.st = state([iss(1, ["stage:triage"])] + ux, [closed(9, ["ux:proposed"])])
    check("no worker, merge or tester for ux:proposed issues", h.step(), [])
    check(
        "the monitor is not asked to triage them, only to screen the new ones",
        [e.split(":")[0] for e in h.pool.events if any(f"#{n}" in e for n in (7, 8, 9))],
        ["new UX proposal #7", "new UX proposal #8"],
    )
    check(
        "not listed as untriaged",
        "Untriaged (yours): none" in h.pool.summary(h.st),
        True,
    )
    h.st = state(
        [
            iss(1, ["stage:triage"]),
            iss(7, ["P1", "ux:accepted"], updated="2026-10-09T12:00:00Z"),
            ux[1],
        ],
        [closed(9, ["ux:proposed"])],
    )
    h.step()
    check(
        "the owner's acceptance reaches the monitor",
        any("#7 is no longer ux:proposed" in e for e in h.pool.events),
        True,
    )
    check(
        "ux:proposed in the tester's rules",
        "ux:proposed"
        in ccpool.tester_prompt(
            ccpool.Unit(
                "tester", "ux", "ux:x", "tst:ux", None, (2, 0, 0), {"sha": MAIN}
            ),
            8021,
            "30m",
        ),
        True,
    )
    assert len(FAILURES) == before, FAILURES[before:]


FAKE_CCROLE_429 = r"""#!/usr/bin/env python3
import json, os, sys
calls = os.environ["FAKE_CALLS"]
with open(calls, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
n = len(open(calls).read().splitlines())
print(json.dumps({"event": "init", "conversation_id": "conv-1"}))
if n <= int(os.environ["FAKE_429_TIMES"]):
    print(json.dumps({"event": "result", "result": {"conversation_id": "conv-1", "status": "ERROR",
          "response": "", "error": os.environ.get("FAKE_429_TEXT",
          "API error (attempt 1): RESOURCE_EXHAUSTED (code 429): TPM quota exhausted.")}}))
    sys.exit(1)
print(json.dumps({"event": "result", "result": {"response": "done"}}))
"""

# Claude Code's documented stream-json shape; not recorded from a live run.
FAKE_CCROLE_429_CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys
calls = os.environ["FAKE_CALLS"]
with open(calls, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
n = len(open(calls).read().splitlines())
print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "m"}))
if n <= int(os.environ["FAKE_429_TIMES"]):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "session_id": "sess-1",
          "result": 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}'}))
    sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "sess-1",
      "result": "done"}))
"""


def run_429_session(times: int, labels: list[str], text: str = "", resumes_max: int = 3, cli: str = "agy"):
    """A merger unit whose CLI exits on a 429 `times` times; the fake gh shows the issue
    with `labels` (stage:pr still on it: the work is not done)."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-429-"))
    role = d / "ccrole"
    role.write_text(FAKE_CCROLE_429_CLAUDE if cli == "claude" else FAKE_CCROLE_429)
    role.chmod(0o755)
    gh = d / "gh"
    view = {"state": "OPEN", "labels": [{"name": x} for x in labels], "comments": []}
    gh.write_text(f"#!/bin/sh\necho '{json.dumps(view)}'\n")
    gh.chmod(0o755)
    cfg = ccpool.Config(
        logdir=d / "pool",
        ccrole=str(role),
        render="cat",
        gh=str(gh),
        timeouts={"merger": "20m"},
        quota_resumes=resumes_max,
        quota_wait_s=0.05,
        quota_wait_max_s=0.2,
    )
    pool = ccpool.Pool(cfg, fetcher=state, spawn=False)
    w = ccpool.Session(
        pool,
        ccpool.Unit("merger", "land", "mrg:9@x", "mrg:9", 9, (0, 9), {"issue": 2}),
        1,
    )
    w.dir.mkdir(parents=True)
    env = dict(w.env(), FAKE_CALLS=str(d / "calls"), FAKE_429_TIMES=str(times), CCROLE_MERGER_CLI=cli)
    if text:
        env["FAKE_429_TEXT"] = text
    rc, _, timed_out = w.session("do it", env)
    run_429_session.last = w
    calls = [json.loads(x) for x in (d / "calls").read_text().splitlines()]
    rl = w.dir / "resumes.log"
    resumes = [x for x in rl.read_text().splitlines() if " conversation " in x] if rl.exists() else []
    events = [
        e.get("event") or e.get("type")
        for e in agent_cli.parse((w.dir / "session.jsonl").read_text().splitlines())
    ]
    return rc, timed_out, calls, resumes, events


def test_idle_pool_makes_tester_work() -> None:
    h = Rig(state([iss(1, ["stage:triage", "P1"])], main="a" * 40),
            ux=True, spec=True, ux_gap_s=1500, ux_idle_gap_s=600, spec_gap_s=2700)
    first = h.step()
    check("with a worker due: one UX run and the worker, no spec check",
          sorted(u.kind for u in first), ["implement", "ux"])
    for u in first:
        h.finish(u.key)
    h.st = state(main="a" * 40)  # nothing left for workers
    later = h.step(700)
    check("idle: an extra UX run on the same sha and a spec check",
          sorted((u.kind, u.key.split(":")[-1][:4]) for u in later), [("spec", "0"), ("ux", "idle")])
    for u in later:
        h.finish(u.key)
    check("not again before the gaps pass", h.step(60), [])
    again = h.step(600)
    check("another idle UX run after POOL_UX_IDLE_GAP_MIN, the spec check waits",
          [u.kind for u in again], ["ux"])
    h.st = state(main="c" * 40)  # a newer sha to test
    check("no UX run while one is going, even on a newer sha, scheduled or idle",
          [u.key for u in h.step(2000) if u.kind == "ux"], [])
    h.finish(again[0].key)
    check("one starts once it ended", [u.key for u in h.step(1) if u.kind == "ux"], [f"ux:{'c' * 12}"])
    spec = next(u for u in later if u.kind == "spec")
    text = ccpool.tester_prompt(spec, 8021, "60m")
    check("the spec check prompt", ("Spec check" in text, "at most five" in text, "does not deploy" in text),
          (True, True, True))
    ux = next(u for u in first if u.kind == "ux")
    check("UX runs file up to five", "at most five" in ccpool.tester_prompt(ux, 8021, "60m"), True)

    h = Rig(state(main="b" * 40), ux=False, spec=False, ux_idle_gap_s=0, spec_gap_s=0)
    check("UX runs and spec checks off: an idle pool starts nothing", h.step(), [])
    h = Rig(state(main="b" * 40), ux=False, spec=True, spec_gap_s=0)
    check("spec checks alone", [u.kind for u in h.step()], ["spec"])


FAKE_GH_LOG = r"""#!/bin/sh
echo "$*" >> "$0.log"
case "$1 $2" in
  "issue list") cat "$0.list" 2>/dev/null || echo "[]" ;;
  "issue create") echo "https://github.com/o/r/issues/77" ;;
esac
"""


def gh_log() -> str:
    """A gh that logs its arguments and files issues (FAKE_GH_LOG), in a directory of its own."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-gh-"))
    (d / "gh").write_text(FAKE_GH_LOG)
    (d / "gh").chmod(0o755)
    return str(d / "gh")


def test_main_check_reports_a_red_main() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-maincheck-"))
    gh = d / "gh"
    gh.write_text(FAKE_GH_LOG)
    gh.chmod(0o755)
    h = Rig(state(main="1" * 40), main_check=True, gh=str(gh))
    pool = h.pool
    red = ("tests/unit/test_ok.py .. \nFAILED tests/e2e/test_a.py::test_x - AssertionError: boom\n"
           "FAILED tests/e2e/test_b.py::test_y\nERROR tests/e2e/test_c.py::test_z\n== sweep is red ==\n")
    results = {"1": (False, red), "2": (False, red), "3": (True, "44 green"),
               "4": (False, "FAILED tests/e2e/test_d.py::test_w\n"), "5": (True, "45 green")}
    swept, gate = [], threading.Event()
    gate.set()

    def sweep(sha):
        swept.append(sha[0])
        gate.wait(10)
        return results[sha[0]]
    pool.sweep_main = sweep
    pool.commits_since = lambda old, new: [f"{new[:7]} Merge PR #5", f"{old[:7] or 'none'} older"]

    def settle():
        for _ in range(100):
            if not pool.main_check_running:
                return
            time.sleep(0.05)

    def calls():
        return (d / "gh.log").read_text() if (d / "gh.log").exists() else ""
    h.step()
    settle()
    check("a red main: an issue titled by the first failing tests, type:bug P0 stage:triage",
          [ln for ln in calls().splitlines() if ln.startswith("issue create")][0].split(" --body ")[0],
          "issue create --title main is red: tests/e2e/test_a.py::test_x, tests/e2e/test_b.py::test_y and 1 more "
          "--label type:bug --label P0 --label stage:triage")
    text = calls()
    check("signed, with the failing tests, the commits and the output",
          ("<!-- merger -->" in text, "- `tests/e2e/test_c.py::test_z`" in text,
           "No green main seen since the supervisor started" in text, "AssertionError: boom" in text),
          (True, True, True, True))
    check("an open one was looked for first", "issue list --state open --search main is red in:title" in text, True)
    check("the monitor hears it", any("main 11111111 is RED" in e and "filed #77" in e for e in pool.events), True)
    h.step()
    check("the dashboard's state has the check",
          {k: v for k, v in json.loads((pool.cfg.logdir / "state.json").read_text())["main_check"].items() if k != "t"},
          {"sha": "1" * 40, "ok": False, "failing": ["tests/e2e/test_a.py::test_x", "tests/e2e/test_b.py::test_y",
                                                     "tests/e2e/test_c.py::test_z"]})
    gate.clear()
    h.st = state(main="2" * 40)
    h.step()
    h.st = state(main="9" * 40)
    h.step()
    h.st = state(main="3" * 40)
    h.step()
    check("one check at a time (a red one runs twice: a pass the second time would be a flake)",
          swept, ["1", "1", "2"])
    gate.set()
    settle()
    h.step()
    settle()
    check("then the newest main only, not the ones in between", swept, ["1", "1", "2", "2", "3"])
    log = calls()
    check("still red on the same tests (2): no second issue and no comment",
          (log.count("issue create"), log.count("issue comment")), (1, 1))
    check("green again (3): a comment and the issue closed",
          (log.splitlines()[-1], "main is green again at 33333333" in log), ("issue close 77", True))
    check("and the monitor hears it", any("green again at 33333333; closed #77" in e for e in pool.events), True)
    h.st = state(main="4" * 40)
    h.step()
    settle()
    check("red again: a new issue, with the commits since the last green main",
          (calls().count("issue create"), "Commits since the last green main (33333333)" in calls()), (2, True))
    (d / "gh.list").write_text(json.dumps([{"number": 80, "title": "main is red: something"}]))
    pool.red_issue, pool.red_looked = None, False  # a restarted supervisor
    h.st = state(main="5" * 40)
    h.step()
    settle()
    check("a restarted supervisor finds the open issue and closes it on green", calls().splitlines()[-1],
          "issue close 80")
    off = Rig(state(main="6" * 40))
    off.step()
    check("POOL_MAIN_CHECK off: no check", (off.pool.main_check_sha, off.pool.main_check_running), ("", False))
    old = {k: os.environ.pop(k, None) for k in ("POOL_MAIN_CHECK", "SWEEP_COMMAND")}
    try:
        check("no sweep command: off", ccpool.Config.from_env().main_check, False)
        os.environ["SWEEP_COMMAND"] = "make verify"
        check("on by default with a sweep command", ccpool.Config.from_env().main_check, True)
        os.environ["POOL_MAIN_CHECK"] = "0"
        check("POOL_MAIN_CHECK=0 turns it off", ccpool.Config.from_env().main_check, False)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    work, g = git_repo(d / "repo")
    (work / ".claude/hooks").mkdir(parents=True)
    for f in ("sweep_run.py", "config.py"):
        (work / ".claude/hooks" / f).write_text((HERE / f).read_text())
    g("add", "-f", ".")
    g("commit", "-qm", "base")
    g("push", "-q", "origin", "HEAD:main")
    sha = g("rev-parse", "HEAD")
    keep = {k: os.environ.get(k) for k in ("SHARED_REPO_PATH", "SWEEP_COMMAND", "SWEEP_LOCK", "SWEEP_RECORD_DIR")}
    os.environ.update(SHARED_REPO_PATH=str(work), SWEEP_LOCK=str(d / "lock"), SWEEP_RECORD_DIR=str(d / "rec"),
                      SWEEP_COMMAND="echo 'FAILED tests/t.py::test_a - boom'; exit 1")
    try:
        real = ccpool.Pool(ccpool.Config(logdir=d / "pool2"), fetcher=state, spawn=False)
        ok, out = real.sweep_main(sha)
        check("the real sweep in a worktree at main: red, with the failing test",
              (ok, ccpool.failing_tests(out)), (False, ["tests/t.py::test_a"]))
        os.environ["SWEEP_COMMAND"] = "true"
        check("and green", real.sweep_main(sha)[0], True)
        check("its worktree is gone", pathlib.Path(f"{SCRATCH}/main-{sha[:12]}").exists(), False)
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    check("a red sweep with no pytest summary names sweep_run.py's reasons",
          ccpool.failing_tests("x\n== sweep is red ==\n   `make verify` exited 2\n\nsweep record not written\n"),
          ["`make verify` exited 2"])


def test_spec_check_when_half_the_pool_is_free() -> None:
    """Workers never ran out of issues with 16 sessions, so spec checks never started,
    even with most slots idle."""
    issues = [iss(n, ["stage:triage", "P1"]) for n in range(1, 4)]
    h = Rig(state(issues, main="a" * 40), spec=True, spec_gap_s=2700, global_cap=16)
    first = h.step()
    check("3 workers due of 16 slots: a spec check starts beside them",
          sorted(u.kind for u in first), ["implement", "implement", "implement", "spec"])
    spec = next(u for u in first if u.kind == "spec")
    check("and the tester is told why", "half of the pool's sessions are free" in ccpool.tester_prompt(spec, 8021, "60m"),
          True)
    h.finish(spec.key)
    check("not again within POOL_SPEC_GAP_MIN", [u.kind for u in h.step(60)], [])
    h.pool.book.last_spec_t = 0
    h.pool.book.running["tst:spec"] = spec  # one still running
    check("never two at once", [u.kind for u in h.step(1)], [])
    del h.pool.book.running["tst:spec"]
    busy = Rig(state([iss(n, ["stage:triage", "P1"]) for n in range(1, 11)], main="a" * 40),
               spec=True, spec_gap_s=2700, global_cap=16)
    check("10 workers due of 16: less than half stays free, no spec check",
          [u.kind for u in busy.step() if u.kind == "spec"], [])
    for u in [u for u in busy.pool.book.running.values()][:6]:
        busy.finish(u.key)
    check("4 left running and nothing else due: half is free, a spec check",
          [u.kind for u in busy.step()], ["spec"])
    uncapped = Rig(state(issues, main="a" * 40), spec=True, spec_gap_s=2700)
    check("no global cap: no half to measure, only the idle trigger",
          [u.kind for u in uncapped.step() if u.kind == "spec"], [])


def test_ux_journeys_and_score() -> None:
    ux = ccpool.Unit("tester", "ux", "ux:abc", "tst:ux:abc", None, (2, 0, 0), {"sha": "a" * 40, "deployed": True})
    own = ccpool.tester_prompt(ux, 8021, "60m")
    check("no journeys script: the tester keeps its own per repository, not in a shared /tmp/ux",
          (f"{SCRATCH}/ux/journeys.py" in own, "/tmp/ux/" in own), (True, False))
    given = ccpool.tester_prompt(ux, 8021, "60m", "ux/journeys.py")
    check("the project's script: run it from the tree, write none, probes in a scratch file",
          ("`ux/journeys.py`, from your extracted tree" in given, "do not write a journeys file" in given,
           f"{SCRATCH}/tst-8021-probes.py" in given, f"{SCRATCH}/ux/" in given), (True, True, True, False))
    check("the report's first line is the score", ("`Score: N/100`" in own, "`Score: N/100`" in given), (True, True))
    old = os.environ.pop("POOL_UX_JOURNEYS", None)
    try:
        os.environ["POOL_UX_JOURNEYS"] = "ux/journeys.py"
        check("POOL_UX_JOURNEYS", ccpool.Config.from_env().ux_journeys, "ux/journeys.py")
    finally:
        os.environ.pop("POOL_UX_JOURNEYS", None)
        if old is not None:
            os.environ["POOL_UX_JOURNEYS"] = old
    h = Rig(state(main="a" * 40), ux=True, ux_idle_gap_s=1e12)
    for i, report in enumerate(("Score: 38/100\n\nThe commit ...", None, "# UX run\n**Score:** 58 / 100\n")):
        h.st = state(main=str(i + 1) * 40)
        u = next(u for u in h.step(2000) if u.kind == "ux")
        w = next(w for w in h.pool.sessions.values() if w.unit.key == u.key)
        w.dir.mkdir(parents=True, exist_ok=True)
        if report is not None:
            (w.dir / "ux_report.md").write_text(report)
        h.finish(u.key)
    logged = [json.loads(x) for x in (h.pool.cfg.logdir / "pool.jsonl").read_text().splitlines()
              if json.loads(x)["kind"] == "ux_score"]
    check("each UX run's score is logged, a missing one as missing",
          [(r["sha"][:1], r["score"], "missing" in r) for r in logged], [("1", 38, False), ("2", None, True), ("3", 58, False)])
    h.step()
    check("and kept in state.json for the dashboard",
          [r["score"] for r in json.loads((h.pool.cfg.logdir / "state.json").read_text())["ux_scores"]], [38, None, 58])
    again = ccpool.Pool(h.pool.cfg, fetcher=state, spawn=False)
    check("a restarted supervisor keeps them", [r["score"] for r in again.ux_scores], [38, None, 58])


def test_leftover_processes_are_killed() -> None:
    """A server or test run a session started with setsid outlives it; when the session
    ends, what runs in its scratch paths is killed, and nothing else."""
    seq = 900000 + os.getpid() % 90000
    inside = pathlib.Path(tempfile.mkdtemp(prefix=f"wrk-{seq}-7-", dir=SCRATCH))
    outside = pathlib.Path(tempfile.mkdtemp(prefix=f"wrk-{seq + 1}-7-", dir=SCRATCH))
    procs = [subprocess.Popen(["sleep", "60"], cwd=str(c), start_new_session=True) for c in (inside, outside)]
    try:
        h = Rig(state())
        w = ccpool.Session(h.pool, ccpool.Unit("worker", "implement", "wrk:7@x", "wrk:7", 7, (4,), {}), seq)
        check("a worker's scratch paths", w.scratch_prefixes(), [f"{SCRATCH}/wrk-{seq}-"])
        killed = w.kill_leftovers()
        check("the process in the session's paths is killed", (killed, procs[0].wait(timeout=10) != 0),
              ([procs[0].pid], True))
        check("one in another session's paths is not", procs[1].poll(), None)
        rec = [json.loads(x) for x in (h.pool.cfg.logdir / "pool.jsonl").read_text().splitlines()
               if json.loads(x)["kind"] == "leftovers_killed"]
        check("and the kill is logged", [(p["pid"], p["cwd"], p["cmd"]) for p in rec[0]["processes"]],
              [(procs[0].pid, str(inside), "sleep 60")])
        check("nothing left: nothing logged", w.kill_leftovers(), [])
        m = ccpool.Session(h.pool, ccpool.Unit("merger", "land", "mrg:12@x", "mrg:12", 12, (0,), {"issue": 2}), 3)
        t = ccpool.Session(h.pool, ccpool.Unit("tester", "ux", "ux:a", "tst:ux:a", None, (2,), {"sha": "a"}), 4)
        t.port = 8121
        a = ccpool.Session(h.pool, ccpool.Unit("auto", "merge", "merge:12@x", "mrg:12", 12, (0,), {}), 5)
        check("a merger's, a tester's, none for a unit without an agent",
              (m.scratch_prefixes()[0], t.scratch_prefixes()[0], a.scratch_prefixes()),
              (f"{SCRATCH}/mrg-12-", f"{SCRATCH}/tst-8121-", []))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        inside.rmdir()
        outside.rmdir()
    check("lsof's cwd listing (no /proc)", ccpool.parse_lsof("p12\nfcwd\nn/tmp/wrk-3-9-wt\np40\nfcwd\nn/home/x\n"),
          {12: "/tmp/wrk-3-9-wt", 40: "/home/x"})


def test_hard_ceiling_and_exit_124() -> None:
    """A session whose clocks stood still ran far past its timeout; and one killed at its
    deadline with an older 429 in its stderr was counted as a quota error."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-ceiling-"))
    agent = d / "ccrole"
    # Busy all along (output every second), with a 429 from earlier in its stderr.
    agent.write_text("#!/bin/sh\necho 'API error: 429 RESOURCE_EXHAUSTED' >&2\n"
                     "for i in $(seq 120); do echo '{\"event\": \"step_update\"}'; sleep 1; done\n")
    agent.chmod(0o755)
    pool = ccpool.Pool(ccpool.Config(logdir=d / "pool", ccrole=str(agent), render="cat",
                                     timeouts={"tester": "4s"}, timeout_ceiling=1.5, stall_s=0,
                                     caps={r: 0 for r in ccpool.ROLES}, kill_leftovers=False),
                       fetcher=state, spawn=False)
    t0 = time.time()
    w = pool.start(ccpool.Unit("tester", "ux", "ux:a", "tst:ux:a", None, (2,), {"sha": "a" * 40}))
    outcome = w.work()
    check("killed at the ceiling though it kept writing (next 15-s tick after 6 s)",
          time.time() - t0 < 40, True)
    check("exit 124 is a timeout, not a quota error", outcome, "timed out after 4s")
    check("resumes.log says why", "killed at the ceiling, 1.5 x the 4s timeout" in (w.dir / "resumes.log").read_text(),
          True)
    old = os.environ.pop("POOL_TIMEOUT_CEILING", None)
    try:
        check("default 1.5", ccpool.Config.from_env().timeout_ceiling, 1.5)
        os.environ["POOL_TIMEOUT_CEILING"] = "0"
        check("POOL_TIMEOUT_CEILING=0 turns it off", ccpool.Config.from_env().timeout_ceiling, 0)
    finally:
        os.environ.pop("POOL_TIMEOUT_CEILING", None)
        if old is not None:
            os.environ["POOL_TIMEOUT_CEILING"] = old


def test_devcheck_gives_workers_more_time() -> None:
    """With the pre-PR live check on, a worker's default timeout and wrap-up move later,
    and its prompt names the check; explicit settings still win."""
    keys = ("POOL_DEVCHECK", "POOL_TIMEOUT_WORKER", "POOL_WORKER_WRAP_S")
    old = {k: os.environ.pop(k, None) for k in keys}
    try:
        cfg = ccpool.Config.from_env()
        check("devcheck off: 45 minutes, wrap-up at 35", (cfg.devcheck, cfg.timeouts["worker"], cfg.worker_wrap_s),
              (False, "45m", 2100.0))
        os.environ["POOL_DEVCHECK"] = "1"
        cfg = ccpool.Config.from_env()
        check("devcheck on: 60 minutes, wrap-up at 50", (cfg.devcheck, cfg.timeouts["worker"], cfg.worker_wrap_s),
              (True, "60m", 3000.0))
        os.environ["POOL_TIMEOUT_WORKER"] = "50m"
        check("an explicit worker timeout wins", ccpool.Config.from_env().timeouts["worker"], "50m")
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    claim = {"issue": 6, "token": "t", "from": "stage:triage", "title": "x"}
    check("the prompt names the live check only when it is on",
          ("devcheck.py" in ccpool.worker_prompt(claim, "1", "60m", "u"),
           "devcheck.py" in ccpool.worker_prompt(claim, "1", "60m", "u", devcheck=True)),
          (False, True))


def test_stall_threshold_by_model() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-stallmodel-"))
    pool = ccpool.Pool(ccpool.Config(logdir=d / "pool"), fetcher=state, spawn=False)
    inits = {
        "agy, Claude model": {"event": "init", "init": {"model": "claude-opus-high"}},
        "agy, other model": {"event": "init", "init": {"model": "some-other-model"}},
        "Claude Code": {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-sonnet"},
    }
    got = {}
    for i, (name, init) in enumerate(inits.items()):
        w = ccpool.Session(pool, ccpool.Unit("worker", "implement", f"wrk:{i}@x", f"wrk:{i}", i, (4,), {}), i + 1)
        w.cli = ccpool.agent_cli.get("claude" if "subtype" in init else "agy")
        w.dir.mkdir(parents=True)
        (w.dir / "session.jsonl").write_text(json.dumps(init) + "\n")
        got[name] = w.stall_after(pool.cfg)
    check("fast models count as stalled after 4 min, others after 10", got,
          {"agy, Claude model": 240, "agy, other model": 600, "Claude Code": 240})
    pool.cfg.stall_s = 0
    check("POOL_STALL_S=0 turns stall resumes off", w.stall_after(pool.cfg), 0)


def test_resume_after_quota_exit() -> None:
    """The CLI exits after a 429, so a resume that only fires while it is alive never would."""
    before = len(FAILURES)
    rc, timed_out, calls, resumes, events = run_429_session(1, ["stage:pr"])
    check(
        "exited on a 429, work not done: resumed once and finished",
        (rc, timed_out, len(calls)),
        (0, False, 2),
    )
    check(
        "the resume continues the conversation",
        calls[1][-2:],
        ["--conversation", "conv-1"],
    )
    check(
        "the first prompt is kept for the first session only",
        "do it" in calls[0] and "do it" not in calls[1],
        True,
    )
    check("resumes.log says why", ["exited with 1" in r for r in resumes], [True])
    check(
        "session.jsonl keeps both sessions",
        events,
        ["init", "result", "init", "result"],
    )
    rc, _, calls, resumes, _ = run_429_session(9, ["stage:pr"])
    check("at most POOL_QUOTA_RESUMES resumes (3 here)", (rc, len(calls), len(resumes)), (1, 4, 3))
    check("and the session counts as ended on quota", run_429_session.last.ended_on_quota(), True)
    capacity = (
        "TPM quota exhausted. No spare capacity is currently available to serve over-limit "
        "requests. Please try again later, reduce your request rate or contact the sales team."
    )
    rc, timed_out, calls, resumes, _ = run_429_session(2, ["stage:pr"], text=capacity, resumes_max=8)
    check(
        "the capacity wording (no RESOURCE_EXHAUSTED in the result) is a quota error too",
        (rc, timed_out, len(calls), len(resumes)),
        (0, False, 3, 2),
    )
    waits = [x for x in (run_429_session.last.dir / "resumes.log").read_text().splitlines() if "quota wait" in x]
    check("each resume waits first", len(waits), 2)
    rc, _, calls, resumes, _ = run_429_session(1, [])
    check(
        "the work is already on GitHub: no resume",
        (rc, len(calls), resumes),
        (1, 1, []),
    )
    rc, timed_out, calls, resumes, events = run_429_session(1, ["stage:pr"], cli="claude")
    check(
        "claude: exited on a 429, resumed once and finished",
        (rc, timed_out, len(calls)),
        (0, False, 2),
    )
    check(
        "claude: the resume names the session",
        (calls[1][calls[1].index("--resume") + 1], calls[1][2]),
        ("sess-1", "-p"),
    )
    check("claude: no print timeout flag", any("--print-timeout" in c for c in calls), False)
    check("claude: both streams kept", events, ["system", "result", "system", "result"])
    rc, _, calls, _, _ = run_429_session(9, ["stage:pr"], cli="claude")
    check("claude: at most POOL_QUOTA_RESUMES, then counted as quota",
          (rc, len(calls), run_429_session.last.ended_on_quota()), (1, 4, True))
    assert len(FAILURES) == before, FAILURES[before:]


def test_family_cap_counts_failures_and_spares_workers() -> None:
    h = Rig(state(), family_max=2)
    started = 0
    for i in range(5):  # a merged PR that a live check sends back: new key each time
        h.st = state([iss(9, ["stage:triage", "P0"], updated=f"2026-10-09T1{i}:00:00Z")])
        t = [u for u in h.step(10) if u.number == 9]
        started += len(t)
        for u in t:
            h.finish(u.key, "failed (exit 1)" if i % 2 else "ok")
    check("a worker keeps getting sessions past POOL_FAMILY_MAX (claim.py bounds it)", started, 5)
    fam = ccpool.Unit("merger", "land", "mrg:5@a", "mrg:5", 5, (0, 5), {})
    h.pool.book.family_starts[fam.family] = 2
    check("another role is stopped after POOL_FAMILY_MAX failures", h.pool.book.why_not(fam, h.pool.cfg), "family")
    w = ccpool.Session(h.pool, fam, 99)
    h.pool.book.running[fam.family] = fam
    h.pool.sessions[fam.family] = w
    h.pool.finished(w, "ok")
    check("a session that ends ok does not count", h.pool.book.family_starts[fam.family], 1)


def test_quota_backoff_and_pause() -> None:
    check(
        "the wait doubles and is capped",
        [ccpool.quota_backoff(k, 60, 900) for k in (1, 2, 3, 4, 5, 6)],
        [60, 120, 240, 480, 900, 900],
    )
    check(
        "a full answer next to a stale 429 is not a quota result",
        agent_cli.get("agy").quota_result({"event": "result", "result": {
            "status": "ERROR", "error": "API error (attempt 1): RESOURCE_EXHAUSTED",
            "response": "Merged PR #9 and moved the issue to stage:live as the skill says."}}),
        False,
    )
    h = Rig(
        state([iss(1, ["stage:triage", "P1"]), iss(2, ["stage:triage", "P1"])]),
        caps={r: 0 for r in ccpool.ROLES}, quota_wait_s=60, quota_wait_max_s=900,
    )
    first = h.step()
    check("two workers start", sorted(u.number for u in first), [1, 2])
    h.finish(first[0].key, "quota error (exit 3)")
    check("a quota error pauses new agent sessions", h.pool.book.quota_until, h.now + 60)
    check("and spends no try", h.pool.book.tries[first[0].key], 0)
    check("nothing starts during the pause", h.step(30), [])
    again = h.step(31)
    check("the unit is due again after it", [u.number for u in again], [first[0].number])
    h.finish(again[0].key, "quota error (exit 3)")
    check("the next pause in a row is twice as long", h.pool.book.quota_until, h.now + 120)
    check("the monitor hears each pause once", sum("no new agent sessions" in e for e in h.pool.events), 2)
    h.finish(first[1].key, "ok")
    check("a session that ends ok resets the streak", h.pool.book.quota_streak, 0)


ALLOW = '{"decision": "allow"}'


def auto_session(guard_out: str, review_rc: int = 0, guard_rc: int = 0, guard_cmd: str = ""):
    """Session.auto_merge with a logging fake gh, a fake review gate and a fake guard."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-auto-"))
    gh = d / "gh"
    gh.write_text(f'#!/bin/sh\necho "$*" >> {d}/gh.log\n')
    gh.chmod(0o755)
    review = d / "review"
    review.write_text(f"#!/bin/sh\nexit {review_rc}\n")
    review.chmod(0o755)
    guard = d / "guard"
    guard.write_text(f"#!/bin/sh\ncat > /dev/null\necho \"$GUARD_EXPLICIT_ALLOW\" > {d}/explicit\n"
                     f"printf '%s' '{guard_out}'\nexit {guard_rc}\n")
    guard.chmod(0o755)
    cfg = ccpool.Config(
        logdir=d / "pool", gh=str(gh), review_cmd=str(review), guard_cmd=guard_cmd or str(guard)
    )
    pool = ccpool.Pool(cfg, fetcher=state, spawn=False)
    u = ccpool.Unit(
        "auto",
        "merge",
        "merge:9@x",
        "mrg:9",
        9,
        (0, 9),
        {"issue": 2, "head": "c" * 40, "mergeable": "MERGEABLE", "needs_live": True},
    )
    w = ccpool.Session(pool, u, 1)
    w.dir.mkdir(parents=True)
    outcome = w.work()
    log = (d / "gh.log").read_text().splitlines() if (d / "gh.log").exists() else []
    return outcome, [x for x in log if not x.startswith("pr view")], pool  # needs_sweep's read


def test_auto_merge_paths() -> None:
    outcome, log, _ = auto_session(ALLOW)
    check("gates pass: merged", outcome, "ok")
    check(
        "squash merge at the head, then stage:pr -> stage:live for a needs:live issue",
        log[:2],
        [
            f"pr merge 9 --squash --delete-branch --match-head-commit {'c' * 40}",
            "issue edit 2 --remove-label stage:pr --add-label stage:live",
        ],
    )
    check(
        "and a signed merge comment",
        log[2].startswith("issue comment 2 --body <!-- merger -->"),
        True,
    )
    outcome, log, _ = auto_session(
        json.dumps(
            {"decision": "block", "reason": "PR #9 body is missing `Fail-before`."}
        )
    )
    check(
        "a gate refusal hands the PR back",
        outcome,
        "refused by the merge gates (handed back)",
    )
    text = "\n".join(log)
    check(
        "with the reason on the PR and the issue back at stage:triage",
        (log[0].startswith("pr comment 9") and "Fail-before" in text, log[-1]),
        (True, "issue edit 2 --remove-label stage:pr --add-label stage:triage"),
    )
    check("never merged", any("pr merge" in x for x in log), False)
    outcome, log, pool = auto_session(
        json.dumps({"decision": "block", "reason": "The shared checkout is dirty."})
    )
    check(
        "Gate 0 (the shared checkout) is not the PR's fault: no hand-back",
        (outcome, log),
        ("held (gate 0: the shared checkout is not clean)", []),
    )
    check("the monitor hears it", any("Gate 0" in e for e in pool.events), True)
    outcome, log, _ = auto_session(json.dumps({"decision": "block", "reason": (
        "PR #110 could not be read from GitHub (gh pr view failed, timed out or answered without a head), "
        "so its merge gates cannot be checked. Not merged; try again later.")}))
    check("GitHub could not be read (PR #110): held, not handed back",
          (outcome, log), ("held (the merge gates could not read GitHub)", []))
    outcome, log, _ = auto_session(ALLOW, review_rc=1)
    check(
        "a review-gate reject stops before the merge gates",
        (outcome, log),
        ("rejected by the review gate (handed back)", []),
    )


def test_auto_merge_when_the_review_waits_for_the_owner() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-reviewowner-"))
    old = os.environ.get("REVIEW_DIR")
    os.environ["REVIEW_DIR"] = str(d)
    try:
        (d / f"{'c' * 40}.json").write_text(json.dumps({"verdict": "owner", "reason": "reject 3 of this PR"}))
        outcome, log, pool = auto_session(ALLOW, review_rc=1)
    finally:
        if old is None:
            os.environ.pop("REVIEW_DIR", None)
        else:
            os.environ["REVIEW_DIR"] = old
    check("a review sent to the owner: not merged, not a hand-back, the monitor hears it",
          (outcome, log, any("waits for the owner" in e for e in pool.events)),
          ("waits for the owner (review gate)", [], True))


def test_auto_merge_holds_without_an_explicit_allow() -> None:
    """A guard that crashed or answered nothing let the merge go ahead: its reply read as {}."""
    for what, out, rc, cmd in (
        ("exit 1 with no output", "", 1, ""),
        ("exit 2 with an allow printed", ALLOW, 2, ""),
        ("empty output", "", 0, ""),
        ("malformed JSON", "{not json", 0, ""),
        ("the guard's own error (fail-open note, no decision)", '{"systemMessage": "role_guard error, allowing"}', 0, ""),
        ("an unknown decision", '{"decision": "maybe"}', 0, ""),
        ("Claude Code's bare allow", "{}", 0, ""),
        ("a guard that cannot be started", "", 0, "/nonexistent/guard"),
    ):
        outcome, log, pool = auto_session(out, guard_rc=rc, guard_cmd=cmd)
        check(f"{what}: held, no merge and no hand-back",
              (outcome.startswith("held ("), [x for x in log if x.startswith(("pr merge", "pr comment", "issue edit"))]),
              (True, []))
        check(f"{what}: the monitor hears it", any("held" in e for e in pool.events), True)
    d = pathlib.Path(auto_session(ALLOW)[2].cfg.logdir).parent
    check("the supervisor asks the guard for an explicit allow", (d / "explicit").read_text().strip(), "1")


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_unfinished_work_survives() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-wip-"))
    origin, root = d / "origin.git", d / "repo"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(root)], check=True, capture_output=True
    )
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        git("config", k, v, cwd=root)
    (root / "a.txt").write_text("a\n")
    git("add", ".", cwd=root)
    git("commit", "-q", "-m", "base", cwd=root)
    git("push", "-q", "origin", "main", cwd=root)
    wt = d / "wrk-7-12-wt"
    git("worktree", "add", "-q", "-B", "issue-12", str(wt), "origin/main", cwd=root)
    (wt / "b.txt").write_text("half done\n")
    gh = d / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")  # pr list --head: no PR
    gh.chmod(0o755)
    old = os.environ.get("SHARED_REPO_PATH")
    os.environ["SHARED_REPO_PATH"] = str(root)
    try:
        pool = ccpool.Pool(
            ccpool.Config(logdir=d / "pool", gh=str(gh)), fetcher=state, spawn=False
        )
        w = ccpool.Session(
            pool,
            ccpool.Unit("worker", "implement", "wrk:12@x", "wrk:12", 12, (4,), {}),
            7,
        )
        w.dir.mkdir(parents=True)
        w.save_wip(str(d / "wrk-7-*-wt"))
        check(
            "the unfinished work is pushed to issue-12",
            git("show", "origin/issue-12:b.txt", cwd=root)
            if git("fetch", "-q", "origin", cwd=root) == ""
            else "",
            "half done",
        )
        wip = w.wip_branch(12)
        check(
            "the next worker learns about it",
            wip.endswith("WIP #12: unfinished worker session"),
            True,
        )
        claim = {"issue": 12, "token": "t", "from": "stage:triage", "title": "x"}
        text = ccpool.worker_prompt(claim, "8", "45m", "unlimited", wip)
        check(
            "and is told to start from origin/issue-12",
            "git worktree add -B issue-12 <worktree> origin/issue-12" in text,
            True,
        )
        check("no branch, no hint", w.wip_branch(99), "")
        with_pr = ccpool.worker_prompt(
            dict(claim, pr={"number": 5, "branch": "issue-12"}), "8", "45m", "u", ""
        )
        check(
            "a handed-back PR is updated, not restarted",
            "Do not open a second" in with_pr,
            True,
        )
    finally:
        if old is None:
            os.environ.pop("SHARED_REPO_PATH", None)
        else:
            os.environ["SHARED_REPO_PATH"] = old


def conflict_repo(test_only: bool, append_src: bool = False):
    """origin with main and branch issue-7 that both append a case to one test file
    (and, unless test_only, both change one line of a source file, or with append_src
    both append a function to it)."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-rebase-"))
    origin, root = d / "origin.git", d / "repo"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(root)], check=True, capture_output=True
    )
    g = lambda *a: git(*a, cwd=root)  # noqa: E731
    g("config", "user.name", "t")
    g("config", "user.email", "t@t")
    (root / "tests").mkdir()
    (root / "tests/test_shell.py").write_text("def test_a():\n    assert True\n")
    (root / "app.js").write_text("const x = 1;\n")
    g("add", ".")
    g("commit", "-q", "-m", "base")
    g("push", "-q", "origin", "main")
    g("checkout", "-q", "-b", "issue-7")
    with open(root / "tests/test_shell.py", "a") as f:
        f.write("\n\ndef test_issue_7():\n    assert 7\n")
    if append_src:
        with open(root / "app.js", "a") as f:
            f.write("function seven() {\n  return 7;\n}\n")
    elif not test_only:
        (root / "app.js").write_text("const x = 7;\n")
    g("commit", "-qam", "issue 7")
    g("push", "-q", "origin", "issue-7")
    g("checkout", "-q", "main")
    with open(root / "tests/test_shell.py", "a") as f:
        f.write("\n\ndef test_issue_8():\n    assert 8\n")
    if append_src:
        with open(root / "app.js", "a") as f:
            f.write("function eight() {\n  return 8;\n}\n")
    elif not test_only:
        (root / "app.js").write_text("const x = 8;\n")
    g("commit", "-qam", "issue 8")
    g("push", "-q", "origin", "main")
    return d, root, origin


def run_rebase(test_only: bool, append_src: bool = False):
    d, root, origin = conflict_repo(test_only, append_src)
    sweep = (
        d / "sweep_locked.py"
    )  # stands in for the real sweep: green, records the call
    sweep.write_text(
        f"import sys\nopen({str(d / 'swept')!r}, 'a').write(' '.join(sys.argv[1:]))\nprint('3 green: unit 3')\n"
    )
    old = (os.environ.get("SHARED_REPO_PATH"), ccpool.HERE)
    os.environ["SHARED_REPO_PATH"] = str(root)
    ccpool.HERE = d
    try:
        pool = ccpool.Pool(
            ccpool.Config(logdir=d / "pool", gh=gh_log(), split_after=1),
            fetcher=state,
            spawn=False,
        )
        u = ccpool.Unit(
            "auto",
            "rebase",
            "rebase:41@x",
            "mrg:41",
            41,
            (0, 41),
            {
                "issue": 7,
                "head": "b" * 40,
                "mergeable": "CONFLICTING",
                "branch": "issue-7",
            },
        )
        w = ccpool.Session(pool, u, 1)
        w.dir.mkdir(parents=True)
        outcome = w.work()
    finally:
        ccpool.HERE = old[1]
        if old[0] is None:
            os.environ.pop("SHARED_REPO_PATH", None)
        else:
            os.environ["SHARED_REPO_PATH"] = old[0]
    git("fetch", "-q", "origin", cwd=root)
    pushed = git("show", "origin/issue-7:tests/test_shell.py", cwd=root)
    if append_src:
        pushed = git("show", "origin/issue-7:app.js", cwd=root)
    return (
        outcome,
        pushed,
        (d / "swept").read_text() if (d / "swept").exists() else "",
        pool,
    )


def test_stop_catches_a_session_still_starting() -> None:
    """A worker still claiming its issue when `stop --all` ran started its agent
    afterwards and kept running, its issue left at stage:doing."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-stoprace-"))
    agent = d / "ccrole"
    agent.write_text(f"#!/bin/sh\necho started >> {d}/agent.calls\nsleep 30\n")
    agent.chmod(0o755)
    pool = ccpool.Pool(ccpool.Config(logdir=d / "pool", ccrole=str(agent), render="cat", gh="true",
                                     kill_leftovers=False, caps={r: 0 for r in ccpool.ROLES},
                                     timeouts={"worker": "45m"}), fetcher=state, spawn=False)
    u = ccpool.Unit("worker", "implement", "wrk:5@x", "wrk:5", 5, (4,), {})
    w = pool.start(u)
    calls = []
    real_sh = ccpool.Session.sh

    def sh(self, args, env, name):
        calls.append((name, args))
        if name == "claim":  # the claim is being made when the owner stops the loop
            (self.dir / "claim").write_text(json.dumps({"issue": 5, "token": "t", "from": "stage:triage",
                                                        "title": "x", "pr": {"number": 9, "branch": "issue-5"}}))
            self.pool.stop()
        return 0
    ccpool.Session.sh = sh
    try:
        outcome = w.work()
    finally:
        ccpool.Session.sh = real_sh
    check("stopped during its claim: the agent never starts", (d / "agent.calls").exists(), False)
    check("and it is wrapped up like the others: claim released, no failed attempt",
          (outcome, [n for n, _ in calls], "--no-fail" in calls[-1][1]),
          ("stopped with the supervisor", ["claim", "finish"], True))
    check("nothing starts once stopping", pool.start(ccpool.Unit("worker", "implement", "wrk:6@x", "wrk:6", 6, (4,), {})),
          None)
    h = Rig(state([iss(1, ["stage:triage"])]))
    h.pool.stopping = True
    check("a step after stop() starts no unit", (h.step(), h.pool.book.running), ([], {}))
    pool2 = ccpool.Pool(ccpool.Config(logdir=d / "pool2", ccrole=str(agent), render="cat",
                                      timeouts={"tester": "60m"}), fetcher=state, spawn=False)
    t = ccpool.Session(pool2, ccpool.Unit("tester", "ux", "ux:a", "tst:ux:a", None, (2,), {"sha": "a" * 40}), 2)
    t.dir.mkdir(parents=True)
    pool2.sessions[t.unit.family] = t
    killer = threading.Timer(1.0, pool2.stop)
    killer.start()
    t0 = time.time()
    rc, _, _ = t.session("walk the app", dict(os.environ))
    check("a session running when stop() comes is killed", (rc != 0, time.time() - t0 < 25), (True, True))


def test_stop_ends_the_supervisors_own_commands() -> None:
    """A main check went on for six minutes after `stop --all` (and left its worktree)."""
    h = Rig(state())
    pool = h.pool
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-owned-"))
    result = {}

    def check_main():
        result["r"] = pool.run_owned(["bash", "-c", f"sleep 30; touch {d}/finished"], 120)
        result["after"] = True  # where the worktree would be removed
    pool.main_check_thread = threading.Thread(target=check_main, daemon=True)
    pool.main_check_thread.start()
    for _ in range(50):
        if pool.owned:
            break
        time.sleep(0.05)
    t0 = time.time()
    pool.stop()
    pool.drain(timeout=10)
    check("stop() ends a running supervisor command, and drain() waits for its thread",
          (result.get("r") is not None and result["r"].returncode != 0, result.get("after"), time.time() - t0 < 5,
           (d / "finished").exists()), (True, True, True, False))
    r = pool.run_owned(["bash", "-c", f"touch {d}/started"], 10)
    check("once stopping, none starts", (r.returncode != 0, (d / "started").exists()), (True, False))
    fresh = Rig(state()).pool
    check("otherwise it runs and returns its output",
          (fresh.run_owned(["bash", "-c", "echo hi; echo err >&2; exit 3"], 10).returncode,
           fresh.run_owned(["bash", "-c", "echo hi"], 10).stdout), (3, "hi\n"))
    try:
        fresh.run_owned(["bash", "-c", "sleep 5"], 0.5)
        timed = False
    except subprocess.TimeoutExpired:
        timed = True
    check("a timeout raises, as subprocess.run's does", timed, True)


def test_stop_lets_sessions_release_their_claims() -> None:
    """`issuelooper stop` kills the sessions; each must still save its work and put its
    claim back, without a failed attempt, before the supervisor exits. Otherwise a
    restart finds the issues at stage:doing until their claims go stale."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-stop-"))
    pool = ccpool.Pool(ccpool.Config(logdir=d / "pool"), fetcher=state, spawn=False)
    done: list[bool] = []

    def wrap_up() -> None:
        time.sleep(0.5)
        done.append(True)

    t = threading.Thread(target=wrap_up, daemon=True)
    t.start()
    pool.sessions["wrk:1"] = t
    pool.stopping = True
    pool.drain(timeout=10)
    check("the supervisor waits for a killed session's wrap-up", done, [True])
    check(
        "a stop is not a failed attempt",
        "stopped with the supervisor".startswith(ccpool.NOT_A_FAILURE),
        True,
    )


def test_supervisor_rebase() -> None:
    outcome, pushed, swept, pool = run_rebase(test_only=True)
    check(
        "two cases appended to one test file: rebased by the supervisor", outcome, "ok"
    )
    check(
        "both cases are kept, main's first",
        (
            pushed.find("test_issue_8") < pushed.find("test_issue_7"),
            "<<<<<<<" in pushed,
        ),
        (True, False),
    )
    check("swept with --update-pr before the push", swept, "--update-pr 41")
    check(
        "test-file conflicts do not count toward a split", pool.book.split_filed, set()
    )
    outcome, pushed, swept, pool = run_rebase(test_only=False)
    check(
        "a source hunk that changes a line is left to a merger",
        outcome,
        "left to a merger (a hunk changes existing lines)",
    )
    check("nothing pushed and no sweep", ("test_issue_8" in pushed, swept), (False, ""))
    check(
        "the source file counts toward a split issue (split_after=1 here)",
        pool.book.split_filed,
        {"app.js"},
    )
    check(
        "and the monitor hears it",
        any("split issue for app.js" in e for e in pool.events),
        True,
    )


def test_split_rules() -> None:
    h = Rig(state(), split_after=3, split_window_s=600, split_max=1, gh=gh_log())
    for pr in (1, 2):
        h.pool.record_conflicts(pr, {"src/app.js", "tests/test_page.py"})
    check("two PRs: below the threshold", h.pool.book.split_filed, set())
    h.pool.record_conflicts(2, {"src/app.js"})
    check("the same PR twice counts once", h.pool.book.split_filed, set())
    h.pool.record_conflicts(3, {"src/app.js", "src/style.css"})
    check(
        "three PRs on one source file: one split issue",
        h.pool.book.split_filed,
        {"src/app.js"},
    )
    for pr in (4, 5, 6):
        h.pool.record_conflicts(pr, {"src/style.css"})
    check(
        "POOL_SPLIT_MAX caps the issues per run",
        h.pool.book.split_filed,
        {"src/app.js"},
    )
    h2 = Rig(state(), split_after=2, split_window_s=60, gh=gh_log())
    h2.pool.record_conflicts(1, {"main.py"})
    h2.now += 120
    h2.pool.record_conflicts(2, {"main.py"})
    check(
        "conflicts older than the window do not count", h2.pool.book.split_filed, set()
    )


def test_source_and_test_files() -> None:
    old = os.environ.pop("REVIEW_PATHS", None)
    try:
        check("test files in any common layout",
              [ccpool.is_test(f) for f in ("tests/unit/test_a.py", "web/src/a.test.ts", "pkg/a_test.go",
                                           "app/tests/x.py", "src/app.py")],
              [True, True, True, True, False])
        check("source: under REVIEW_PATHS (default src/), not a test",
              [ccpool.is_source(f) for f in ("src/app.py", "src/a.test.ts", "docs/x.md", "lib/b.py")],
              [True, False, False, False])
        os.environ["REVIEW_PATHS"] = "lib/,src/"
        check("REVIEW_PATHS lists the source roots", ccpool.is_source("lib/b.py"), True)
    finally:
        os.environ.pop("REVIEW_PATHS", None)
        if old is not None:
            os.environ["REVIEW_PATHS"] = old


def test_supervisor_rebase_appended_source() -> None:
    outcome, pushed, swept, pool = run_rebase(test_only=False, append_src=True)
    check("two functions appended to one source file: rebased", outcome, "ok")
    check(
        "both kept, main's first, no markers",
        (pushed.find("eight") < pushed.find("seven"), "<<<<<<<" in pushed),
        (True, False),
    )
    check("swept before the push", swept, "--update-pr 41")


def test_split_per_window_and_large_files() -> None:
    h = Rig(state(), split_after=1, split_window_s=600, split_per_window=1, gh=gh_log())
    h.pool.record_conflicts(1, {"src/a.js"})
    h.pool.record_conflicts(2, {"src/b.js"})
    check("one split issue per window", h.pool.book.split_filed, {"src/a.js"})
    h.now += 700
    h.pool.record_large({"src/places.py": 1371, "src/small.py": 950})
    check(
        "a long file is split in the next window, the longest first",
        h.pool.book.split_filed,
        {"src/a.js", "src/places.py"},
    )
    h.now += 700
    h.pool.record_conflicts(3, {"src/live.py"})
    check(
        "a conflicting file goes before a long one",
        h.pool.book.split_filed,
        {"src/a.js", "src/places.py", "src/live.py"},
    )
    h2 = Rig(state(), split_lines=900, gh=gh_log())
    h2.pool.record_large({"src/ok.py": 900})
    check("at the limit is not over it", h2.pool.book.split_filed, set())
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-splitmark-"))
    (d / "gh").write_text(FAKE_GH_LOG)
    (d / "gh").chmod(0o755)
    h3 = Rig(state(), split_lines=900, gh=str(d / "gh"))
    h3.pool.record_large({"src/big.py": 1200})
    log = (d / "gh.log").read_text()
    check("a split issue is signed and marked for the review gate, and asks for verbatim moves",
          ("--body <!-- merger -->\n<!-- split -->" in log, "Move code as it is" in log), (True, True))
    # Run 7: a split logged ok was never filed (GitHub's secondary rate limit, no URL).
    limited = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-ratelimit-"))
    (limited / "gh").write_text(f"""#!/bin/sh
n=$(cat "{limited}/n" 2>/dev/null || echo 0); echo $((n + 1)) > "{limited}/n"
[ "$n" -lt "$(cat {limited}/limit)" ] && {{ echo "was submitted too quickly: secondary rate limit" >&2; exit 0; }}
echo "https://github.com/o/r/issues/88"
""")
    (limited / "gh").chmod(0o755)
    (limited / "limit").write_text("1")
    h4 = Rig(state(), split_lines=900, gh=str(limited / "gh"), gh_rate_wait_s=0)
    h4.pool.record_large({"src/big.py": 1200})
    check("rate limited once: waited out and filed", (h4.pool.book.split_filed, (limited / "n").read_text().strip(),
          any("issues/88" in e for e in h4.pool.events)), ({"src/big.py"}, "2", True))
    waits = [json.loads(ln) for ln in (h4.pool.cfg.logdir / "pool.jsonl").read_text().splitlines()
             if '"gh_rate_wait"' in ln]
    check("and the wait is logged, one line", [(w["command"], w["attempt"]) for w in waits], [("issue create --title", 1)])
    (limited / "n").unlink()
    (limited / "limit").write_text("9")
    h5 = Rig(state(), split_lines=900, gh=str(limited / "gh"), gh_rate_wait_s=0)
    h5.pool.record_large({"src/big.py": 1200})
    check("still limited: not counted as filed, logged as failed, due again later",
          (h5.pool.book.split_filed, h5.pool.book.split_times, any("could not file a split issue" in e for e in h5.pool.events)),
          (set(), [], True))


def test_a_starving_pr_holds_merges_on_its_files() -> None:
    """Cohort 52: #143 ("near here", P1) waited 5 h 40 min through 3 merger sessions while
    77 commits landed, every merge touching its files."""
    def st(*extra):
        return state([iss(1, ["stage:pr", "P1"]), iss(2, ["stage:pr", "P2"]), iss(3, ["stage:pr", "P0"])][:2 + len(extra)],
                     prs=[pr(11, 1), pr(12, 2)] + list(extra))
    h = Rig(st(), starve_hold_s=3600)
    h.step()
    h.pool.record_conflicts(11, {"src/live.py", "src/near.py"}, hard=True)
    check("one conflict left to a merger: not starving", h.pool.starving, {})
    h.pool.record_conflicts(11, {"src/near.py"}, hard=True)
    check("twice on the same file: starving, on that file", sorted(h.pool.starving[11]["files"]), ["src/near.py"])
    check("the monitor hears it", sum("PR #11 (P1) conflicted 2 times on src/near.py" in e for e in h.pool.events), 1)
    check("another PR's merge that touches the file waits; one that does not, and the starving PR itself, go",
          (h.pool.starve_hold(12, {"src/near.py", "x"}).startswith("held (it touches src/near.py, which starving PR #11"),
           h.pool.starve_hold(12, {"src/other.py"}), h.pool.starve_hold(11, {"src/near.py"})), (True, "", ""))
    for _ in range(2):
        h.pool.record_conflicts(12, {"src/near.py"}, hard=True)
    check("a P2 PR does not starve others", 12 in h.pool.starving, False)
    for _ in range(2):
        h.pool.record_conflicts(11, {"src/live.py"}, hard=True)
    h.st = st(pr(13, 3))
    h.step()
    h.now += 10
    for _ in range(2):
        h.pool.record_conflicts(13, {"src/near.py"}, hard=True)
    check("two starving PRs on one file: the P0 waits for nobody, the P1 waits for it",
          (h.pool.starve_hold(13, {"src/near.py"}), h.pool.starve_hold(11, {"src/near.py"}).endswith("starving PR #13 needs)")),
          ("", True))
    h.st = st()
    h.step()
    check("a starving PR no longer open ends its hold, and the monitor hears it",
          (13 in h.pool.starving, any("PR #13's files no longer wait for it: it is no longer open" in e for e in h.pool.events)),
          (False, True))
    h.now += 3601
    h.step()
    check("the time limit ends a hold", (h.pool.starving, h.pool.starve_hold(12, {"src/near.py"})), ({}, ""))
    real = ccpool.Session.starve_hold
    ccpool.Session.starve_hold = lambda self, pr, head, branch: "held (it touches src/near.py, which starving PR #11 needs)"
    try:
        body = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "c" * 40 + "\n"
        outcome, calls, log, _ = resweep_session(body, ALLOW_SH, record=True)
    finally:
        ccpool.Session.starve_hold = real
    check("a merge unit on such a PR is held before any sweep or gate, not merged",
          (outcome, calls, "pr merge" in log), ("held (it touches src/near.py, which starving PR #11 needs)", [], False))


def test_ux_runs_never_run_out() -> None:
    h = Rig(state(), ux=True, ux_gap_s=60, family_max=3, ux_idle_gap_s=1e12)
    ran = 0
    for i in range(8):
        h.st = state(main=str(i + 1) * 40)
        t = h.step(120)
        ran += len([u for u in t if u.kind == "ux"])
        for u in t:
            h.finish(u.key)
    check("a UX run per sha past POOL_FAMILY_MAX", ran, 8)
    check(
        "and no stuck events for UX",
        [e for e in h.pool.events if "stuck" in e and "ux" in e],
        [],
    )


def resweep_session(body: str | None, guard_script: str, sweep_ok: bool = True, record: bool = False,
                    main_check: dict | None = None, sweeps: list | None = None, devchecks: list | None = None):
    """auto_merge with a fake resweep that notes whether the merge lock was held, a gh
    whose `pr view` prints `body` (None: gh pr view fails), and a guard script."""
    calls = []
    real = ccpool.Session.resweep
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-resweep-"))

    def fake(self, pr, branch, head, env):
        calls.append((pr, branch, head, self.pool.merge_lock.locked()))
        if sweeps:
            return sweeps.pop(0)
        return sweep_ok, "FAILED tests/e2e/test_issue_20.py::test_map - boom\n" if not sweep_ok else "281 green"

    ccpool.Session.resweep = fake
    real_devcheck = ccpool.Session.devcheck_rerun

    def fake_devcheck(self, pr, branch, head, earlier, env):
        calls.append(("devcheck", earlier, self.pool.merge_lock.locked()))
        return devchecks.pop(0) if devchecks else (True, "devcheck: pass")
    ccpool.Session.devcheck_rerun = fake_devcheck
    old = os.environ.get("SWEEP_RECORD_DIR")
    os.environ["SWEEP_RECORD_DIR"] = str(d / "records")
    try:
        if record:
            (d / "records").mkdir()
            (d / "records" / f"{'c' * 40}.json").write_text("{}")
        guard = d / "guard"
        guard.write_text("#!/bin/sh\ncat > /dev/null\n" + guard_script.replace("{d}", str(d)))
        guard.chmod(0o755)
        gh = d / "gh"
        view = (f'if [ "$1 $2" = "pr view" ]; then cat {d}/body; exit 0; fi\n' if body is not None
                else 'if [ "$1 $2" = "pr view" ]; then exit 1; fi\n')
        gh.write_text(f'#!/bin/sh\necho "$*" >> {d}/gh.log\n{view}')
        gh.chmod(0o755)
        (d / "body").write_text(body or "")
        review = d / "review"
        review.write_text("#!/bin/sh\nexit 0\n")
        review.chmod(0o755)
        pool = ccpool.Pool(
            ccpool.Config(logdir=d / "pool", gh=str(gh), review_cmd=str(review), guard_cmd=str(guard)),
            fetcher=state, spawn=False,
        )
        pool.main_check_last = main_check or {}
        u = ccpool.Unit("auto", "merge", "merge:9@x", "mrg:9", 9, (0, 9),
                        {"issue": 2, "head": "c" * 40, "mergeable": "MERGEABLE", "branch": "issue-2"})
        w = ccpool.Session(pool, u, 1)
        w.dir.mkdir(parents=True)
        outcome = w.work()
    finally:
        ccpool.Session.resweep = real
        ccpool.Session.devcheck_rerun = real_devcheck
        if old is None:
            os.environ.pop("SWEEP_RECORD_DIR", None)
        else:
            os.environ["SWEEP_RECORD_DIR"] = old
    log = (d / "gh.log").read_text() if (d / "gh.log").exists() else ""
    return outcome, calls, log, pool


ALLOW_SH = "echo '{\"decision\": \"allow\"}'\n"
SWEEP_REFUSED_ONCE = ("if [ -e {d}/once ]; then " + ALLOW_SH.strip() + "; else touch {d}/once; "
                      "echo '{\"decision\": \"block\", \"reason\": \"No sweep record for this head.\"}'; fi\n")


def test_a_red_resweep_on_a_red_main_is_held() -> None:
    """PR #159 was handed back for the two tests that had just turned main red."""
    stale = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "b" * 40 + "\n"
    refuse = "echo '{\"decision\": \"block\", \"reason\": \"does not match Sweep SHA\"}'\n"
    red = {"sha": "d" * 40, "ok": False, "failing": ["tests/e2e/test_issue_20.py::test_map",
                                                     "tests/e2e/test_issue_20.py::test_mobile"]}
    outcome, calls, log, pool = resweep_session(stale, refuse, sweep_ok=False, main_check=red)
    check("main fails the same tests: held, not handed back, the monitor hears it",
          (outcome, "pr comment" in log, "issue edit" in log, any("not handed back" in e for e in pool.events)),
          ("held (main dddddddd is red with the same failures)", False, False, True))
    other = {**red, "failing": ["tests/e2e/test_issue_14.py::test_restart"]}
    outcome, _, log, _ = resweep_session(stale, refuse, sweep_ok=False, main_check=other)
    check("main red on other tests: the PR's own failure, handed back",
          (outcome, "pr comment 9" in log), ("refused by the merge gates (handed back)", True))
    outcome, _, _, _ = resweep_session(stale, refuse, sweep_ok=False, main_check={**red, "ok": True})
    check("main green: handed back", outcome, "refused by the merge gates (handed back)")
    outcome, _, _, _ = resweep_session(stale, refuse, sweep_ok=False)
    check("no main check yet: handed back, as before", outcome, "refused by the merge gates (handed back)")


def test_while_main_is_red_only_a_pr_built_on_it_merges() -> None:
    """Run 7: main was red for 25 minutes and four PRs merged meanwhile; one added a
    second failure the merged-tree check could not blame, as main failed too."""
    body = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "c" * 40 + "\n"
    red = {"sha": "d" * 40, "ok": False, "failing": ["tests/e2e/test_issue_6.py::test_map"]}
    real = ccpool.Session.contains
    try:
        for built_on_red, want in ((False, "held (main dddddddd is red; the PR is not built on it)"), (True, "ok"),
                                   (None, "ok")):
            ccpool.Session.contains = lambda self, head, sha, branch, v=built_on_red: v
            outcome, calls, log, pool = resweep_session(body, ALLOW_SH, record=True, main_check=red)
            check(f"main red, the head built on it: {built_on_red}", (outcome, "pr merge 9" in log), (want, want == "ok"))
            if built_on_red is False:
                check("held before any sweep or gate, not handed back, and the monitor hears it once",
                      (calls, "pr comment" in log, sum("waits: main dddddddd is red" in e for e in pool.events)),
                      ([], False, 1))
        ccpool.Session.contains = lambda self, head, sha, branch: False
        outcome, _, log, _ = resweep_session(body, ALLOW_SH, record=True, main_check={**red, "ok": True})
        check("main green: merged as before", outcome, "ok")
    finally:
        ccpool.Session.contains = real


RED_SWEEP = ("tests/unit .....F\nFAILED tests/unit/test_issue_50.py::test_cli_exits - AssertionError\n"
             "1 failed, 64 passed in 14.50s\n\n== sweep is red ==\n   `make verify` exited 2\n")


def test_a_red_resweep_is_handed_back_with_its_failures() -> None:
    """PRs #110, #126 and #140 were handed back with "does not match Sweep SHA" or "missing
    the green line" when their re-sweep was red; the failing tests were not named."""
    stale = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "b" * 40 + "\n"
    outcome, calls, log, pool = resweep_session(stale, ALLOW_SH, sweeps=[(False, RED_SWEEP), (False, RED_SWEEP)])
    check("handed back for the red sweep, not by the gates, never merged",
          (outcome, "pr merge" in log, "Sweep SHA" in log.split("pr comment 9", 1)[-1].split("issue edit")[0]),
          ("sweep red at the head (handed back)", False, False))
    check("the comment names the failing test and quotes the output",
          ("tests/unit/test_issue_50.py::test_cli_exits" in log, "1 failed, 64 passed" in log), (True, True))
    outcome, _, log, _ = resweep_session(stale, "echo '{\"decision\": \"block\", \"reason\": "
                                                "\"does not match Sweep SHA\"}'\n", sweep_ok=False)
    check("a re-sweep that did not run (no red sweep in its output): the gates decide, as before",
          outcome, "refused by the merge gates (handed back)")


def test_a_single_flaky_failure_is_tried_again() -> None:
    """Single flaky failures handed PRs back (#106 and #103 in the merged-tree check, #110,
    #126 and #140 in a re-sweep)."""
    stale = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "b" * 40 + "\n"
    outcome, calls, log, _ = resweep_session(stale, ALLOW_SH, sweeps=[(False, RED_SWEEP), (True, "281 green")])
    check("a red re-sweep that passes on the second try: merged", (outcome, len(calls), "pr merge 9" in log),
          ("ok", 2, True))
    other = RED_SWEEP.replace("test_issue_50.py::test_cli_exits", "test_issue_14.py::test_restart")
    outcome, _, log, pool = resweep_session(stale, ALLOW_SH, sweeps=[(False, RED_SWEEP), (False, other)])
    check("red twice, differently: held as flaky, not handed back",
          (outcome, "pr comment" in log, any("flaky" in e for e in pool.events)),
          ("held (the re-sweep failed twice, differently: flaky)", False, True))
    pool = ccpool.Pool(ccpool.Config(logdir=pathlib.Path(tempfile.mkdtemp(prefix="ccpool-flakymerge-")) / "pool",
                                     fast_check_cmd="true"), fetcher=state, spawn=False)
    w = ccpool.Session(pool, ccpool.Unit("auto", "merge", "merge:5@x", "mrg:5", 5, (0, 5), {}), 1)
    w.dir.mkdir(parents=True)
    w.git = lambda *a, **k: subprocess.CompletedProcess(a, 0, "m" * 40, "")
    w.behind_main = lambda head, branch: True
    w.main_ok = lambda main, env: True
    handed = []
    w.hand_back = lambda pr, n, text: handed.append(text)
    fail = "FAILED tests/unit/test_weather.py::test_card - flaky\n1 failed"
    for results, want, back in (
        ([(False, fail), (True, "ok")], "", 0),
        ([(False, fail), (False, fail.replace("test_card", "test_other"))],
         "held (the merged-tree check failed twice, differently: flaky)", 0),
        ([(False, fail), (False, fail)], "breaks main once merged (handed back)", 1),
    ):
        queue = list(results)
        w.fast_check = lambda wt, env: queue.pop(0)
        handed.clear()
        check(f"merged-tree check {[r[0] for r in results]}: {want or 'merges'}",
              (w.merged_check(5, 1, "h" * 40, "issue-1", {}), len(handed), queue), (want, back, []))


def test_a_flaky_red_main_gets_an_issue() -> None:
    """main went red four times on an e2e weather-card flake and nobody filed a fix. Only a
    failure and a pass at one commit make a test flaky: cohort 52's main break (#194) was
    labeled flaky because a later commit was green with no change to the test."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-flaky-"))
    gh = d / "gh"
    gh.write_text(FAKE_GH_LOG)
    gh.chmod(0o755)
    h = Rig(state(main="1" * 40), main_check=True, gh=str(gh))
    red = "FAILED tests/e2e/test_issue_14.py::test_weather_card - Timeout\n== sweep is red ==\n"
    runs = {"1": [(False, red), (False, red)], "2": [(True, "green")], "3": [(False, red), (True, "green")]}
    h.pool.sweep_main = lambda sha: runs[sha[0]].pop(0)
    h.pool.commits_since = lambda old, new: []
    for k in "12":
        h.st = state(main=k * 40)
        h.step()
        for _ in range(100):
            if not h.pool.main_check_running:
                break
            time.sleep(0.05)
    log = (d / "gh.log").read_text()
    check("red at one commit, green at the next: a main break and its fix, not a flake",
          ("Flaky test:" in log, any("flaky" in e for e in h.pool.events)), (False, False))
    h.st = state(main="3" * 40)
    h.step()
    for _ in range(100):
        if not h.pool.main_check_running:
            break
        time.sleep(0.05)
    log = (d / "gh.log").read_text()
    check("red and then green at the same commit: a flaky-test issue, marked as such",
          ([ln.split(" --body ")[0] for ln in log.splitlines() if ln.startswith("issue create --title Flaky")],
           ccpool.FLAKY_MARK in log, any("failed and passed at one commit (main at 33333333)" in e for e in h.pool.events)),
          (["issue create --title Flaky test: tests/e2e/test_issue_14.py::test_weather_card --label type:bug "
            "--label P1 --label stage:triage"], True, True))


def test_known_flaky_tests_do_not_turn_main_red() -> None:
    """Run 8: tests the supervisor had filed as flaky (#92, #135) turned main red again,
    holding merges, and PR #107 was handed back for one."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-knownflaky-"))
    gh = d / "gh"
    gh.write_text(FAKE_GH_LOG)
    gh.chmod(0o755)
    (d / "gh.list").write_text(json.dumps([{"number": 92, "title": "Flaky test: tests/unit/test_a.py::test_x",
                                            "body": ccpool.FLAKY_MARK + "\n- `tests/unit/test_a.py::test_x`",
                                            "comments": []},
                                           {"number": 194, "title": "Flaky test: tests/unit/test_b.py::test_y",
                                            "body": "main went red ... green again ...\n- `tests/unit/test_b.py::test_y`",
                                            "comments": []}]))
    h = Rig(state(main="1" * 40), main_check=True, gh=str(gh))
    flaky = "FAILED tests/unit/test_a.py::test_x - boom\n== sweep is red ==\n"
    other = "FAILED tests/unit/test_b.py::test_y - boom\n== sweep is red ==\n"
    runs = {"1": [(False, flaky)], "2": [(False, other), (True, "45 green")], "3": [(False, other), (False, other)]}
    h.pool.sweep_main = lambda sha: runs[sha[0]].pop(0)
    h.pool.commits_since = lambda old, new: []
    h.pool.touched = lambda old, new, paths: []
    for k in "123":
        h.st = state(main=k * 40)
        h.step()
        for _ in range(100):
            if not h.pool.main_check_running:
                break
            time.sleep(0.05)
        if k == "1":
            log = (d / "gh.log").read_text()
            check("only a known flaky test failed: main green, a comment on its issue, no `main is red`",
                  (h.pool.main_check_last["ok"], "issue comment 92" in log, "--title main is red" in log),
                  (True, True, False))
        if k == "2":
            log = (d / "gh.log").read_text()
            check("another test failed and passed on the second run: main green, the test reported as flaky",
                  (h.pool.main_check_last["ok"], "--title main is red" in log,
                   "passed when they ran again at the same commit" in log), (True, False, True))
    check("failed twice: main red, as before",
          (h.pool.main_check_last["ok"], "--title main is red" in (d / "gh.log").read_text()),
          (False, True))
    # A PR whose re-sweep fails only a known flaky test is held, not handed back.
    stale = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "b" * 40 + "\n"
    refuse = "echo '{\"decision\": \"block\", \"reason\": \"does not match Sweep SHA\"}'\n"
    real = ccpool.Pool.flaky_tests
    ccpool.Pool.flaky_tests = lambda self, max_age=600: {"tests/unit/test_issue_50.py::test_cli_exits": 135}
    try:
        outcome, _, log, pool = resweep_session(stale, refuse, sweeps=[(False, RED_SWEEP), (False, RED_SWEEP)])
    finally:
        ccpool.Pool.flaky_tests = real
    check("a re-sweep red only on a known flaky test: held, not handed back",
          (outcome, "pr comment" in log), ("held (the re-sweep failed only known flaky tests: #135)", False))
    stale2 = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "b" * 40 + "\n"
    outcome, _, log, pool = resweep_session(stale2, ALLOW_SH, sweeps=[(False, RED_SWEEP), (True, "281 green")])
    check("a re-sweep that fails, then passes at the same head: merged, and the test recorded as flaky",
          (outcome, any("failed and passed at one commit (PR #9's head cccccccc)" in e for e in pool.events)),
          ("ok", True))


def test_a_check_that_failed_on_the_machine_blames_nobody() -> None:
    """Run 9: /tmp ran out of inodes; fast checks handed PRs #57 and #59 back, and a main
    check started then hung for its 3600 s."""
    nospace = "error: Failed to install\n  Caused by: No space left on device (os error 28)\n== sweep is red ==\n"
    stale = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "b" * 40 + "\n"
    refuse = "echo '{\"decision\": \"block\", \"reason\": \"does not match Sweep SHA\"}'\n"
    outcome, _, log, _ = resweep_session(stale, refuse, sweeps=[(False, nospace), (False, nospace)])
    check("a re-sweep that failed on the machine: held, not handed back",
          (outcome, "pr comment" in log), ("held (the re-sweep failed on the machine: No space left on device)", False))
    h = Rig(state(), main_check=True, gh="true")
    check("a main check that failed on the machine could not run: no red main", h.pool.second_look("a" * 40, nospace)[0],
          None)
    check("the main check has a bounded limit", h.pool.cfg.main_check_s, 1200)
    real = os.statvfs
    try:
        os.statvfs = lambda p: os.statvfs_result((4096, 4096, 100, 50, 50, 1000, 10, 10, 0, 255))
        h.pool.check_disk()
        h.pool.check_disk()
        os.statvfs = lambda p: os.statvfs_result((4096, 4096, 100, 50, 50, 1000, 900, 900, 0, 255))
        h.pool.check_disk()
    finally:
        os.statvfs = real
    said = [e for e in h.pool.events if "file system under" in e]
    check("nearly out of inodes: the monitor hears it once, and once when it recovers", (len(said), "inodes under 5% free" in said[0], "room again" in said[1]),
          (2, True, True))


def test_the_second_try_reruns_only_the_failing_tests() -> None:
    """Cohort 52: a red main check's second full run cost 0.7 h a cohort, and every held
    PR's second full re-sweep as long again; the failing tests alone say the same."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-rerun-"))
    h = Rig(state(main="1" * 40), main_check=True, gh="true",
            rerun_cmd=f"echo {{tests}} > {d}/ran; grep -q flaky {d}/ran")
    check("no [sweep] rerun: the caller runs the whole check again", Rig(state()).pool.rerun_tests(str(d), ["a::b"]),
          (None, ""))
    check("no node id to name: the whole check again", h.pool.rerun_tests(str(d), ["`make verify` exited 2"]), (None, ""))
    ok, _ = h.pool.rerun_tests(str(d), ["tests/e2e/test_a.py::test_flaky[chromium]"])
    check("the command gets the failing tests, quoted", (ok, (d / "ran").read_text().strip()),
          (True, "tests/e2e/test_a.py::test_flaky[chromium]"))
    check("and fails when they fail", h.pool.rerun_tests(str(d), ["tests/e2e/test_a.py::test_real"])[0], False)
    red = "FAILED tests/e2e/test_a.py::test_real - boom\n== sweep is red ==\n"
    full = []
    h.pool.sweep_main = lambda sha: full.append(sha) or (True, "green")
    h.pool.rerun_at = lambda ref, tests, fetch="main": (False, "1 failed")
    check("the failing tests fail again alone: main is red, with no second full run",
          (h.pool.second_look("a" * 40, red)[0], full), (False, []))
    h.pool.rerun_at = lambda ref, tests, fetch="main": (True, "1 passed")
    check("they pass alone: flaky, main green, still no second full run",
          (h.pool.second_look("b" * 40, red)[0], full, any("failed and passed at one commit" in e for e in h.pool.events)),
          (True, [], True))
    stale = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "b" * 40 + "\n"
    refuse = "echo '{\"decision\": \"block\", \"reason\": \"does not match Sweep SHA\"}'\n"
    real = ccpool.Pool.rerun_at
    ccpool.Pool.rerun_at = lambda self, ref, tests, fetch="main": (False, "1 failed")
    try:
        outcome, calls, log, _ = resweep_session(stale, refuse, sweeps=[(False, RED_SWEEP)])
    finally:
        ccpool.Pool.rerun_at = real
    check("a re-sweep whose failing tests fail again alone: handed back after one full sweep",
          (outcome, len(calls), "pr comment 9" in log), ("sweep red at the head (handed back)", 1, True))


def test_a_held_pr_is_not_reswept_at_the_same_head() -> None:
    """Cohort 52: PR #157, built on a broken main, was re-swept 15 times at one head
    (19:49-20:46), each time held instead of rebased."""
    stale = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "b" * 40 + "\n"
    refuse = "echo '{\"decision\": \"block\", \"reason\": \"does not match Sweep SHA\"}'\n"
    red = {"sha": "d" * 40, "ok": False, "failing": ["tests/e2e/test_issue_20.py::test_map"]}
    outcome, calls, _, pool = resweep_session(stale, refuse, sweep_ok=False, main_check=red)
    check("first try: re-swept, held as main's failure", (outcome.startswith("held (main dddddddd is red"), len(calls)),
          (True, 1))
    swept = []
    real = ccpool.Session.resweep
    ccpool.Session.resweep = lambda self, pr, branch, head, env: swept.append(head) or (False, "")
    try:
        u = ccpool.Unit("auto", "merge", "merge:9@x", "mrg:9", 9, (0, 9),
                        {"issue": 2, "head": "c" * 40, "mergeable": "MERGEABLE", "branch": "issue-2"})
        again = ccpool.Session(pool, u, 2)
        again.dir.mkdir(parents=True)
        out2 = again.work()
        u2 = ccpool.Unit("auto", "merge", "merge:9@y", "mrg:9", 9, (0, 9), dict(u.info))
        moved = ccpool.Session(pool, u2, 3)
        moved.dir.mkdir(parents=True)
        moved.work()
    finally:
        ccpool.Session.resweep = real
    check("the same head on the same main: the same hold, not swept again", (out2, swept[:1]), (outcome, ["c" * 40]))
    check("main moved (a new key): swept again", len(swept), 1)
    # The failure is main's and main moved on: the supervisor rebases instead of handing back.
    realb = ccpool.Session.behind_main
    ccpool.Session.behind_main = lambda self, head, branch: True
    try:
        h = Rig(state())
        h.pool.red_failed["tests/unit/test_issue_50.py::test_cli_exits"] = "e" * 40
        w = ccpool.Session(h.pool, ccpool.Unit("auto", "merge", "merge:9@z", "mrg:9", 9, (0, 9),
                                               {"issue": 2, "head": "c" * 40, "branch": "issue-2"}), 4)
        out3 = w.sweep_hand_back(9, 2, "c" * 40, RED_SWEEP)
    finally:
        ccpool.Session.behind_main = realb
    check("a failure seen on a red main, head behind main: a rebase, not a hand-back",
          (out3, h.pool.book.rebase_wanted), ("held (its failure is main's: asked for a rebase onto main)", {9: "c" * 40}))
    h2 = Rig(state([iss(2, ["stage:pr"])], prs=[pr(9, 2, head="c" * 40)]))
    h2.pool.book.rebase_wanted[9] = "c" * 40
    check("the next plan rebases the mergeable PR", [u.kind for u in h2.step()], ["rebase"])
    h3 = Rig(state([iss(2, ["stage:pr"])], prs=[pr(9, 2, head="f" * 40)]))
    h3.pool.book.rebase_wanted[9] = "c" * 40
    check("a new head: merged as usual, the request dropped", ([u.kind for u in h3.step()], h3.pool.book.rebase_wanted),
          (["merge"], {}))


def test_a_flaky_test_has_a_deadline() -> None:
    """Cohort 52: test_issue_96 failed 29 runs (1 in 3 even alone) and nobody fixed it."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-flakydl-"))
    gh = d / "gh"
    gh.write_text(FAKE_GH_LOG)
    gh.chmod(0o755)
    mark = ccpool.FLAKY_MARK
    (d / "gh.list").write_text(json.dumps([{
        "number": 96, "title": "Flaky test: t", "createdAt": "2026-10-24T10:00:00Z", "labels": [{"name": "P2"}],
        "body": mark + "\n- `tests/e2e/test_issue_96.py::test_resize`",
        "comments": [{"createdAt": "2026-10-24T15:00:00Z", "body": mark + "\n- `tests/e2e/test_issue_21.py::test_order`"},
                     {"createdAt": "2026-10-24T09:00:00Z", "body": "- `tests/e2e/test_old.py::test_x` (no mark)"}]}]))
    h = Rig(state(), gh=str(gh), flaky_merges=10)
    h.pool.merges_since = lambda at: {"2026-10-24T10:00:00Z": 12, "2026-10-24T15:00:00Z": 3}.get(at, 99)
    h.pool.flaky_deadlines()
    log = (d / "gh.log").read_text()
    check("past 10 merges: one comment naming only that test, and the issue raised to P1",
          ("tests/e2e/test_issue_96.py::test_resize` (12 merges)" in log, "test_issue_21" in log.split("issue comment")[-1],
           "test_old" in log.split("issue comment")[-1], "issue edit 96 --add-label P1 --remove-label P2" in log),
          (True, False, False, True))
    rows = json.loads((d / "gh.list").read_text())
    rows[0]["comments"].append({"createdAt": "2026-10-25T00:00:00Z",
                                "body": ccpool.FLAKY_DEADLINE + "\n- `tests/e2e/test_issue_96.py::test_resize`"})
    (d / "gh.list").write_text(json.dumps(rows))
    (d / "gh.log").write_text("")
    h.pool.flaky_known = (0.0, {})
    h.pool.flaky_deadlines()
    check("said once: no second comment", "issue comment" in (d / "gh.log").read_text(), False)


GATE7_ONCE = ("if [ -e {d}/once ]; then " + "echo '{\"decision\": \"allow\"}'" + "; else touch {d}/once; "
              "echo '{\"decision\": \"block\", \"reason\": \"Gate 7 (live check): the live check ran at "
              "`aeb98a0d9c57`, and the head `8857c8d4597a` changes something else.\"}'; fi\n")


def test_gate7_after_a_rebase_replays_the_live_check() -> None:
    """Resolving a conflict changes the removed lines, so gate 7 refused every rebased head
    whose change moved; each refusal cost a worker session (#5: 53 minutes)."""
    fresh = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "c" * 12 + "\n"
    outcome, calls, log, _ = resweep_session(fresh, GATE7_ONCE, record=True)
    check("the supervisor replays the live check outside the lock, then merges, with no extra sweep",
          (outcome, calls, "pr merge 9" in log), ("ok", [("devcheck", "aeb98a0d9c57", False)], True))
    outcome, calls, log, _ = resweep_session(fresh, GATE7_ONCE, record=True,
                                             devchecks=[(False, "  script 1: exit 1\n    | AssertionError: no card")])
    check("a replay that fails: handed back with what failed, not merged",
          (outcome, "AssertionError: no card" in log, "pr merge" in log),
          ("live check fails on the rebased head (handed back)", True, False))
    outcome, _, log, _ = resweep_session(fresh, GATE7_ONCE, record=True,
                                         devchecks=[(None, "devcheck: no free slot in time; NOT run")])
    check("a replay that could not run: held, not handed back", (outcome, "pr comment" in log),
          ("held (the live check could not be replayed)", False))
    always = ("echo '{\"decision\": \"block\", \"reason\": \"Gate 7 (live check): the live check ran at "
              "`aeb98a0d9c57`, and the head `8857c8d4597a` changes something else.\"}'\n")
    outcome, calls, log, _ = resweep_session(fresh, always, record=True)
    check("still refused after a passing replay: handed back by the gates, replayed once",
          (outcome, sum(1 for c in calls if c[0] == "devcheck")), ("refused by the merge gates (handed back)", 1))
    m = ccpool.merger_prompt(
        ccpool.Unit("merger", "land", "k", "f", 9, (0,), {"issue": 2, "head": "c" * 40, "mergeable": "X"}), "45m")
    check("the merger reruns it without judging whether the behavior changed",
          "naming another sha than your" in " ".join(m.split()), True)


def test_auto_merge_resweeps_outside_the_merge_lock() -> None:
    """A merger's clean re-rebase comes without a sweep; re-sweeping it under the merge
    lock (5-10 minutes each) held every other merge, and their PRs conflicted again."""
    stale = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "b" * 40 + "\n"
    outcome, calls, log, _ = resweep_session(stale, ALLOW_SH)
    check("a PR body naming another head: swept before the lock, then merged",
          (outcome, calls, "pr merge 9" in log), ("ok", [(9, "issue-2", "c" * 40, False)], True))
    fresh = "<!-- worker -->\nFixes #2\n- **Sweep SHA:** " + "c" * 12 + "\n"
    outcome, calls, _, _ = resweep_session(fresh, ALLOW_SH)
    check("its own head but no sweep record: swept first", calls, [(9, "issue-2", "c" * 40, False)])
    outcome, calls, _, _ = resweep_session(fresh, ALLOW_SH, record=True)
    check("its own head and a green record: no sweep", (outcome, calls), ("ok", []))
    waived = fresh + "- **Sweep:** waived - docs only\n"
    outcome, calls, _, _ = resweep_session(waived, ALLOW_SH)
    check("a waived sweep at its own head: no sweep", (outcome, calls), ("ok", []))
    outcome, calls, log, _ = resweep_session(None, SWEEP_REFUSED_ONCE)
    check("the body unreadable but the gates want a sweep: the lock is left, then swept and merged",
          (outcome, calls, log.count("pr merge 9")), ("ok", [(9, "issue-2", "c" * 40, False)], 1))
    outcome, calls, log, _ = resweep_session(stale, "echo '{\"decision\": \"block\", \"reason\": "
                                                    "\"No sweep record for this head.\"}'\n")
    check("a sweep refusal after the head was swept here is handed back, swept once",
          (outcome, len(calls), "pr merge" in log), ("refused by the merge gates (handed back)", 1, False))


def test_scratch_paths_are_the_repositorys() -> None:
    """Fixed names under /tmp collided across repositories: a leftover /tmp/mrg-121-auto
    from another loop failed one rebase eight times (#5)."""
    claim = {"issue": 5, "token": "t", "from": "stage:triage", "title": "x"}
    text = ccpool.worker_prompt(claim, "3", "45m", "u")
    m = ccpool.merger_prompt(
        ccpool.Unit("merger", "land", "k", "f", 9, (0,), {"issue": 2, "head": "c" * 40, "mergeable": "X"}), "45m")
    t = ccpool.tester_prompt(ccpool.Unit("tester", "live", "k", "f", 4, (2,), {"title": "x"}), 8021, "60m")
    check("every prompt names paths under the repository's scratch directory, none under /tmp itself",
          (f"{SCRATCH}/wrk-3-5-wt" in text, f"{SCRATCH}/mrg-9-wt" in m, f"{SCRATCH}/tst-8021-" in t,
           any(f"/tmp/{x}-" in y for x in ("wrk", "mrg", "tst") for y in (text, m, t))),
          (True, True, True, False))
    check("the supervisor's own worktrees too", ccpool.scratch("mrg-121-auto"), f"{SCRATCH}/mrg-121-auto")
    import shutil
    shutil.rmtree(SCRATCH)
    Rig(state())
    check("the pool makes the directory", pathlib.Path(SCRATCH).is_dir(), True)
    for role in ("worker", "merger", "tester"):
        skill = (HERE.parent / "skills" / f"issuelooper-{role}" / "SKILL.md").read_text()
        check(f"the {role} skill's scratch paths are under $ISSUELOOPER_TMP",
              ("/tmp/wrk-" in skill or "/tmp/mrg-" in skill or "/tmp/tst-" in skill, "$ISSUELOOPER_TMP/" in skill),
              (False, True))


def test_worker_prompt_points_to_the_project() -> None:
    claim = {"issue": 5, "token": "t", "from": "stage:triage", "title": "x"}
    text = ccpool.worker_prompt(claim, "1", "45m", "u")
    check("the worker reads the project's own facts", ".issuelooper/project.md" in text, True)
    check("and the issue's newest comments (a reopened issue says why there)", "newest" in text, True)
    check("the renamed skill", "issuelooper-worker skill" in text, True)
    check("the review rules come from the config", ".issuelooper/review_rules.md" in text, True)
    m = ccpool.merger_prompt(
        ccpool.Unit("merger", "land", "k", "f", 9, (0,), {"issue": 2, "head": "c" * 40, "mergeable": "X"}), "45m")
    check("the merger runs review_gate.py", ('review_gate.py" --pr 9' in m, "issuelooper-merger" in m), (True, True))
    check("and catches up with a main that moved, without a second sweep for a clean rebase",
          ("git merge-base --is-ancestor origin/main HEAD" in m, "do not sweep again" in " ".join(m.split()),
           "three rounds" in m),
          (True, True, True))


FAKE_AGY_WRAP = r"""#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
with open(sys.argv[0] + ".calls", "a") as f:
    f.write(json.dumps(args) + "\n")
if "--conversation" in args:
    print(json.dumps({"event": "result", "result": {"response": "pushed"}}), flush=True)
    sys.exit(0)
print(json.dumps({"event": "init", "conversation_id": "conv-1"}), flush=True)
time.sleep(120)
"""


FAKE_AGY_STALL = r"""#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
with open(sys.argv[0] + ".calls", "a") as f:
    f.write(json.dumps(args) + "\n")
if "--conversation" in args:
    print(json.dumps({"event": "result", "result": {"response": "continued and finished"}}), flush=True)
    sys.exit(0)
print(json.dumps({"event": "init", "conversation_id": "conv-7"}), flush=True)
step = sys.argv[0].endswith("busy")
state = "ACTIVE" if step else "DONE"
if sys.argv[0].endswith("midreply"):  # a reply that started and went quiet
    print(json.dumps({"event": "step_update", "step_update": {"step_index": 3, "state": "ACTIVE",
          "step_type": "agent_response", "text_delta": "Reproduced. Now the fix"}}), flush=True)
else:
    print(json.dumps({"event": "step_update", "step_update": {"step_index": 3, "state": state,
          "step_type": "tool", "tool_name": "run_command"}}), flush=True)
time.sleep(40 if step else 120)
print(json.dumps({"event": "result", "result": {"response": "the long command ended"}}), flush=True)
"""


FAKE_CLAUDE_STALL = r"""#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
with open(sys.argv[0] + ".calls", "a") as f:
    f.write(json.dumps(args) + "\n")
if "--resume" in args:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "s7",
          "result": "continued and finished"}), flush=True)
    sys.exit(0)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s7"}), flush=True)
busy = sys.argv[0].endswith("busy")
print(json.dumps({"type": "assistant", "session_id": "s7", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}}]}}), flush=True)
if not busy:
    print(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "3 passed"}]}}), flush=True)
time.sleep(40 if busy else 120)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "s7",
      "result": "the long command ended"}), flush=True)
"""


def stall_session(name, cli="agy"):
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-stall-"))
    agy = d / name
    agy.write_text(FAKE_CLAUDE_STALL if cli == "claude" else FAKE_AGY_STALL)
    agy.chmod(0o755)
    pool = ccpool.Pool(
        ccpool.Config(logdir=d / "pool", ccrole=str(agy), render="cat", timeouts={"tester": "60m"},
                      stall_s=2),
        fetcher=state, spawn=False,
    )
    w = ccpool.Session(pool, ccpool.Unit("tester", "ux", "ux:abc", "tst:ux:abc", None, (2,), {}), 1)
    w.dir.mkdir(parents=True)
    rc, _, timed_out = w.session("walk the app", dict(os.environ, CCROLE_TESTER_CLI=cli))
    calls = [json.loads(x) for x in (d / f"{name}.calls").read_text().splitlines()]
    return rc, timed_out, calls


FAKE_AGY_IDLE = r"""#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
with open(sys.argv[0] + ".calls", "a") as f:
    f.write(json.dumps(args) + "\n")
if "--conversation" in args and not sys.argv[0].endswith("always"):
    print(json.dumps({"event": "result", "result": {"response": "ran it in the foreground; done"}}), flush=True)
    sys.exit(0)
print(json.dumps({"event": "init", "conversation_id": "conv-9"}), flush=True)
# A tool still running (no DONE), the turn over, and agy waiting for the background task.
print(json.dumps({"event": "step_update", "step_update": {"step_index": 4, "state": "ACTIVE",
      "step_type": "tool", "tool_name": "run_command"}}), flush=True)
print("root agent idle; waiting up to 1h40m0s for 1 background task(s)", file=sys.stderr, flush=True)
time.sleep(120)
"""


def test_idle_on_a_background_task_is_resumed() -> None:
    """agy sessions sat until their 45-minute timeout with the turn over, waiting for a
    background task (8 in one run); the tool still read as running, so no stall fired."""
    for name, want_calls, nudged in (("agy-idle", 2, True), ("agy-idle-always", 3, True)):
        d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-idle-"))
        agy = d / name
        agy.write_text(FAKE_AGY_IDLE)
        agy.chmod(0o755)
        pool = ccpool.Pool(ccpool.Config(logdir=d / "pool", ccrole=str(agy), render="cat", timeouts={"worker": "60m"},
                                         stall_s=0, background_idle_s=1), fetcher=state, spawn=False)
        w = ccpool.Session(pool, ccpool.Unit("worker", "implement", "wrk:5@x", "wrk:5", 5, (4,), {}), 1)
        w.dir.mkdir(parents=True)
        t0 = time.time()
        rc, _, timed_out = w.session("do issue 5", dict(os.environ, CCROLE_WORKER_CLI="agy"))
        calls = [json.loads(x) for x in (d / f"{name}.calls").read_text().splitlines()]
        if name == "agy-idle":
            check("idle on a background task: resumed in the same conversation with a nudge, and finished",
                  (rc, timed_out, len(calls), calls[1][-2:], "in the foreground" in calls[1][calls[1].index("--print") + 1]),
                  (0, False, want_calls, ["--conversation", "conv-9"], nudged))
        else:
            check("still idle after two nudges: ended early, not at the timeout",
                  (rc, len(calls), time.time() - t0 < 90,
                   "ended: turn over, waiting on a background task" in (w.dir / "resumes.log").read_text()),
                  (0, want_calls, True, True))
    claude = ccpool.Session(pool, ccpool.Unit("worker", "implement", "wrk:6@x", "wrk:6", 6, (4,), {}), 2)
    claude.cli = ccpool.agent_cli.get("claude")
    claude.dir.mkdir(parents=True)
    (claude.dir / "session.err").write_text("root agent idle; waiting up to 1h for 1 background task(s)\n")
    check("a CLI without that state is never taken for it", claude.background_idle(0), None)


FAKE_AGY_UNSEEN = r"""#!/usr/bin/env python3
import json, os, sqlite3, sys, time
with open(sys.argv[0] + ".calls", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
print(json.dumps({"event": "init", "conversation_id": "conv-7"}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"step_index": 33, "state": "ACTIVE",
      "step_type": "tool", "tool_name": "run_command"}}), flush=True)
# The agent goes on past the stuck step and finishes; only agy's own record shows it.
c = sqlite3.connect(os.path.join(os.environ["AGY_CONVERSATIONS_DIR"], "conv-7.db"))
c.execute("create table steps (idx integer primary key, step_type integer, status integer)")
c.executemany("insert into steps values (?, ?, ?)", [(33, 132, 1), (34, 132, 3), (35, 15, 3)])
c.commit()
c.close()
time.sleep(120)
"""


def test_a_stream_stuck_behind_a_finished_conversation_ends_early() -> None:
    """Nine agy mergers in one run: the stream stopped at a command agy lost track of,
    the agent finished unseen, and the process sat until the 45-minute timeout."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-unseen-"))
    agy = d / "agy-unseen"
    agy.write_text(FAKE_AGY_UNSEEN)
    agy.chmod(0o755)
    pool = ccpool.Pool(ccpool.Config(logdir=d / "pool", ccrole=str(agy), render="cat", timeouts={"merger": "45m"},
                                     stall_s=0, background_idle_s=0, unseen_idle_s=1), fetcher=state, spawn=False)
    m = ccpool.Session(pool, ccpool.Unit("merger", "land", "mrg:5@a@b", "mrg:5", 5, (4,), {}), 1)
    m.dir.mkdir(parents=True)
    t0 = time.time()
    os.environ["AGY_CONVERSATIONS_DIR"] = str(d)  # the supervisor reads agy's record, and so does the fake
    try:
        rc, _, timed_out = m.session("resolve PR 5", dict(os.environ, CCROLE_MERGER_CLI="agy"))
    finally:
        os.environ.pop("AGY_CONVERSATIONS_DIR", None)
    calls = (d / "agy-unseen.calls").read_text().splitlines()
    check("a conversation finished behind a stuck stream: ended early, not resumed, not a timeout",
          (rc, timed_out, len(calls), time.time() - t0 < 90,
           "the conversation finished without it" in (m.dir / "resumes.log").read_text()),
          (0, False, 1, True, True))
    # Run 8: two mergers had replied "waiting for the sweep" while it ran; ending them killed it.
    tmp = d / "scratch"
    (tmp / "mrg-5-wt").mkdir(parents=True)
    old_tmp = os.environ.get("ISSUELOOPER_TMP")
    os.environ["ISSUELOOPER_TMP"] = str(tmp)
    spin = subprocess.Popen([sys.executable, "-c", "import time\nt = time.time()\nwhile time.time() - t < 20: pass"],
                            cwd=tmp / "mrg-5-wt")
    idle = subprocess.Popen(["sleep", "60"], cwd=tmp / "mrg-5-wt")
    m2 = ccpool.Session(pool, ccpool.Unit("merger", "land", "mrg:5@c@d", "mrg:5", 5, (4,), {}), 2)
    m2.dir.mkdir(parents=True)
    (d / "agy-unseen.calls").unlink()
    (d / "conv-7.db").unlink()
    os.environ["AGY_CONVERSATIONS_DIR"] = str(d)
    try:
        check("a sweep that works is busy, a server that sleeps is not", m2.busy_processes(), [spin.pid])
        t0 = time.time()
        rc, _, timed_out = m2.session("resolve PR 5", dict(os.environ, CCROLE_MERGER_CLI="agy"))
        log = (m2.dir / "resumes.log").read_text()
        check("it waits while the sweep works, then ends as finished, the sweep not killed",
              (rc, timed_out, "waiting for 1 process(es)" in log, "finished without it" in log,
               time.time() - t0 >= 15, spin.wait(5)), (0, False, True, True, True, 0))
    finally:
        os.environ.pop("AGY_CONVERSATIONS_DIR", None)
        if old_tmp is None:
            os.environ.pop("ISSUELOOPER_TMP", None)
        else:
            os.environ["ISSUELOOPER_TMP"] = old_tmp
        for p in (spin, idle):
            if p.poll() is None:
                p.kill()


def test_silent_model_is_resumed() -> None:
    rc, timed_out, calls = stall_session("agy-silent")
    check("silent after a finished step: resumed in the same conversation", (rc, timed_out, len(calls)), (0, False, 2))
    check("told why", "stopped responding" in calls[1][calls[1].index("--print") + 1], True)
    rc, timed_out, calls = stall_session("agy-busy")
    check("silent while a tool is running: left alone", (rc, len(calls)), (0, 1))
    rc, timed_out, calls = stall_session("agy-midreply")
    check("silent in the middle of a reply: resumed", (rc, timed_out, len(calls)), (0, False, 2))
    rc, timed_out, calls = stall_session("claude-silent", "claude")
    check("claude: silent after a tool result: resumed", (rc, timed_out, len(calls)), (0, False, 2))
    check("claude: in the same session, told why",
          (calls[1][calls[1].index("--resume") + 1], "stopped responding" in calls[1][calls[1].index("-p") + 1]),
          ("s7", True))
    rc, timed_out, calls = stall_session("claude-busy", "claude")
    check("claude: a tool_use with no result yet is left alone", (rc, len(calls)), (0, 1))


def test_orphaned_stage_pr_goes_back_to_the_queue() -> None:
    """GitHub closed PR #143 when a force-push made its head its base; #82 stayed at
    stage:pr with no open PR, and #141 (blocked on it) waited too."""
    old = iss(82, ["stage:pr", "P1"], updated="2033-05-18T03:00:00Z")  # NOW - ~33 min
    h = Rig(state([old]))
    started = h.step()
    check("an old stage:pr issue with no open PR is requeued",
          [(u.role, u.kind, u.number) for u in started], [("auto", "requeue", 82)])
    fresh = iss(83, ["stage:pr", "P1"], updated="2033-05-18T03:30:00Z")  # NOW - ~3 min
    check("a fresh one is left alone (its PR may be landing)", Rig(state([fresh])).step(), [])
    check("one with an open PR is the merge path's", [u.kind for u in Rig(state([old], prs=[pr(143, 82, draft=True)])).step()], [])


def test_requeue_looks_again_before_moving() -> None:
    """The plan's snapshot can predate the supervisor's own merge by seconds: #28 was
    requeued three seconds after its PR merged, before the label edit that follows."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-requeue-"))
    log = d / "calls"

    def outcome(pr_state: str) -> tuple[str, list[str]]:
        gh = d / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> {log}\n'
            'case "$1 $2" in\n'
            "  'issue view') echo '{\"state\": \"OPEN\", \"labels\": [{\"name\": \"stage:pr\"}]}' ;;\n"
            f"  'pr list') echo '[{{\"state\": \"{pr_state}\"}}]' ;;\n"
            "esac\n"
        )
        gh.chmod(0o755)
        log.write_text("")
        pool = ccpool.Pool(ccpool.Config(logdir=d / "pool", gh=str(gh)), fetcher=state, spawn=False)
        w = ccpool.Session(pool, ccpool.Unit("auto", "requeue", "requeue:28@x", "rcl:28", 28, (1, 28), {}), 5)
        w.dir.mkdir(parents=True, exist_ok=True)
        return w.requeue({}), [c for c in log.read_text().splitlines() if c.startswith("issue edit")]

    got, edits = outcome("MERGED")
    check("a PR merged meanwhile: left alone", (got, edits), ("skipped (its PR is merged)", []))
    got, edits = outcome("CLOSED")
    check("closed without a merge: back to stage:triage",
          (got, edits), ("ok", ["issue edit 28 --remove-label stage:pr --add-label stage:triage"]))


def test_wrap_up_fits_the_timeout() -> None:
    """With a 20-minute timeout a 35-minute wrap-up never came: the worker was killed
    first, and the prompt promised a wrap-up it would never get."""
    cfg = ccpool.Config(timeouts={**ccpool.Config().timeouts, "worker": "20m"})
    check("a short timeout wraps up at three quarters of it", cfg.wrap_s(), 900.0)
    cfg = ccpool.Config(timeouts={**ccpool.Config().timeouts, "worker": "45m"})
    check("one longer than POOL_WORKER_WRAP_S keeps it (35 of 45 minutes)", cfg.wrap_s(), cfg.worker_wrap_s)
    check("off stays off", ccpool.Config(worker_wrap_s=0).wrap_s(), 0.0)


def test_worker_wrap_up() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-wrap-"))
    agy = d / "ccrole"
    agy.write_text(FAKE_AGY_WRAP)
    agy.chmod(0o755)
    pool = ccpool.Pool(
        ccpool.Config(logdir=d / "pool", ccrole=str(agy), render="cat", timeouts={"worker": "60m"},
                      worker_wrap_s=1, worker_wrap_budget_s=90, retry_pause_max_s=0),
        fetcher=state, spawn=False,
    )
    w = ccpool.Session(pool, ccpool.Unit("worker", "implement", "wrk:5@x", "wrk:5", 5, (4,), {}), 1)
    w.dir.mkdir(parents=True)
    t0 = time.time()
    rc, elapsed, timed_out = w.session("do issue 5", dict(os.environ))
    calls = [json.loads(x) for x in (d / "ccrole.calls").read_text().splitlines()]
    check("a worker past POOL_WORKER_WRAP_S is resumed once", len(calls), 2)
    second = calls[-1]
    check(
        "in the same conversation, told to wrap up, with the short budget",
        (second[second.index("--conversation") + 1], "Time is almost up" in second[second.index("--print") + 1],
         second[second.index("--print-timeout") + 1]),
        ("conv-1", True, "90s"),
    )
    check("and ends cleanly well before its timeout", (rc, timed_out, time.time() - t0 < 60), (0, False, True))


FAKE_AGY_RETRY = r"""#!/usr/bin/env python3
import json, sys, time
with open(sys.argv[0] + ".calls", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
print(json.dumps({"event": "init", "conversation_id": "conv-9"}), flush=True)
print(json.dumps({"event": "step_update", "step_update": {"step_index": 4, "state": "DONE",
      "step_type": "error_message", "duration_seconds": 0}}), flush=True)
time.sleep(18)
print(json.dumps({"event": "result", "result": {"response": "pushed after the retries"}}), flush=True)
"""


def test_clocks_stop_during_agy_retries() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-retry-"))
    agy = d / "ccrole"
    agy.write_text(FAKE_AGY_RETRY)
    agy.chmod(0o755)
    pool = ccpool.Pool(
        ccpool.Config(logdir=d / "pool", ccrole=str(agy), render="cat", timeouts={"worker": "60m"},
                      worker_wrap_s=5, worker_wrap_budget_s=90, stall_s=2),
        fetcher=state, spawn=False,
    )
    w = ccpool.Session(pool, ccpool.Unit("worker", "implement", "wrk:6@x", "wrk:6", 6, (4,), {}), 1)
    w.dir.mkdir(parents=True)
    rc, _, timed_out = w.session("do issue 6", dict(os.environ))
    calls = [json.loads(x) for x in (d / "ccrole.calls").read_text().splitlines()]
    check("agy retrying a model call: no wrap-up and no stall resume", (rc, timed_out, len(calls)), (0, False, 1))
    check("agy's own print timeout has room for the pause",
          calls[0][calls[0].index("--print-timeout") + 1], f"{3600 + 900 + 2400}s")
    check("the pause is logged", "clocks paused" in (w.dir / "resumes.log").read_text(), True)


def git_repo(d: pathlib.Path):
    """A bare origin and a clone of it in d; returns a git runner for the clone."""
    origin, work = d / "origin.git", d / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)

    def g(*a):
        return subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True,
                              text=True).stdout.strip()
    g("config", "user.name", "t")
    g("config", "user.email", "t@t")
    return work, g


def test_the_supervisors_checks_leave_no_process_behind() -> None:
    """Run 9: the main check, re-sweeps and auto-rebases left 109 app servers (12.3 GB):
    a test fixture stopped `uv run`, not the server under it, and worker cleanup never
    looked at the supervisor's own worktrees."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-reap-"))
    try:
        work, g = git_repo(d)
        (work / "a").write_text("a\n")
        g("add", "a")
        g("commit", "-qm", "a")
        wt, beside = d / "main-abc", d / "main-abc2"
        g("worktree", "add", "-q", "--detach", str(wt))
        beside.mkdir()
        procs = [subprocess.Popen(["sleep", "60"], cwd=str(c), start_new_session=True) for c in (wt, wt / ".", beside)]
        logged = []
        ccpool.remove_worktree(str(work), str(wt), lambda kind, **kw: logged.append((kind, len(kw["processes"]))))
        check("what runs in the worktree is killed before it is removed, a directory beside it is left alone",
              (procs[0].wait(timeout=10) != 0, procs[1].wait(timeout=10) != 0, procs[2].poll(), wt.exists(), logged),
              (True, True, None, False, [("leftovers_killed", 2)]))
        procs[2].kill()
        h = Rig(state())
        pid_file = d / "pid"
        r = h.pool.run_owned(["bash", "-c", f"sleep 60 >/dev/null 2>&1 & echo $! > {pid_file}; echo started"], 30)
        child = int(pid_file.read_text())
        for _ in range(50):
            if not ccpool.pid_alive(child) or open(f"/proc/{child}/stat").read().split()[2] == "Z":
                break
            time.sleep(0.1)
        check("a check's own process group goes when it ends (the server a fixture left)",
              (r.stdout.strip(), not ccpool.pid_alive(child) or open(f"/proc/{child}/stat").read().split()[2] == "Z"),
              ("started", True))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_scratch_from_earlier_runs_is_pruned() -> None:
    """Run 9: /tmp ran out of inodes, largely on what earlier runs and tests had left. Only
    what is two days stale goes, and a worktree's unsaved work is kept in a rescued diff."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-prune-"))
    old_repo = os.environ.get("SHARED_REPO_PATH")
    try:
        work, g = git_repo(d)
        (work / "a.py").write_text("a = 1\n")
        g("add", "a.py")
        g("commit", "-qm", "a")
        g("push", "-q", "origin", "HEAD:main")
        g("fetch", "-q", "origin")
        os.environ["SHARED_REPO_PATH"] = str(work)
        root = d / "issuelooper-app-0badc0de"
        root.mkdir()
        old = time.time() - 3 * 86400

        def age(p: pathlib.Path) -> None:
            for q in [p] + [pathlib.Path(dp) / n for dp, dirs, files in os.walk(p) for n in dirs + files]:
                os.utime(q, (old, old), follow_symlinks=False)

        def tree(name: str, dirty: bool, stale: bool) -> pathlib.Path:
            wt = root / name
            g("worktree", "add", "-q", "--detach", str(wt), "origin/main")
            if dirty:
                (wt / "a.py").write_text("a = 2  # not pushed\n")
            if stale:
                age(wt)
            return wt

        fresh_dirty = tree("wrk-1-5-wt", dirty=True, stale=False)
        stale_dirty = tree("wrk-2-6-wt", dirty=True, stale=True)
        stale_clean = tree("wrk-3-7-wt", dirty=False, stale=True)
        rescued_before = tree("mrg-9-wt", dirty=True, stale=True)
        files = {"wrk-4-8-wt-rescued.diff": False, "wrk-5-9-wt-rescued.diff": True, "mrg-9-wt-rescued.diff": True,
                 "mrg-7-sweep/a": True, "main-abc/a": False, "sweep/x.json": True, "review/x.json": True,
                 "devcheck/x.json": True, "devcheck/out/old/s.py": True, "devcheck/out/new/s.py": False,
                 "ux/old/a.png": True}
        for rel, stale in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("earlier rescue" if rel.startswith("mrg-9") else "x")
            if stale:
                age(root / rel.split("/")[0] if rel.startswith(("mrg-7", "main-")) else root / rel)
                if rel.startswith(("devcheck/out/old", "ux/old")):
                    age((root / rel).parent)
        stale_root, busy_root = d / "issuelooper-other-deadbeef", d / "issuelooper-busy-cafebabe"
        for r in (stale_root, busy_root):
            (r / "sweep").mkdir(parents=True)
        age(stale_root)
        ccpool.prune_scratch(root)
        left = sorted(p.name for p in root.iterdir())
        check("fresh work stays: a dirty worktree, a rescued diff, a session directory; records stay",
              [n for n in left if n in ("wrk-1-5-wt", "wrk-4-8-wt-rescued.diff", "main-abc", "sweep", "review")],
              ["main-abc", "review", "sweep", "wrk-1-5-wt", "wrk-4-8-wt-rescued.diff"])
        check("the fresh dirty worktree keeps its work", "not pushed" in (fresh_dirty / "a.py").read_text(), True)
        check("stale ones go: a clean worktree, an old rescued diff, a session directory",
              (stale_clean.exists(), (root / "wrk-5-9-wt-rescued.diff").exists(), (root / "mrg-7-sweep").exists()),
              (False, False, False))
        rescued = root / "wrk-2-6-wt-rescued.diff"
        check("a stale dirty worktree goes and leaves its work in a rescued diff",
              (stale_dirty.exists(), rescued.exists() and "not pushed" in rescued.read_text()), (False, True))
        check("one that already had a rescued diff keeps that one as it was",
              (rescued_before.exists(), (root / "mrg-9-wt-rescued.diff").read_text()), (False, "earlier rescue"))
        check("old live-check and UX output goes, new stays",
              ((root / "devcheck/out/old").exists(), (root / "devcheck/out/new").exists(), (root / "ux/old").exists()),
              (False, True, False))
        check("another repository's root untouched for two days goes, a busy one stays",
              (stale_root.exists(), busy_root.exists()), (False, True))
        check("git forgets the removed worktrees", sum(f"{root}/wrk-" in ln for ln in g("worktree", "list").splitlines()), 1)
        custom = d / "scratch"
        (custom / "wrk-1-wt").mkdir(parents=True)
        age(custom / "wrk-1-wt")
        (d / "issuelooper-old-11111111").mkdir()
        age(d / "issuelooper-old-11111111")
        ccpool.prune_scratch(custom)
        check("an ISSUELOOPER_TMP of its own: its stale sessions go, nothing beside it is touched",
              ((custom / "wrk-1-wt").exists(), (d / "issuelooper-old-11111111").exists()), (False, True))
    finally:
        if old_repo is None:
            os.environ.pop("SHARED_REPO_PATH", None)
        else:
            os.environ["SHARED_REPO_PATH"] = old_repo
        shutil.rmtree(d, ignore_errors=True)


def test_session_cleanup_rescues_unpushed_work() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-cleanup-"))
    old_repo = os.environ.get("SHARED_REPO_PATH")
    try:
        work, g = git_repo(d)
        (work / "a.py").write_text("a = 1\n")
        g("add", "a.py")
        g("commit", "-qm", "a")
        g("push", "-q", "origin", "HEAD:main")
        g("fetch", "-q", "origin")
        os.environ["SHARED_REPO_PATH"] = str(work)
        dirty, clean = d / "wrk-4-1-wt", d / "wrk-4-2-wt"
        for wt in (dirty, clean):
            g("worktree", "add", "-q", "--detach", str(wt), "origin/main")
        (dirty / "b.py").write_text("b = 1\n")
        h = Rig(state())
        w = ccpool.Session(h.pool, ccpool.Unit("worker", "implement", "wrk:1@x", "wrk:1", 1, (4,), {}), 4)
        w.cleanup(str(d / "wrk-4-*-wt"))
        rescued = d / "wrk-4-1-wt-rescued.diff"
        logged = [json.loads(x)["path"] for x in (h.pool.cfg.logdir / "pool.jsonl").read_text().splitlines()
                  if json.loads(x)["kind"] == "rescued"]
        check("a session's worktrees go; uncommitted work is kept in a rescued diff, and logged",
              (dirty.exists(), clean.exists(), rescued.exists() and "b = 1" in rescued.read_text(),
               (d / "wrk-4-2-wt-rescued.diff").exists(), logged), (False, False, True, False, [str(dirty)]))
    finally:
        if old_repo is None:
            os.environ.pop("SHARED_REPO_PATH", None)
        else:
            os.environ["SHARED_REPO_PATH"] = old_repo
        shutil.rmtree(d, ignore_errors=True)


def test_worker_prompt_carries_the_issue_and_a_file_map() -> None:
    thread = {"body": "Add `forget-history` to settings (R-SET-7).", "comments": [
        {"a": "bot", "b": "<!-- worker --> <!-- claim loop=3 token=t from=stage:triage --> Claimed by worker loop 3."},
        *({"a": "owner", "b": f"note {i}"} for i in range(8)),
    ]}
    text = ccpool.issue_text(thread)
    check("body and the newest comments, claim notices left out",
          (text.startswith("Add `forget-history`"), "note 7" in text, "note 1" in text, "Claimed" in text,
           "(2 older comments not shown)" in text), (True, True, False, False, True))
    work, g = git_repo(pathlib.Path(tempfile.mkdtemp(prefix="ccpool-map-")))
    for path, body in (("src/settings.js", "a\nconst b = 'forget-history';\n"),
                       ("docs/REQUIREMENTS.md", "R-SET-7 Forget my history\n"),
                       (".claude/hooks/x.py", "forget-history\n")):  # the framework's own files
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(body)
    g("add", "-f", ".")
    g("commit", "-qm", "x")
    check("files that name the issue's backticked names, with lines",
          ccpool.file_map(work, thread["body"], ref="main").splitlines(),
          ["src/settings.js (2 lines): forget-history@2"])
    m = ccpool.file_map(work, thread["body"], ref="main", id_pattern=r"\bR-[A-Z]+-\d+\b")
    check("and the requirement ids the project's pattern matches", m.splitlines(),
          ["docs/REQUIREMENTS.md (1 lines): R-SET-7@1", "src/settings.js (2 lines): forget-history@2"])
    prompt = ccpool.worker_prompt({"issue": 5, "token": "t", "from": "stage:triage", "title": "x"},
                                  "1", "45m", "u", issue=text, files=m)
    check("both in the worker's prompt", ("Issue #5 as of the claim" in prompt, "    src/settings.js" in prompt),
          (True, True))


def test_merged_check_catches_a_semantic_conflict() -> None:
    """Main moved an import away, a PR swept on the older main still used it, and the
    two merged without a textual conflict."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-merged-"))
    work, g = git_repo(d)
    (work / "pkg").mkdir()
    (work / "pkg/__init__.py").write_text("")
    (work / "pkg/main.py").write_text("from typing import Any\n\nX: Any = 1\n\n\n\n\n\ndef run():\n    pass\n")
    g("add", ".")
    g("commit", "-qm", "base")
    g("push", "-q", "origin", "HEAD:main")
    base = g("rev-parse", "HEAD")
    g("checkout", "-qb", "issue-1")
    with open(work / "pkg/main.py", "a") as f:
        f.write("\n\ndef config() -> dict[str, Any]:\n    return {}\n")
    g("commit", "-qam", "the PR uses Any")
    g("push", "-q", "origin", "issue-1")
    pr_head = g("rev-parse", "HEAD")
    g("checkout", "-q", "main")
    (work / "pkg/main.py").write_text("X = 1\n\n\n\n\n\n\n\ndef run():\n    pass\n")
    g("commit", "-qam", "main moves the import away")
    g("push", "-q", "origin", "HEAD:main")
    g("reset", "-q", "--hard", base)  # this checkout has not fetched the new main yet
    calls = d / "gh-calls"
    gh = d / "gh"
    gh.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n")
    gh.chmod(0o755)
    pool = ccpool.Pool(ccpool.Config(logdir=d / "pool", gh=str(gh),
                                     fast_check_cmd=f"{sys.executable} -c 'import pkg.main'"),
                       fetcher=state, spawn=False)
    w = ccpool.Session(pool, ccpool.Unit("auto", "merge", "merge:1@x", "mrg:1", 1, (0, 1), {}), 1)
    w.dir.mkdir(parents=True)
    old = os.environ.get("SHARED_REPO_PATH")
    os.environ["SHARED_REPO_PATH"] = str(work)
    try:
        check("a PR behind main (fetched first) is behind", w.behind_main(pr_head, "issue-1"), True)
        out = w.merged_check(5, 1, pr_head, "issue-1", dict(os.environ))
        check("a head that breaks main once merged is handed back", out, "breaks main once merged (handed back)")
        check("with the error", "is not defined" in calls.read_text(), True)
        main = g("rev-parse", "origin/main")
        check("a head that contains main needs no check", w.merged_check(5, 1, main, "", dict(os.environ)), "")
        # PR #108: a timeout and its retry held the merge lock for ten minutes.
        pool.cfg.fast_check_cmd, pool.cfg.fast_check_s = "if grep -q 'def config' pkg/main.py; then sleep 5; fi", 1
        t0 = time.time()
        first = w.merged_check(5, 1, pr_head, "issue-1", dict(os.environ))
        check("a timeout: not retried, held the first time", (first, time.time() - t0 < 4),
              ("held (the merged-tree check timed out)", True))
        check("a second timeout on the same head: handed back",
              w.merged_check(5, 1, pr_head, "issue-1", dict(os.environ)), "breaks main once merged (handed back)")
        # Run 9: PRs #57 and #59 were handed back when /tmp had no inodes left.
        pool.cfg.fast_check_cmd = "echo 'Caused by: No space left on device (os error 28)'; exit 1"
        pool.cfg.fast_check_s = 30
        check("a check that failed on the machine (ENOSPC): held, not handed back",
              w.merged_check(6, 1, pr_head, "issue-1", dict(os.environ)),
              "held (the merged-tree check failed on the machine: No space left on device)")
        pool.cfg.fast_check_cmd = "exit 1"
        pool.main_checks.clear()
        calls.write_text("")
        check("main itself fails the check: nobody is blamed, the gates decide",
              (w.merged_check(5, 1, pr_head, "issue-1", dict(os.environ)), calls.read_text()), ("", ""))
        g("push", "-q", "-f", "origin", f"{base}:refs/heads/main")  # main back at the base
        check("a head on the current main is not behind", w.behind_main(pr_head, "issue-1"), False)
    finally:
        if old is None:
            os.environ.pop("SHARED_REPO_PATH", None)
        else:
            os.environ["SHARED_REPO_PATH"] = old


FAKE_CLAUDE_WRAP = r"""#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
with open(sys.argv[0] + ".calls", "a") as f:
    f.write(json.dumps(args) + "\n")
if "--resume" in args:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "pushed"}), flush=True)
    sys.exit(0)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}), flush=True)
time.sleep(120)
"""


def test_worker_wrap_up_on_claude() -> None:
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccpool-wrapc-"))
    cli = d / "ccrole"
    cli.write_text(FAKE_CLAUDE_WRAP)
    cli.chmod(0o755)
    pool = ccpool.Pool(
        ccpool.Config(logdir=d / "pool", ccrole=str(cli), render="cat", timeouts={"worker": "60m"},
                      worker_wrap_s=1, worker_wrap_budget_s=90),
        fetcher=state, spawn=False,
    )
    w = ccpool.Session(pool, ccpool.Unit("worker", "implement", "wrk:5@x", "wrk:5", 5, (4,), {}), 1)
    w.dir.mkdir(parents=True)
    rc, _, timed_out = w.session("do issue 5", dict(os.environ, CCROLE_WORKER_CLI="claude"))
    calls = [json.loads(x) for x in (d / "ccrole.calls").read_text().splitlines()]
    check("claude: the role's CLI is the one started", calls[0][:3], ["worker", "--", "-p"])
    second = calls[-1]
    check(
        "claude: wrap-up resumes the session",
        (len(calls), second[second.index("--resume") + 1], "Time is almost up" in second[second.index("-p") + 1]),
        (2, "s1", True),
    )
    check("claude: ends cleanly", (rc, timed_out), (0, False))


# --- end to end: the real loop, fake gh and fake agy ---------------------------

FAKE_GH_GRAPHQL = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["api", "graphql"]:
    import fcntl
    with open(os.environ["FAKE_GH_STATE"] + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        s = json.load(open(os.environ["FAKE_GH_STATE"]))
    lab = lambda i: {"nodes": [{"name": n} for n in i["labels"]]}
    issues = list(s["issues"].values())
    out = {"data": {"repository": {
        "open": {"nodes": [{"number": i["number"], "title": i["title"], "body": i.get("body", ""),
                            "updatedAt": i["updated_at"], "labels": lab(i)}
                           for i in issues if i.get("state", "open") == "open"]},
        "closed": {"nodes": [{"number": i["number"], "title": i["title"], "closedAt": i["updated_at"],
                              "stateReason": "COMPLETED", "labels": lab(i)}
                             for i in issues if i.get("state") == "closed"]},
        "pullRequests": {"nodes": [{"number": p["number"], "headRefName": p["head"]["ref"],
                                    "headRefOid": p["head"].get("sha", "b" * 40), "body": p.get("body", ""),
                                    "isDraft": False, "mergeable": "MERGEABLE"} for p in s["prs"]]},
        "main": {"target": {"oid": s.get("main", "a" * 40)}}}}}
    print(json.dumps(out))
    sys.exit(0)
if args[:2] in (["pr", "merge"], ["pr", "list"], ["pr", "comment"], ["issue", "comment"]):
    import fcntl
    with open(os.environ["FAKE_GH_STATE"] + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        s = json.load(open(os.environ["FAKE_GH_STATE"]))
        with open(os.environ["FAKE_GH_STATE"] + ".calls", "a") as f:
            f.write(json.dumps(args) + "\n")
        if args[:2] == ["pr", "merge"]:
            n = int(args[2])
            p = next(p for p in s["prs"] if p["number"] == n)
            s["prs"] = [q for q in s["prs"] if q["number"] != n]
            s["main"] = "e" * 40
            issue = p["body"].split("#")[1]
            if p["body"].startswith("Fixes"):
                s["issues"][issue]["state"] = "closed"
            json.dump(s, open(os.environ["FAKE_GH_STATE"], "w"))
    sys.exit(0)
os.execv(sys.executable, [sys.executable, os.environ["FAKE_GH_REST"]] + args)
"""

FAKE_CCROLE = r"""#!/usr/bin/env python3
import fcntl, json, os, sys, time
role = sys.argv[1]
prompt = sys.argv[sys.argv.index("--print") + 1]
rec = {"role": role, "CC_ROLE_set_by_ccrole": True, "first": prompt.splitlines()[0],
       "MERGE_PR": os.environ.get("MERGE_PR"), "TST_PORT": os.environ.get("TST_PORT"),
       "WRK_CLAIM_FILE": os.environ.get("WRK_CLAIM_FILE"), "WORKER_POOL": os.environ.get("WORKER_POOL"),
       "claim": open(os.environ["WRK_CLAIM_FILE"]).read() if os.environ.get("WRK_CLAIM_FILE") else None}
with open(os.environ["FAKE_CCROLE_LOG"], "a") as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    f.write(json.dumps(rec) + "\n")
time.sleep(float(os.environ.get("FAKE_CCROLE_SLEEP", "0.3")))
print(json.dumps({"event": "result", "result": {"response": "done"}}))
"""


def test_end_to_end_with_fake_github() -> None:
    repo = claim_test.Repo(
        [
            claim_test.issue(1, ["stage:triage", "P1"]),
            claim_test.issue(2, ["stage:ready", "P0"]),
            claim_test.issue(3, ["stage:pr", "P1"]),
            claim_test.issue(5, ["stage:pr", "P1"]),
            claim_test.issue(4, ["stage:triage", "P2"], body="Blocked by: #1"),
        ],
        prs=[
            {
                "number": 40,
                "head": {"ref": "issue-3", "sha": "c" * 40},
                "body": "Fixes #3",
            },
            {
                "number": 41,
                "head": {"ref": "issue-5", "sha": "d" * 40},
                "body": "Fixes #5",
            },
        ],
    )
    gh = repo.dir / "gh-graphql"
    gh.write_text(FAKE_GH_GRAPHQL)
    gh.chmod(0o755)
    fake = repo.dir / "ccrole"
    fake.write_text(FAKE_CCROLE)
    fake.chmod(0o755)
    log = repo.dir / "sessions.jsonl"
    review = repo.dir / "review"
    review.write_text(
        f'#!/bin/sh\necho "$@" >> {repo.dir}/reviews\n[ "$2" = 41 ] && exit 1\nexit 0\n'
    )
    review.chmod(0o755)
    guard = repo.dir / "guard"
    guard.write_text(
        f"#!/bin/sh\ncat > /dev/null\necho \"$CC_ROLE $MERGE_PR\" >> {repo.dir}/guards\necho '{{\"decision\": \"allow\"}}'\n"
    )
    guard.chmod(0o755)
    env = repo.env(role="")
    env.pop("CC_ROLE", None)
    env.update(
        CLAIM_GH=str(gh),
        FAKE_GH_REST=str(repo.gh),
        POOL_CCROLE=str(fake),
        POOL_RENDER="cat",
        FAKE_CCROLE_LOG=str(log),
        POOL_LOGDIR=str(repo.dir / "pool"),
        POOL_POLL_S="0.5",
        POOL_MIN_FETCH_S="0.2",
        GH_REPO="o/r",
        TST_PORT="8721",
        CLAIM_SETTLE_S="0",
        SHARED_REPO_PATH=str(HERE.parents[1]),
        POOL_REVIEW_CMD=str(review),
        POOL_GUARD_CMD=str(guard),
        POOL_MAIN_CHECK="0",  # it would fetch into this checkout
        POOL_KILL_LEFTOVERS="0",  # nothing to kill: its sessions start no processes
    )
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "ccpool.py"), "run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        labels = {n: repo.labels(n) for n in (1, 2)}
        if all("stage:owner" in v for v in labels.values()):
            break
        time.sleep(0.5)
    proc.terminate()
    out = proc.communicate(timeout=30)[0]
    sessions = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    imps = [s for s in sessions if s["role"] == "worker"]
    mergers = [s for s in sessions if s["role"] == "merger"]
    check(
        "e2e: both claimable issues got workers, the blocked one none",
        sorted({json.loads(s["claim"])["issue"] for s in imps}),
        [1, 2],
    )
    check(
        "e2e: blocked #4 never claimed",
        any(json.loads(s["claim"])["issue"] == 4 for s in imps),
        False,
    )
    check(
        "e2e: every worker session started with a written claim (token, origin stage)",
        all(
            json.loads(s["claim"]).get("token") and json.loads(s["claim"]).get("from")
            for s in imps
        ),
        True,
    )
    check(
        "e2e: workers see an unlimited pool",
        {s["WORKER_POOL"] for s in imps},
        {"unlimited"},
    )
    calls = (
        [
            json.loads(x)
            for x in (repo.dir / "state.json.calls").read_text().splitlines()
        ]
        if (repo.dir / "state.json.calls").exists()
        else []
    )
    check("e2e: no merger session for mergeable PRs", mergers, [])
    reviews = (repo.dir / "reviews").read_text().split("\n")[:-1]
    check(
        "e2e: the review gate ran on both PRs (#40 once)",
        (reviews.count("--pr 40"), reviews.count("--pr 41") >= 1),
        (1, True),
    )
    check(
        "e2e: the merge gates ran as the merger for #40 only (#41 was rejected)",
        (repo.dir / "guards").read_text().split("\n")[:-1],
        ["merger 40"],
    )
    check(
        "e2e: #40 squash-merged at its head",
        [c for c in calls if c[:2] == ["pr", "merge"]],
        [
            [
                "pr",
                "merge",
                "40",
                "--squash",
                "--delete-branch",
                "--match-head-commit",
                "c" * 40,
            ]
        ],
    )
    check(
        "e2e: stage:pr cleared on #3 and a signed merge comment posted",
        (
            "stage:pr" in repo.labels(3),
            any(
                c[:3] == ["issue", "comment", "3"]
                and c[4].startswith("<!-- merger -->")
                for c in calls
            ),
        ),
        (False, True),
    )
    check(
        "e2e: a session without a PR is released, and the third failure parks it at stage:owner",
        (repo.labels(1), repo.labels(2)),
        (["P1", "stage:owner"], ["P0", "stage:owner"]),
    )
    check(
        "e2e: each issue got exactly three worker sessions",
        sorted(json.loads(s["claim"])["issue"] for s in imps),
        [1, 1, 1, 2, 2, 2],
    )
    claims = [c for c in repo.comments(1) if "<!-- claim " in c]
    check(
        "e2e: the claims are v2's signed comments",
        all(c.startswith("<!-- worker -->") for c in claims),
        True,
    )
    if FAILURES:
        print(out[-3000:])


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all ccpool cases pass ({len(tests)} groups, {CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
