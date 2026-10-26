#!/usr/bin/env python3
"""The supervisor of the issue loop: one process per repository.

    ccpool.py run                 supervise until killed (`issuelooper start` starts it)
    ccpool.py plan [--fixture F]  print what would start now; writes nothing
    ccpool.py probe ROLE [--prompt TEXT]
                                  one session with ROLE's environment

One process reads GitHub every POOL_POLL_S seconds (one GraphQL request per 100 open
issues or 60 open pull requests; a failed page fails the whole read) and turns
the state into units of work. A unit starts the moment it appears; when it ends,
the state is read again at once and the next unit starts. Settings come from the
environment, with .issuelooper/config.toml filling what is not exported.

UNITS

  worker  (agent) one per claimable issue (stage:triage/stage:ready, unblocked,
          not held). The supervisor claims it first (`claim.py claim --issue N`:
          signed comment, stage:doing) and finishes it after the session
          (`claim.py finish`). A session that ends without a pull request leaves
          its work on branch issue-<N> (pushed here), and the next worker on the
          issue continues from it. An issue claim.py refuses waits until it or main
          changes, or POOL_UNCLAIMABLE_RETRY_S (240) passes.
  auto    (no agent) merge: one per open PR whose issue is at stage:pr and that
          GitHub calls MERGEABLE. The review gate (review_gate.py) reads the diff,
          role_guard.py's merge gates run with CC_ROLE=merger, and the PR is
          squash-merged, or with POOL_MERGE_TRAIN (the default) queued for the merge
          train (Pool.train: one full sweep of main with up to POOL_TRAIN_SIZE queued
          PRs merged decides them, and is main's check); a refusal hands the PR back
          to a worker (a head the gates
          would refuse for its sweep is swept here first, outside the merge lock; a
          gate 7 refusal after a rebase replays the live check outside the lock; a PR
          held after a red re-sweep is not swept again for the same head and main, and
          one failing only where an earlier red main failed is rebased, not handed back). With POOL_FAST_CHECK_CMD set, a
          PR whose head is behind main is first merged with main in a worktree and
          the command runs there, under the merge lock right before the merge: a
          failure hands the PR back, unless main alone fails it too. It gets POOL_FAST_CHECK_S
          (120); a timeout is not retried: the PR is held, and handed back after a second
          timeout on the same head. rebase: a CONFLICTING PR is first rebased here, once per
          head and main, and not again for a head whose rebase failed; when every
          conflict hunk, in any text file, only adds lines on both sides, both are
          kept, the PR is swept and pushed, and the next poll merges it. The conflicted files are
          counted: a source file that conflicts in POOL_SPLIT_AFTER PRs within
          POOL_SPLIT_WINDOW_S, or grows past POOL_SPLIT_LINES on main, gets an
          issue to split it (at most POOL_SPLIT_PER_WINDOW per window,
          POOL_SPLIT_MAX per run). A P0/P1 PR whose rebase was left to a merger
          POOL_STARVE_AFTER (2) times on the same files is starving: merges of other
          PRs that touch those files are held until it is no longer open or
          POOL_STARVE_HOLD_S (3600) passes; of two starving PRs on one file, the
          higher priority (then the one that conflicted first) waits for nobody. reclaim: a stage:doing claim older than
          CLAIM_STALE_MIN with no PR and no running worker goes back to the queue.
          requeue: an issue at stage:pr with no open PR for POOL_ORPHAN_PR_MIN
          (its PR was closed without a merge) goes back to stage:triage.
  merger  (agent) one per open PR whose issue is at stage:pr and that GitHub calls
          CONFLICTING after the supervisor's rebase could not land it (or UNKNOWN for
          POOL_UNKNOWN_S). It works on only that PR ($MERGE_PR, enforced by
          role_guard): rebase, resolve, re-sweep, review gate; the supervisor's next
          poll merges it, so every merge passes the same checks. When a merger
          ends, GitHub is read again at once and every POOL_MIN_FETCH_S while the
          PR reads UNKNOWN, so the resolved head is merged before main moves again.
          A PR whose POOL_MERGER_MAX (3) merger sessions each ended without a merge
          (main moved on and it conflicted again) goes back to a worker instead
          (handback, no agent).
  tester  (agent) with POOL_LIVE=1, one per stage:live issue each time it enters
          stage:live (the live check against the real services); with POOL_UX=1, a
          UX run at most every POOL_UX_GAP_MIN minutes, on the deployed sha (or on
          main's head when there is no deploy command), never while another is
          running. When no worker or merger has anything to do: another UX run once
          POOL_UX_IDLE_GAP_MIN passed since the last, and with POOL_SPEC=1 a spec check (the app against the spec, one
          requirement at a time) every POOL_SPEC_GAP_MIN; what they file is the
          workers' next work. A spec check also starts while at least half of
          POOL_MAX_WORKERS would stay free after the due units start, once
          POOL_SPEC_GAP_MIN passed; never two at once. Each tester gets its own port.
          A UX run walks the project's journeys script (POOL_UX_JOURNEYS, a path
          in the repository) when there is one, else journeys it keeps in
          $ISSUELOOPER_TMP/ux/; the score on its report's
          first line (`Score: N/100`) is logged and shown on the dashboard.
          The supervisor acts on a live check's verdict itself (settle_live): a
          `live-verified:` line closes the issue, `live-check-blocked:` moves it to
          stage:owner, anything else hands it back to stage:triage, and a second
          failed check moves it to stage:owner.
          With POOL_ACCEPT ([accept] paths set) the live check folds into the
          acceptance check: a PR that changes those paths says `Refs #N`, and the merge
          moves its issue to stage:accept (stage:live from before counts the same).
          Once the build the tester checks (the deployed sha, or main when nothing
          deploys) contains its merge, one tester unit accepts up to POOL_ACCEPT_BATCH
          (6) such issues against the checklist, live mode for needs:live issues and
          live paths. settle_accept: `accept-verified:` closes, `accept-blocked:` goes
          to the owner, a failure is checked once more by a fresh session before it
          counts, then hands back to stage:triage, and a second counted failure goes to
          the owner. With [accept] guard set, the scripts of verified issues become one
          chore issue (tests/accept/test_accept_<N>.py, written by a worker through the
          usual gates), and the supervisor runs that command on each deploy in the main
          lane, with no model: a failing test files `Regression of #N` (P1), once while
          it is open, after a rerun that passes counts it flaky instead.

Triage (an issue with no stage label), priorities, holds and dependencies are the
monitor's; no unit is started for them, and none for an issue with an open blocker
(a `Blocked by:` line or a GitHub issue dependency). Issues labeled ux:proposed are proposals
waiting for the owner: no unit and no triage, but the monitor is told of each new one
to screen it (POOL_UX_SCREEN, on by default). A reopened issue is an open issue
like any other; its newest comments say why it came back. The monitor is also told,
once, of a claimable P0/P1 issue blocked for POOL_BLOCKED_REVIEW_MIN (60), and of an
issue that has failed POOL_DECOMPOSE_AFTER (2; 0 is off) times, to split it: a worker
session that held its claim and ended without a pull request, or a pull request handed
back for red tests, the merged-tree check, a failed live-check replay or a review reject
of a diff it read whole (not gate refusals, merger loops or review cuts).

A unit's key carries the state it was computed from (the issue's updatedAt, the
PR's head and main). A key never runs twice at once, never again after a clean
exit, and at most POOL_KEY_TRIES times after failures; a role+number family runs
one unit at a time and fails at most POOL_FAMILY_MAX times per supervisor lifetime
(sessions that end ok do not count; workers, bounded by claim.py's failed attempts,
and UX runs, one family per sha, are exempt). A unit that ends held (a red main, the
guard or the review gate failing, a dirty shared checkout) spends neither and is due
again after POOL_HOLD_RETRY_S (60). A pull request's family starts afresh when its head
changes and when it is handed back.

SESSIONS

Each role runs on the agent CLI agent_cli.py picks for it (agy or Claude Code,
CCROLE_<ROLE>_CLI > CCROLE_CLI > agy), started through ccrole.sh. A worker still
going at POOL_WORKER_WRAP_S is resumed in the same conversation with a wrap-up
prompt and POOL_WORKER_WRAP_BUDGET_S. A session whose model went silent after a
finished step for POOL_STALL_S (POOL_STALL_FAST_S for a model whose name contains
one of POOL_STALL_FAST_MODELS) is resumed (twice at most), and so is one whose CLI says
the model's turn is over while it waits for a background task (agy), after
POOL_BACKGROUND_IDLE_S (120); still idle after two nudges, it is ended early. An agy session
whose stream stopped behind a step while its conversation went on and finished (agy's
own record of it) is ended POOL_UNSEEN_IDLE_S (180) after the last step, unless a process in
its scratch paths still uses CPU (a sweep it started): then it waits for that. A session that ends on a
model quota error is resumed in the same conversation up to POOL_QUOTA_RESUMES
times, each after a wait that doubles from POOL_QUOTA_WAIT_S up to
POOL_QUOTA_WAIT_MAX_S; one that still ends on it is "quota error": no try spent and
the claim released without a failed attempt. Every quota error also holds new
agent sessions pool-wide for a doubling pause (quota_seen), reset when a session
ends ok. While the CLI itself retries a failed model call inside a turn (agy: the
stream's last event is an error_message step), the session's clocks (timeout,
wrap-up, stall) stand still, up to POOL_RETRY_PAUSE_MAX_S per session. Whatever
paused, a session is killed at POOL_TIMEOUT_CEILING (1.5) times its timeout of wall
clock, and counts as timed out. When a
session ends, processes still running with a working directory in its scratch paths
(a worker's wrk-<seq>-*, a merger's mrg-<pr>-*, a tester's tst-<port>-*, all under
$ISSUELOOPER_TMP) are killed and logged (leftovers_killed); POOL_KILL_LEFTOVERS=0
turns that off.

A worker's prompt quotes its issue (the body and the newest comments) and a map of
the files on origin/main that name the issue's backticked names, and the spec's
requirement ids when POOL_MAP_ID_PATTERN gives their form.

CAPS (0 = unlimited)

  POOL_MAX_WORKERS caps agent sessions of every role together; POOL_CAP_WORKER,
  POOL_CAP_MERGER and POOL_CAP_TESTER cap one role; POOL_CAP_AUTO (default 4)
  caps the no-agent units, which do not count against POOL_MAX_WORKERS. Units
  start in this order: merges, testers, workers by priority.

DEPLOY

With POOL_DEPLOY_CMD set, the supervisor deploys origin/main whenever it moved and
that sha was not deployed or tried yet: it runs the command with `bash -c` in the
shared checkout, with DEPLOY_SHA=<sha>, for at most POOL_DEPLOY_TIMEOUT_S. A run
that fails on git's "cannot lock ref" (another unit fetching into the same .git) is
tried again after 5 s and 10 s. While a merger session is running it waits, so one
deploy carries both merges, but never longer than POOL_DEPLOY_GRACE_S after main
first moved past the last deploy. Empty: no deploys.

MAIN CHECK

With POOL_MAIN_CHECK (on by default when SWEEP_COMMAND is set), each time origin/main
moves the supervisor runs the full check suite on it in a worktree (sweep_locked.py
--lane main: its own lock, not one of the SWEEP_SLOTS), in the background, one run at a time: a
main that moved during a run is not checked, only the newest. It gets POOL_MAIN_CHECK_S
(1200); a failure of the machine itself (INFRA: no space left, and the like) counts as
not run, here and for a PR's checks, which are then held. A check that fails only
tests named in an open `Flaky test:` issue counts as green and comments there; any
other failure is run once more, and a pass then counts as green and reports the tests
as flaky. A PR whose re-sweep or merged-tree check fails only known flaky tests is held,
not handed back. A red main tells the
monitor and files an issue `main is red: <first failing tests>` (type:bug, P0,
stage:triage) with the failing tests, the commits since the last green main and the
end of the output, or comments on the one already open; the next green main closes
it. Until it is green, only a PR whose head contains that red main merges (POOL_RED_HOLD,
on by default): its green sweep shows the merge leaves main no worse, and the fix itself
is such a PR, so the hold ends with it. A test that fails and then passes at the same
commit (the main check's second run, a re-sweep's second try, the merged-tree check's) is
flaky: an issue `Flaky test: <tests>` is filed with FLAKY_MARK (or the open one gets a
comment), and only tests so marked count as known flaky. One still flaky after
POOL_FLAKY_MERGES (10) merges gets one comment (FLAKY_DEADLINE) asking to fix or delete
it, and its issue goes to P1.

MONITOR

The monitor is broker.py's long-lived session: it triages and re-prioritizes. When
the queue changed in a way it acts on (a new, closed or reopened issue, a merge,
stage:owner, a hand-back, a failed or stuck unit, a failed deploy, a stalled or
drained queue) and the monitor is idle, the supervisor sends it one message through
the broker socket with everything since the last one. Not before the monitor's
first turn (the owner's kickoff), and not more often than POOL_MONITOR_MIN_GAP_S.

Logs: POOL_LOGDIR (default $ISSUELOOPER_RUN/pool): pool.jsonl, state.json, one
directory per session, deploy.log and deploys.jsonl. Worktrees and scratch files go
under the repository's own scratch directory, $ISSUELOOPER_TMP (config.scratch_root),
which every session inherits.
"""

from __future__ import annotations

import argparse
import ast
import collections
import collections.abc
import dataclasses
import datetime as dt
import fnmatch
import glob
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_cli  # noqa: E402
import herdr_link  # noqa: E402
import claim as claimlib  # noqa: E402
import config  # noqa: E402

ROLES = ("worker", "merger", "tester")  # agent sessions; "auto" units run here
CLAIMABLE = set(claimlib.CLAIMABLE)
HOLDS = claimlib.HOLDS
IGNORED = claimlib.IGNORED  # ux:proposed: a proposal waiting for the owner
LINK = re.compile(r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?|refs?)\s+#(\d+)\b")
# The tester's verdict on a live check (issuelooper-tester, Live check), and the marker
# the supervisor leaves on a hand-back after one, to count failed checks.
LIVE_VERIFIED = re.compile(r"(?im)^[ \t>*-]*live-verified:[ \t]*\S")
LIVE_BLOCKED = re.compile(r"(?im)^[ \t>*`-]*live-check-blocked:")
LIVE_HANDBACK = "<!-- live-handback -->"
# The acceptance check (POOL_ACCEPT): stage:live, a needs:live issue after its merge from
# before the check covered every product change, is accepted the same way (in live mode).
ACCEPT_STAGE = "stage:accept"
ACCEPTING = (ACCEPT_STAGE, "stage:live")
ACCEPT_VERIFIED = re.compile(r"(?im)^[ \t>*-]*(?:accept|live)-verified:[ \t]*\S")
ACCEPT_BLOCKED = re.compile(r"(?im)^[ \t>*`-]*(?:accept-blocked|live-check-blocked):")
ACCEPT_RETRY = "<!-- accept-retry -->"
ACCEPT_HANDBACK = "<!-- accept-handback -->"
def bare_login(login: str) -> str:
    """A login as both GitHub APIs agree on it: REST names a GitHub App's bot
    `<slug>[bot]` and GraphQL (`gh issue view --json`) plain `<slug>`. Comparing them
    as written, the supervisor never recognized the loop's own tester and asked the
    monitor about every live check."""
    login = login.strip().lower()
    return login[:-5] if login.endswith("[bot]") else login


# A sub-issue the monitor split off a larger one starts with this, so it is not split again.
PART_OF = re.compile(r"<!--\s*part of #(\d+)\s*-->")
BRANCH = re.compile(r"^issue-(\d+)\b")

# Open issues and open pull requests are read page by page ($ia, $pa: the cursors);
# the recently closed issues and main come with the first page only.
QUERY = """
query($owner: String!, $name: String!, $ia: String, $pa: String, $first: Boolean!) {
  repository(owner: $owner, name: $name) {
    open: issues(states: OPEN, first: 100, after: $ia) {
      pageInfo { hasNextPage endCursor }
      nodes { number title body updatedAt labels(first: 30) { nodes { name } } comments { totalCount }
              issueDependenciesSummary { blockedBy } blockedBy(first: 20) { nodes { number state } } }
    }
    closed: issues(states: CLOSED, first: 60, orderBy: {field: UPDATED_AT, direction: DESC}) @include(if: $first) {
      nodes { number title closedAt stateReason labels(first: 30) { nodes { name } } }
    }
    pullRequests(states: OPEN, first: 60, after: $pa) {
      pageInfo { hasNextPage endCursor }
      nodes { number headRefName headRefOid body isDraft mergeable }
    }
    main: ref(qualifiedName: "refs/heads/main") @include(if: $first) { target { oid } }
  }
}
"""
MAX_PAGES = 50  # 5000 open issues; more is an error, not a quietly partial queue


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def seconds(t: str) -> int:
    m = re.fullmatch(r"(\d+)([smh]?)", t.strip())
    if not m:
        return 1800
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


def run_dir() -> pathlib.Path:
    """$ISSUELOOPER_RUN, else <repo root>/.issuelooper/run (the installer gitignores it)."""
    v = os.environ.get("ISSUELOOPER_RUN", "").strip()
    return pathlib.Path(v) if v else repo_root() / ".issuelooper" / "run"


def gap_s(name: str, default_min: float) -> float:
    """<name>_S seconds when set, else <name>_MIN minutes (default_min when unset)."""
    if os.environ.get(f"{name}_S", "").strip():
        return float(os.environ[f"{name}_S"])
    return float(os.environ.get(f"{name}_MIN", "") or default_min) * 60


def scratch(name: str) -> str:
    """A path under this repository's scratch directory ($ISSUELOOPER_TMP)."""
    return str(config.scratch_root() / name)


def ux_gap_s() -> float:
    """POOL_UX_GAP_S when set, else POOL_UX_GAP_MIN minutes (default 25)."""
    return gap_s("POOL_UX_GAP", 25)


DEPLOY_LOCK_PAUSE_S = 5  # deploy() retries a ref-lock failure after 5 s, then 10 s


def quota_backoff(k: int, base: float, cap: float) -> float:
    """The wait before the k-th (1-based) resume after a quota error: base, 2x, 4x ..., capped."""
    return min(cap, base * 2 ** max(0, k - 1))


@dataclasses.dataclass
class Config:
    poll_s: float = 15
    min_fetch_s: float = 3
    caps: dict = dataclasses.field(default_factory=dict)
    global_cap: int = 0
    timeouts: dict = dataclasses.field(default_factory=dict)
    logdir: pathlib.Path = dataclasses.field(default_factory=lambda: run_dir() / "pool")
    deploy_cmd: str = ""
    deploy_timeout_s: float = 420
    live: bool = False
    required_files: list = dataclasses.field(default_factory=list)  # in the shared checkout; see check_files
    devcheck: bool = False
    ux: bool = False
    broker_sock: str = ""
    herdr_monitor: str = ""
    herdr_quiet_s: float = 3
    feed_file: str = ""
    monitor_min_gap_s: float = 60
    deploy_grace_s: float = 300
    key_tries: int = 2
    family_max: int = 6
    consolidate_glob: str = "tests/e2e/test_issue_*.py"  # per-issue test files counted for the chore
    consolidate_at: int = 40  # that many on main: one chore to merge or parametrize them by flow (0 = off)
    flaky_merges: int = 10  # merges a recorded flaky test has to be fixed or deleted in (0 = no deadline)
    hold_retry_s: float = 60  # a unit that ended held is due again after this, without spending a try
    crash_max: int = 3
    crash_pause_s: float = 120
    quota_resumes: int = 8
    quota_wait_s: float = 60
    quota_wait_max_s: float = 900
    stall_s: float = 600
    hold_review_s: float = 3600  # a hold:monitor this old is pointed out to the monitor
    blocked_review_s: float = 3600  # a claimable P0/P1 issue blocked this long is pointed out too
    decompose_after: int = 2  # failed tries on one issue (Book.failed_tries) before the monitor is asked to split it
    ux_screen: bool = True  # the monitor screens ux:proposed proposals before the owner reads them
    background_idle_s: float = 120  # agy idle on a background task this long: resumed (Session.background_idle)
    unseen_idle_s: float = 180  # agy's conversation over this long behind a stuck stream: ended (Session.finished_unseen)
    stall_fast_s: float = 240
    stall_fast_models: str = "claude"
    kill_leftovers: bool = True  # kill what a session left running in its scratch paths
    # The most a session's clocks stand still for the CLI's own retries of a model call;
    # 20 min was not enough for a model whose calls kept failing for a while.
    retry_pause_max_s: float = 2400
    # Wall clock a session may run, as a multiple of its timeout, whatever paused its
    # clocks: a merger with 25 minutes of paused clocks ran 52 minutes on a 45-minute
    # timeout, a UX run 69 on 60. 0 = no ceiling.
    timeout_ceiling: float = 1.5
    fast_check_cmd: str = ""  # run on a behind PR merged with main; empty = no check
    rerun_cmd: str = ""  # [sweep] rerun: reruns the named failing tests ({tests}); empty = the whole check again
    main_check: bool = False  # run the full sweep on each new origin/main (Pool.main_check_due)
    main_check_s: float = 1200  # the main check's time limit (run 9: 58-375 s, one hung for its 3600 s)
    fast_check_s: float = 120
    map_id_pattern: str = ""  # a regex for the spec's requirement ids (the worker's file map)
    stale_min: int = 60
    orphan_pr_min: int = 10
    unclaimable_retry_s: float = 240
    merger_max: int = 3  # merger sessions on one PR without a merge before it goes back to a worker
    auto_cap: int = 4
    ux_gap_s: float = 1500
    ux_idle_gap_s: float = 600
    ux_journeys: str = ""  # the project's journeys script (a repository path); empty = the tester keeps its own
    spec: bool = False
    spec_gap_s: float = 2700
    unknown_s: float = 300
    split_after: int = 4
    starve_after: int = 2  # conflicts left to a merger on the same files before a P0/P1 PR is starving
    starve_hold_s: float = 3600  # how long merges that touch a starving PR's files wait for it at most
    split_window_s: float = 5400
    split_max: int = 8
    split_per_window: int = 1
    split_lines: int = 900
    worker_wrap_s: float = 2100
    worker_wrap_budget_s: float = 720
    tst_port: int = 8021
    tst_port_step: int = 100
    ccrole: str = str(HERE / "ccrole.sh")
    render: str = f"python3 {HERE / 'ccloop_render.py'}"
    gh: str = "gh"
    red_hold: bool = True  # while main's full check is red, only a PR built on that main merges
    # The merge train (Pool.train): a PR that passes the gates waits in a queue, and one
    # full sweep of main with a batch of them merged decides the batch; that sweep is also
    # main's check. Off: each PR merges on its own after the merged-tree check.
    merge_train: bool = False
    train_size: int = 4  # PRs in one train at most
    # The acceptance check: issues whose PR changed accept_paths (and needs:live issues)
    # wait at stage:accept after the merge for a tester's check (settle_accept).
    accept: bool = False
    accept_paths: list = dataclasses.field(default_factory=list)
    accept_batch: int = 6  # issues one acceptance unit checks at most
    accept_guard: str = ""  # [accept] guard: tests/accept run on each deploy (Pool.guard_due); empty = off
    gh_rate_wait_s: float = 30  # first wait after GitHub's secondary rate limit refused a gh call; doubles, 3 retries
    repo: str = ""
    review_cmd: str = f"{sys.executable} {HERE / 'review_gate.py'}"
    guard_cmd: str = f"{sys.executable} {HERE / 'role_guard.py'}"

    def wrap_s(self) -> float:
        """When a worker gets its wrap-up turn: POOL_WORKER_WRAP_S, unless the worker
        timeout comes first; then at three quarters of the timeout, so the work can
        still be saved (a 20-minute timeout wraps up at 15)."""
        if not self.worker_wrap_s:
            return 0.0
        timeout = seconds(self.timeouts["worker"]) if self.timeouts.get("worker") else 0
        if timeout and self.worker_wrap_s >= timeout:
            return 0.75 * timeout
        return self.worker_wrap_s

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        # A worker that also runs the app for real before its PR (devcheck.py) needs
        # longer: the check alone takes several minutes.
        devcheck = env_flag("POOL_DEVCHECK")
        return cls(
            poll_s=float(e("POOL_POLL_S", "15")),
            min_fetch_s=float(e("POOL_MIN_FETCH_S", "3")),
            caps={r: env_int(f"POOL_CAP_{r.upper()}", 0) for r in ROLES},
            global_cap=env_int("POOL_MAX_WORKERS", 0),
            timeouts={
                "worker": e("POOL_TIMEOUT_WORKER", "60m" if devcheck else "45m"),
                "merger": e("POOL_TIMEOUT_MERGER", "45m"),
                # 60 min: a UX run walks every journey and reads every screenshot;
                # testers kept timing out at 45.
                "tester": e("POOL_TIMEOUT_TESTER", "60m"),
            },
            logdir=pathlib.Path(e("POOL_LOGDIR", "") or run_dir() / "pool"),
            deploy_cmd=e("POOL_DEPLOY_CMD", "").strip(),
            deploy_timeout_s=float(e("POOL_DEPLOY_TIMEOUT_S", "") or 420),
            live=env_flag("POOL_LIVE"),
            required_files=[f.strip() for f in (e("POOL_REQUIRED_FILES", "") or (".env" if env_flag("POOL_LIVE") else ""))
                            .split(",") if f.strip()],
            devcheck=devcheck,
            ux=env_flag("POOL_UX"),
            spec=env_flag("POOL_SPEC"),
            broker_sock=e("POOL_BROKER_SOCK", ""),
            herdr_monitor=e("POOL_HERDR_MONITOR", "").strip(),
            herdr_quiet_s=float(e("POOL_HERDR_QUIET_S", "") or 3),
            feed_file=e("POOL_MONITOR_FEED", ""),
            monitor_min_gap_s=float(e("POOL_MONITOR_MIN_GAP_S", "60")),
            deploy_grace_s=float(e("POOL_DEPLOY_GRACE_S", "300")),
            key_tries=env_int("POOL_KEY_TRIES", 2),
            family_max=env_int("POOL_FAMILY_MAX", 6),
            hold_retry_s=float(e("POOL_HOLD_RETRY_S", "") or 60),
            flaky_merges=env_int("POOL_FLAKY_MERGES", 10),
            consolidate_glob=e("POOL_CONSOLIDATE_GLOB", "") or "tests/e2e/test_issue_*.py",
            consolidate_at=env_int("POOL_CONSOLIDATE_AT", 40),
            crash_max=env_int("POOL_CRASH_MAX", 3),
            crash_pause_s=float(e("POOL_CRASH_PAUSE_S", "120")),
            quota_resumes=env_int("POOL_QUOTA_RESUMES", 8),
            quota_wait_s=float(e("POOL_QUOTA_WAIT_S", "60")),
            quota_wait_max_s=float(e("POOL_QUOTA_WAIT_MAX_S", "900")),
            stall_s=float(e("POOL_STALL_S", "600")),
            hold_review_s=gap_s("POOL_HOLD_REVIEW", 60),
            blocked_review_s=gap_s("POOL_BLOCKED_REVIEW", 60),
            decompose_after=env_int("POOL_DECOMPOSE_AFTER", 2),
            ux_screen=env_flag("POOL_UX_SCREEN", True),
            background_idle_s=float(e("POOL_BACKGROUND_IDLE_S", "") or 120),
            unseen_idle_s=float(e("POOL_UNSEEN_IDLE_S", "") or 180),
            stall_fast_s=float(e("POOL_STALL_FAST_S", "") or 240),
            stall_fast_models=e("POOL_STALL_FAST_MODELS", "claude"),
            kill_leftovers=env_flag("POOL_KILL_LEFTOVERS", True),
            retry_pause_max_s=float(e("POOL_RETRY_PAUSE_MAX_S", "") or 2400),
            timeout_ceiling=float(e("POOL_TIMEOUT_CEILING", "") or 1.5),
            fast_check_cmd=e("POOL_FAST_CHECK_CMD", "").strip(),
            rerun_cmd=e("SWEEP_RERUN", "").strip(),
            main_check=env_flag("POOL_MAIN_CHECK", True) and bool(e("SWEEP_COMMAND", "").strip()),
            main_check_s=float(e("POOL_MAIN_CHECK_S", "") or 1200),
            fast_check_s=float(e("POOL_FAST_CHECK_S", "") or 120),
            map_id_pattern=e("POOL_MAP_ID_PATTERN", "").strip(),
            stale_min=env_int("CLAIM_STALE_MIN", 60),
            orphan_pr_min=env_int("POOL_ORPHAN_PR_MIN", 10),
            unclaimable_retry_s=float(e("POOL_UNCLAIMABLE_RETRY_S", "") or 240),
            merger_max=env_int("POOL_MERGER_MAX", 3),
            auto_cap=env_int("POOL_CAP_AUTO", 4),
            ux_gap_s=ux_gap_s(),
            ux_idle_gap_s=gap_s("POOL_UX_IDLE_GAP", 10),
            ux_journeys=e("POOL_UX_JOURNEYS", "").strip(),
            spec_gap_s=gap_s("POOL_SPEC_GAP", 45),
            unknown_s=float(e("POOL_UNKNOWN_S", "300")),
            split_after=env_int("POOL_SPLIT_AFTER", 4),
            starve_after=env_int("POOL_STARVE_AFTER", 2),
            starve_hold_s=float(e("POOL_STARVE_HOLD_S", "") or 3600),
            split_window_s=float(e("POOL_SPLIT_WINDOW_S", "5400")),
            split_max=env_int("POOL_SPLIT_MAX", 8),
            split_per_window=env_int("POOL_SPLIT_PER_WINDOW", 1),
            split_lines=env_int("POOL_SPLIT_LINES", 900),
            worker_wrap_s=float(e("POOL_WORKER_WRAP_S", "3000" if devcheck else "2100")),
            # 12 min: workers on slower models ran out of 8 before they had pushed.
            worker_wrap_budget_s=float(e("POOL_WORKER_WRAP_BUDGET_S", "720")),
            tst_port=env_int("TST_PORT", config.tester_port_base()),
            tst_port_step=env_int("POOL_TESTER_PORT_STEP", 100),
            ccrole=e("POOL_CCROLE", str(HERE / "ccrole.sh")),
            render=e("POOL_RENDER", f"python3 {HERE / 'ccloop_render.py'}"),
            gh=e("POOL_GH", e("CLAIM_GH", "gh")),
            gh_rate_wait_s=float(e("POOL_GH_RATE_WAIT_S", "") or 30),
            red_hold=env_flag("POOL_RED_HOLD", True),
            merge_train=env_flag("POOL_MERGE_TRAIN", True) and bool(e("SWEEP_COMMAND", "").strip()),
            train_size=env_int("POOL_TRAIN_SIZE", 4),
            accept=env_flag("POOL_ACCEPT", True) and bool(config.accept_paths()),
            accept_paths=config.accept_paths(),
            accept_batch=env_int("POOL_ACCEPT_BATCH", 6),
            accept_guard=e("ACCEPT_GUARD", "").strip(),
            repo=e("GH_REPO", ""),
            review_cmd=e("POOL_REVIEW_CMD", f"{sys.executable} {HERE / 'review_gate.py'}"),
            guard_cmd=e("POOL_GUARD_CMD", f"{sys.executable} {HERE / 'role_guard.py'}"),
        )


# --- GitHub state -------------------------------------------------------------


def names(node: dict) -> list[str]:
    return sorted(lab["name"] for lab in (node.get("labels") or {}).get("nodes") or [])


def normalize(data: dict) -> dict:
    """The GraphQL answer as plain dicts; tests build this shape directly."""
    r = data["data"]["repository"]
    return {
        "open": [
            {
                "number": n["number"],
                "title": n.get("title", ""),
                "body": n.get("body") or "",
                "updated": n.get("updatedAt", ""),
                "labels": names(n),
                "comments": (n.get("comments") or {}).get("totalCount", 0),
                # open issues blocking this one through GitHub's own dependencies: how many,
                # and which (the first 20; their labels are in "open", so a blocker at
                # stage:accept can count as done). None when the answer had no list.
                "blocked_by": (n.get("issueDependenciesSummary") or {}).get("blockedBy", 0),
                "blockers": [b["number"] for b in (n.get("blockedBy") or {}).get("nodes") or []
                             if b.get("state") == "OPEN"] if "blockedBy" in n else None,
            }
            for n in r["open"]["nodes"]
        ],
        "closed": [
            {
                "number": n["number"],
                "title": n.get("title", ""),
                "closed": n.get("closedAt", ""),
                "reason": n.get("stateReason") or "COMPLETED",
                "labels": names(n),
            }
            for n in r["closed"]["nodes"]
        ],
        "prs": [
            {
                "number": p["number"],
                "branch": p.get("headRefName", ""),
                "head": p.get("headRefOid", ""),
                "body": p.get("body") or "",
                "draft": bool(p.get("isDraft")),
                "mergeable": p.get("mergeable", "UNKNOWN"),
            }
            for p in r["pullRequests"]["nodes"]
        ],
        "main": ((r.get("main") or {}).get("target") or {}).get("oid", ""),
    }


STAGES = ("stage:triage", "stage:ready", "stage:doing", "stage:pr", "stage:accept", "stage:live", "stage:owner")


def queue_snapshot(st: dict) -> dict:
    """The queue as counts per stage, for state.json and the dashboard (which never asks
    GitHub itself): open issues by stage, holds, the owner's open decisions, and the
    open pull requests by what GitHub says about merging them."""
    counts = collections.Counter()
    owner = []
    for i in st["open"]:
        labels = set(i["labels"])
        stage = next((x for x in STAGES if x in labels), "")
        if "ux:proposed" in labels:
            counts["ux:proposed"] += 1
        elif stage:
            counts[stage] += 1
        else:
            counts["untriaged"] += 1
        for hold in ("on hold", "hold:monitor"):
            if hold in labels:
                counts[hold] += 1
        if stage == "stage:owner":
            owner.append({"number": i["number"], "title": i["title"], "updated": i["updated"]})
    prs = collections.Counter(p["mergeable"] for p in st["prs"])
    return {"open": len(st["open"]), "stages": dict(counts), "owner": owner, "prs": dict(prs)}


def fetch(cfg: Config) -> dict:
    repo = (
        cfg.repo
        or claimlib.gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()
    )
    owner, name = repo.split("/", 1)
    # Every page, or none: a queue planned from the first 100 issues missed work, merges
    # and blockers, and a failed page raises, so the poll counts as failed.
    first, cursors, merged = True, {"open": None, "pullRequests": None}, None
    for _ in range(MAX_PAGES):
        args = [cfg.gh, "api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"name={name}",
                "-F", f"first={'true' if first else 'false'}"]
        for var, key in (("ia", "open"), ("pa", "pullRequests")):
            args += ["-f", f"{var}={cursors[key]}"] if cursors[key] else ["-F", f"{var}=null"]
        out = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(f"graphql: {out.stderr.strip()[-300:]}")
        page = json.loads(out.stdout)
        repo_data = page["data"]["repository"]
        if merged is None:
            merged = page
        more = False
        for key in ("open", "pullRequests"):
            conn = repo_data[key]
            if merged is not page:
                merged["data"]["repository"][key]["nodes"] += conn["nodes"]
            info = conn.get("pageInfo") or {}
            # A finished connection is asked again after its last cursor, which reads as
            # empty; asked from the start it would come back twice.
            cursors[key] = info.get("endCursor") or cursors[key]
            more = more or bool(info.get("hasNextPage"))
        if not more:
            return normalize(merged)
        first = False
    raise RuntimeError(f"graphql: more than {MAX_PAGES} pages of open issues or pull requests")


def signature(st: dict) -> str:
    """ccwake_sig.py's idea, for the whole queue: a hash of what the units read."""
    items = [f"main@{st['main']}"]
    items += [
        f"o{i['number']}@{i['updated']}:{','.join(i['labels'])}:{i.get('blocked_by', 0)}:{i.get('blockers')}"
        for i in st["open"]
    ]
    items += [f"c{i['number']}:{','.join(i['labels'])}" for i in st["closed"]]
    items += [f"p{p['number']}@{p['head']}:{p['mergeable']}" for p in st["prs"]]
    return hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()[:16]


def pr_issue(pr: dict) -> int | None:
    m = BRANCH.match(pr.get("branch") or "")
    if m:
        return int(m.group(1))
    m = LINK.search(pr.get("body") or "")
    return int(m.group(1)) if m else None


FAILED_LINE = re.compile(r"(?m)^(?:FAILED|ERROR) (\S+)")
# Gate 7's refusal of a head whose change key moved since its live check (role_guard).
GATE7_MOVED = re.compile(r"Gate 7 \(live check\): the live check ran at `([0-9a-fA-F]{7,40})`, and the head")
# Gate 2's reading of a PR body (role_guard.SWEEP_SHA_PATTERN and SWEEP_WAIVER).
SWEEP_SHA = re.compile(r"(?i)\bSweep\s+SHA\b[*`:\s]*([0-9a-f]{7,40})")
SWEEP_WAIVED = re.compile(r"(?i)\bSweep\b[*`:\s]*waived\s*[-–—:]\s*(loop/process|docs\s+only|owner\s+ruling)\b")


def failing_tests(out: str) -> list[str]:
    """The tests a red sweep names (pytest's `FAILED <test>` and `ERROR <test>` lines),
    else the reasons sweep_run.py printed under `== sweep is red ==`."""
    found: list[str] = []
    for t in FAILED_LINE.findall(out):
        if t not in found:
            found.append(t)
    if found:
        return found
    tail = out.split("== sweep is red ==", 1)
    if len(tail) < 2:
        return []
    return [ln.strip() for ln in tail[1].splitlines() if ln.strip() and not ln.startswith("sweep ")][:5]


FAST_TIMEOUT = "the fast check timed out after"
# In a `Flaky test:` issue's body or comment: these tests failed and passed at one commit.
FLAKY_MARK = "<!-- flaky: failed and passed at one commit -->"
# On the comment that says a recorded flaky test is past its deadline (once per test).
FLAKY_DEADLINE = "<!-- flaky: past its deadline -->"
# The machine, not the code: a check that failed this way says nothing about the PR. In
# run 9 /tmp ran out of inodes and PRs #57 and #59 were handed back for it.
INFRA = re.compile(r"(?i)no space left on device|os error 28|\bENOSPC\b|disk quota exceeded|\bEDQUOT\b|"
                   r"too many open files|\bEMFILE\b|cannot allocate memory|\bENOMEM\b|read-only file system")


def same_failure(first: str, second: str) -> bool:
    """Whether two red runs failed the same way: a test in common, or (when either names
    none) as far as anyone can tell."""
    a, b = set(failing_tests(first)), set(failing_tests(second))
    return bool(a & b) if a and b else True


def accepted(st: dict) -> set[int]:
    """Open issues whose change is merged and on main, waiting only for their acceptance
    check (stage:accept, or stage:live): done, for the issues they block. In cohort 55
    three merged hub issues waited over an hour at stage:accept with 19 in that queue,
    and the issues they blocked waited with them until the monitor cut the dependencies."""
    return {i["number"] for i in st["open"] if set(i["labels"]) & set(ACCEPTING)}


def native_blockers(issue: dict, done: set[int]) -> list:
    """The issue's open GitHub-dependency blockers that are not done: their numbers, and
    "?" for each one the answer did not list (none read, or beyond the first 20)."""
    count, known = issue.get("blocked_by") or 0, issue.get("blockers")
    if known is None:
        return ["?"] * count
    return [n for n in known if n not in done] + ["?"] * max(0, count - len(known))


def line_blockers(issue: dict, open_numbers: set[int], done: set[int]) -> list[int]:
    """The open issues a `Blocked by:` line names that are not done."""
    return [int(x) for m in claimlib.BLOCKED_LINE.finditer(issue.get("body") or "")
            for x in claimlib.NUM.findall(m.group(1)) if int(x) in open_numbers and int(x) not in done]


def blocked(issue: dict, open_numbers: set[int], done: set[int] = frozenset()) -> bool:
    """An open blocker, a GitHub dependency or a `Blocked by:` line, that is not done
    (accepted). claim.py refuses both too, but leaving the native ones to it started a
    unit per issue per poll that it only refused."""
    return bool(native_blockers(issue, done) or line_blockers(issue, open_numbers, done))


def priority(labels) -> int:
    return min((claimlib.PRIO[n] for n in labels if n in claimlib.PRIO), default=9)


def age_s(stamp: str, now: float) -> float:
    if not stamp:
        return 0.0
    return now - dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


# --- planning (pure; ccpool_test.py drives it) ----------------------------------


@dataclasses.dataclass(frozen=True)
class Unit:
    role: str
    kind: str
    key: str  # never runs twice at once, never again after a clean exit
    family: str  # one session at a time per role+number
    number: int | None
    rank: tuple
    info: dict = dataclasses.field(default_factory=dict, compare=False, hash=False)


class Book:
    """What the supervisor has started and how it ended."""

    def __init__(self):
        self.running: dict[str, Unit] = {}  # family -> unit
        self.tries: collections.Counter = collections.Counter()  # key -> starts
        self.family_starts: collections.Counter = collections.Counter()
        self.clean: set[str] = set()  # keys whose session ended cleanly
        self.unclaimable: dict[int, tuple] = {}  # issue -> (updated, main, when) claim.py refused
        self.held_until: dict[str, float] = {}  # key -> when a unit that ended held is due again
        self.hard_conflicts: dict[int, list] = {}  # PR -> [(time, files)] of rebases left to a merger
        self.swept_held: dict[int, tuple[str, str]] = {}  # PR -> (unit key, hold) after a red re-sweep
        self.rebase_wanted: dict[int, str] = {}  # PR -> head the supervisor rebases onto main (auto rebase)
        self.rebase_failed: dict[int, str] = {}  # PR -> head whose auto-rebase failed
        self.train: dict[int, dict] = {}  # PR -> its place in the merge train's queue (Pool.enqueue)
        # Acceptance (Pool.track_accept): issue -> {"pr", "sha" (its merge), "live", "t"};
        # issue -> the build its merge was found in; issue -> checks that settled nothing
        # (part of the unit's key, so the next batch checks it again); the issues the next
        # unit may take, and that build.
        self.accept_merged: dict[int, dict] = {}
        self.accept_in: dict[int, str] = {}
        self.accept_rounds: collections.Counter = collections.Counter()
        self.accept_ready: list[int] = []
        self.accept_target = ""
        self.merger_runs: collections.Counter = collections.Counter()  # PR -> merger sessions since its last merge or hand-back
        self.pr_heads: dict[int, str] = {}  # PR -> the head the last plan read
        # PR -> [head handed back, seen off stage:pr since]: a poll read before the hand-back
        # still shows stage:pr, and PR #131 was merged again 2 s later on the same head.
        self.handed_back: dict[int, list] = {}
        self.paused: dict[str, float] = {}  # role -> until (crash breaker)
        self.quota_until = 0.0  # no new agent sessions before this (model quota errors)
        self.quota_streak = 0  # pauses in a row without a session ending ok
        self.live_entry: dict[int, str] = {}  # issue -> updatedAt when it entered stage:live
        self.missing: list[str] = []  # required files gone from the shared checkout (Pool.check_files)
        self.holds: dict[int, float] = {}  # issue -> when hold:monitor was first seen on it
        self.holds_told: set[int] = set()  # holds the monitor was reminded of
        self.blocked: dict[int, float] = {}  # claimable P0/P1 issue -> when it was first seen blocked
        self.blocked_told: set[int] = set()  # blocked issues the monitor was pointed to
        # issue -> its own failures: worker sessions that held its claim and ended without a
        # pull request, and pull requests handed back for red tests or the review's reject.
        self.failed_tries: collections.Counter = collections.Counter()
        self.decompose_told: set[int] = set()  # issues the monitor was asked to split
        self.last_ux_t = 0.0  # when the last UX run started
        self.last_spec_t = 0.0  # when the last spec check started
        self.extra_runs = 0  # idle UX runs and spec checks started (part of their keys)
        self.unknown_since: dict[int, float] = {}  # PR -> first poll it read UNKNOWN
        self.conflicts: list[tuple[float, str, int]] = []  # (time, file, PR) seen by a rebase
        self.split_filed: set[str] = set()  # files a split issue was filed for
        self.split_times: list[float] = []  # when each split issue was filed
        self.large: dict[str, int] = {}  # source file on main -> lines, when over the limit

    def role_counts(self) -> collections.Counter:
        return collections.Counter(u.role for u in self.running.values())

    def reset_pr(self, n: int) -> None:
        """Start PR n's family afresh: its failed starts and its keys' tries. A PR handed
        back after three merger sessions kept the failures of its rebases, merges and
        hand-back, so the head the worker then pushed was neither rebased nor merged
        ("family") until the monitor closed the PR. merger_runs is left alone here: each
        merger pushes a new head, and the merger loop is counted across those heads."""
        self.family_starts.pop(f"mrg:{n}", None)
        for k in [k for k in self.tries if re.match(rf"(?:merge|rebase|mrg|handback):{n}@", k)]:
            del self.tries[k]

    def why_not(self, u: Unit, cfg: Config, now: float | None = None) -> str:
        if u.family in self.running:
            return "running"
        if u.key in self.clean:
            return "done"
        if now is not None and self.held_until.get(u.key, 0) > now:
            return "held"
        if self.tries[u.key] >= cfg.key_tries:
            return "tries"
        # The family cap is a backstop against a unit that keeps failing under a new key.
        # Workers are exempt: claim.py already parks an issue at stage:owner after three
        # failed attempts, which the owner can reset, while a family cap cannot be reset
        # (an issue whose merged PRs each came back for the next fix hit it and could
        # not be restarted). UX runs are one family per sha.
        if (self.family_starts[u.family] >= cfg.family_max and u.kind != "ux"
                and u.role != "worker"):
            return "family"
        return ""


def plan(
    st: dict, book: Book, cfg: Config, now: float, ux_sha: str = ""
) -> tuple[list[Unit], list[Unit]]:
    """(units to start, in admission order; units that are due but exhausted).
    ux_sha is the commit a UX run would test (Pool.ux_target)."""
    units: list[Unit] = []
    open_numbers = {i["number"] for i in st["open"]}
    done = accepted(st)
    pr_of: dict[int, dict] = {}
    for pr in st["prs"]:
        n = pr_issue(pr)
        if n is not None:
            pr_of.setdefault(n, pr)
    main = st["main"]

    for i in st["open"]:
        n, labels = i["number"], set(i["labels"])
        if labels & IGNORED:
            continue  # a UX candidate waits for the owner's decision
        if not any(x.startswith("stage:") for x in labels):
            continue  # untriaged: the monitor's (it is told in the feed)
        if labels & HOLDS:
            continue
        if labels & CLAIMABLE:
            memo = book.unclaimable.get(n)
            if blocked(i, open_numbers, done) or (
                memo and memo[:2] == (i["updated"], main) and now - memo[2] < cfg.unclaimable_retry_s
            ):
                continue
            units.append(
                Unit(
                    "worker",
                    "implement",
                    f"wrk:{n}@{i['updated']}",
                    f"wrk:{n}",
                    n,
                    (4, priority(labels), n),
                    {"title": i["title"]},
                )
            )
        elif claimlib.DOING in labels:
            if (
                n not in pr_of
                and f"wrk:{n}" not in book.running
                and age_s(i["updated"], now) > cfg.stale_min * 60
            ):
                units.append(
                    Unit("auto", "reclaim", f"reclaim:{n}@{i['updated']}", f"rcl:{n}", n, (1, n), {})
                )
        elif (
            "stage:pr" in labels
            and n not in pr_of
            and f"wrk:{n}" not in book.running
            and age_s(i["updated"], now) > cfg.orphan_pr_min * 60
        ):
            # Its pull request was closed without a merge (GitHub closes one whose head
            # became its base after a force-push): nothing else would ever move it, and
            # issues blocked on it waited too.
            units.append(
                Unit("auto", "requeue", f"requeue:{n}@{i['updated']}", f"rcl:{n}", n, (1, n), {})
            )
        elif "stage:live" in labels and cfg.live and not cfg.accept and not book.missing:
            # (While a file the live environment needs is missing, a tester could only
            # report that it cannot run the check; the monitor hears it once instead.)
            # Keyed on this entry into stage:live (Pool.track_live), so a hand-back,
            # a reopen or a later merge for it gets a new check. Until a check of
            # this entry ends cleanly the key carries main too: one that never
            # finished is tried again after the next merge.
            since = book.live_entry.get(n, "")
            base = f"live:{n}@{since}"
            key = base if base in book.clean else f"{base}@{main[:12]}"
            units.append(
                Unit("tester", "live", key, f"tst:live{n}", n, (2, 1, n),
                     {"title": i["title"], "base": base, "since": since})
            )

    open_by = {i["number"]: i for i in st["open"]}
    if cfg.accept and book.accept_ready and book.accept_target:
        # One tester unit accepts a batch of merged issues on the build that has them:
        # near one tester session per deploy. The rest wait for the next unit.
        batch = []
        for n in book.accept_ready:
            i = open_by.get(n)
            if not i or set(i["labels"]) & (HOLDS | IGNORED) or not set(i["labels"]) & set(ACCEPTING):
                continue
            live = cfg.live and bool((book.accept_merged.get(n) or {}).get("live") or "needs:live" in i["labels"])
            if live and book.missing:
                continue  # the live environment lacks a file only the owner has (check_files)
            batch.append((priority(i["labels"]), n, live))
        batch.sort()
        issues = [{"n": n, "title": open_by[n]["title"], "live": live, "since": book.live_entry.get(n, ""),
                   "pr": (book.accept_merged.get(n) or {}).get("pr")}
                  for _, n, live in batch[: max(1, cfg.accept_batch)]]
        if issues:
            sha = book.accept_target
            key = f"accept:{sha[:12]}:" + ",".join(f"{x['n']}@{x['since']}#{book.accept_rounds[x['n']]}" for x in issues)
            units.append(Unit("tester", "accept", key, "tst:accept", None, (2, 1, 0),
                              {"sha": sha, "deployed": bool(cfg.deploy_cmd), "issues": issues}))
    seen_prs = set()
    for pr in sorted(st["prs"], key=lambda p: p["number"]):
        seen_prs.add(pr["number"])
        if book.pr_heads.get(pr["number"], pr["head"]) != pr["head"]:
            book.reset_pr(pr["number"])  # someone pushed: new work, not the old failures
        book.pr_heads[pr["number"]] = pr["head"]
        n = pr_issue(pr)
        issue = open_by.get(n) if n is not None else None
        queued = book.train.get(pr["number"])
        if queued and (queued["head"] != pr["head"] or pr["mergeable"] == "CONFLICTING" or pr["draft"]
                       or issue is None or "stage:pr" not in issue["labels"]
                       or set(issue["labels"]) & (HOLDS | IGNORED)):
            # Out of the train's queue: a new head, a conflict, or the issue moved on. Not
            # for UNKNOWN: GitHub reads every PR as UNKNOWN for a while after main moves.
            del book.train[pr["number"]]
            queued = None
        if pr["draft"] or issue is None:
            continue
        labels = set(issue["labels"])
        back = book.handed_back.get(pr["number"])
        if back and back[0] != pr["head"]:
            del book.handed_back[pr["number"]]  # a new head: new work
            back = None
        if back:
            if "stage:pr" not in labels:
                back[1] = True
            elif not back[1]:
                continue  # the state from before the hand-back
            else:
                del book.handed_back[pr["number"]]  # moved back to stage:pr: tried again
        if labels & (HOLDS | IGNORED) or "stage:pr" not in labels or f"wrk:{n}" in book.running:
            continue
        info = {
            "issue": n,
            "head": pr["head"],
            "mergeable": pr["mergeable"],
            "needs_live": cfg.live and "needs:live" in labels,
            "branch": pr.get("branch") or "",
        }
        state = pr["mergeable"]
        if state == "UNKNOWN":
            first = book.unknown_since.setdefault(pr["number"], now)
            if now - first < cfg.unknown_s:
                continue  # GitHub computes mergeability lazily; read it again next poll
        else:
            book.unknown_since.pop(pr["number"], None)
        if book.rebase_wanted.get(pr["number"]) not in (None, pr["head"]):
            del book.rebase_wanted[pr["number"]]  # a new head: the rebase happened, or work moved on
        if state == "MERGEABLE" and queued:
            pass  # passed the gates; waits for the merge train
        elif state == "MERGEABLE" and book.rebase_wanted.get(pr["number"]) == pr["head"]:
            units.append(
                Unit(
                    "auto",
                    "rebase",
                    f"rebase:{pr['number']}@{pr['head'][:12]}@{main[:12]}",
                    f"mrg:{pr['number']}",
                    pr["number"],
                    (0, pr["number"]),
                    info,
                )
            )
        elif state == "MERGEABLE":
            units.append(
                Unit(
                    "auto",
                    "merge",
                    f"merge:{pr['number']}@{pr['head'][:12]}@{main[:12]}",
                    f"mrg:{pr['number']}",
                    pr["number"],
                    (0, pr["number"]),
                    info,
                )
            )
        elif state == "CONFLICTING" and (
            f"rebase:{pr['number']}@{pr['head'][:12]}@{main[:12]}" not in book.tries
        ) and book.rebase_failed.get(pr["number"]) != pr["head"]:
            # Try the mechanical rebase first: conflicts where both sides only added
            # lines need no merger session. Once per head: a head whose rebase failed
            # fails again on the next main (one PR was rebased six times, each with a
            # red sweep, before a merger got it).
            units.append(
                Unit(
                    "auto",
                    "rebase",
                    f"rebase:{pr['number']}@{pr['head'][:12]}@{main[:12]}",
                    f"mrg:{pr['number']}",
                    pr["number"],
                    (0, pr["number"]),
                    info,
                )
            )
        elif cfg.merger_max and book.merger_runs[pr["number"]] >= cfg.merger_max:
            # Main kept moving while merger sessions resolved it, and it conflicted again
            # each time (one PR had four merger sessions before the monitor sent it back).
            # A worker rebases it onto the current main and finishes it instead.
            units.append(
                Unit(
                    "auto",
                    "handback",
                    f"handback:{pr['number']}@{pr['head'][:12]}",
                    f"mrg:{pr['number']}",
                    pr["number"],
                    (0, pr["number"]),
                    {**info, "mergers": book.merger_runs[pr["number"]]},
                )
            )
        else:
            units.append(
                Unit(
                    "merger",
                    "land",
                    f"mrg:{pr['number']}@{pr['head'][:12]}@{main[:12]}",
                    f"mrg:{pr['number']}",
                    pr["number"],
                    (0, pr["number"]),
                    info,
                )
            )
    for k in set(book.unknown_since) - seen_prs:
        del book.unknown_since[k]
    for k in set(book.pr_heads) - seen_prs:
        del book.pr_heads[k]
    for k in set(book.handed_back) - seen_prs:
        del book.handed_back[k]
    for k in set(book.train) - seen_prs:
        del book.train[k]  # merged or closed

    # Never beside a running UX run: a slow run outlasts the gap, and the overlapping
    # runs test the same app and file the same findings.
    ux_running = any(u.kind == "ux" for u in book.running.values())
    if cfg.ux and ux_sha and not ux_running and now - book.last_ux_t >= cfg.ux_gap_s:
        units.append(
            Unit(
                "tester",
                "ux",
                f"ux:{ux_sha[:12]}",
                f"tst:ux:{ux_sha[:12]}",
                None,
                (2, 0, 0),
                {"sha": ux_sha, "deployed": bool(cfg.deploy_cmd)},
            )
        )

    # When no worker or merger has anything to do, the testers make work instead of the
    # pool sitting idle (fast workers can clear a seed backlog in a few hours and then
    # wait): another UX run once POOL_UX_IDLE_GAP_MIN passed since the last one, and,
    # with POOL_SPEC=1, a check of the app against the spec every POOL_SPEC_GAP_MIN.
    busy = any(r in ("worker", "merger") for r in book.role_counts()) or any(
        u.role in ("worker", "merger") and not book.why_not(u, cfg, now) for u in units
    )
    if ux_sha and not busy:
        info = {"sha": ux_sha, "deployed": bool(cfg.deploy_cmd)}
        if (cfg.ux and not ux_running and not any(u.kind == "ux" for u in units)
                and now - book.last_ux_t >= cfg.ux_idle_gap_s):
            units.append(Unit("tester", "ux", f"ux:{ux_sha[:12]}:idle{book.extra_runs}",
                              f"tst:ux:{ux_sha[:12]}", None, (2, 0, 0), {**info, "idle": True}))
        if cfg.spec and now - book.last_spec_t >= cfg.spec_gap_s:
            units.append(Unit("tester", "spec", f"spec:{ux_sha[:12]}:{book.extra_runs}",
                              "tst:spec", None, (2, 0, 1), info))
    elif ux_sha and cfg.spec and now - book.last_spec_t >= cfg.spec_gap_s:
        # Workers never ran out of issues in a six-hour run with 16 sessions, so the
        # idle trigger never fired, even with 11-14 slots idle near the end. A spec
        # check also starts when half the pool would stay free after what is due starts
        # (the "tst:spec" family keeps it to one at a time). Without a global cap there
        # is no half to measure; the idle trigger alone applies.
        agents = sum(v for r, v in book.role_counts().items() if r != "auto")
        due_agents = sum(1 for u in units if u.role != "auto" and not book.why_not(u, cfg, now))
        if cfg.global_cap and cfg.global_cap - agents - due_agents >= cfg.global_cap / 2:
            units.append(Unit("tester", "spec", f"spec:{ux_sha[:12]}:{book.extra_runs}", "tst:spec", None,
                              (2, 0, 1), {"sha": ux_sha, "deployed": bool(cfg.deploy_cmd), "spare": True}))

    units.sort(key=lambda u: u.rank)
    due, stuck = [], []
    for u in units:
        why = book.why_not(u, cfg, now)
        if not why:
            due.append(u)
        elif u.kind != "ux" and (why in ("tries", "family") or (why == "done" and u.kind not in ("live", "accept"))):
            stuck.append(u)
    return due, stuck


def admit(due: list[Unit], book: Book, cfg: Config, now: float) -> list[Unit]:
    """The due units that fit under the caps, in order. Agent sessions count against
    POOL_MAX_WORKERS; the no-agent units only against POOL_CAP_AUTO."""
    counts = book.role_counts()
    total = sum(v for r, v in counts.items() if r != "auto")
    out = []
    for u in due:
        if book.paused.get(u.role, 0) > now:
            continue
        if u.role != "auto" and book.quota_until > now:
            continue
        if u.role == "auto":
            if cfg.auto_cap and counts["auto"] >= cfg.auto_cap:
                continue
            out.append(u)
            counts["auto"] += 1
            continue
        cap = cfg.caps.get(u.role, 0)
        if cap and counts[u.role] >= cap:
            continue
        if cfg.global_cap and total >= cfg.global_cap:
            continue
        out.append(u)
        counts[u.role] += 1
        total += 1
    return out


# --- prompts --------------------------------------------------------------------


PROJECT = ".issuelooper/project.md"


def review_rules() -> str:
    return os.environ.get("REVIEW_RULES", "").strip() or ".issuelooper/review_rules.md"


CLAIM_NOTICE = re.compile(r"^<!-- worker -->\s*<!-- (claim|release) loop=")
ISSUE_BODY_MAX, ISSUE_COMMENTS, COMMENT_MAX = 4000, 6, 900


def issue_text(thread: dict) -> str:
    """The issue's body and its newest comments (claim and release notices left out),
    for the worker's prompt: workers on some models reread the issue with `gh issue
    view` several times a session."""
    body = (thread.get("body") or "").strip()
    out = [body[:ISSUE_BODY_MAX] + (" [...]" if len(body) > ISSUE_BODY_MAX else "")]
    comments = [c for c in thread.get("comments") or [] if not CLAIM_NOTICE.match(c.get("b") or "")]
    if len(comments) > ISSUE_COMMENTS:
        out.append(f"--- ({len(comments) - ISSUE_COMMENTS} older comments not shown)")
    for c in comments[-ISSUE_COMMENTS:]:
        b = (c.get("b") or "").strip()
        out.append(f"--- comment by {c.get('a') or '?'}:\n"
                   + b[:COMMENT_MAX] + (" [...]" if len(b) > COMMENT_MAX else ""))
    return "\n".join(out).strip()


MAP_TOKEN = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./:-]{2,60})`")
MAP_FILES, MAP_TOKENS = 8, 12
# The framework's own files, never what an issue is about.
MAP_EXCLUDE = (":!.claude", ":!.agents", ":!.issuelooper", ":!.githooks", ":!*.lock")


def file_map(root: pathlib.Path, text: str, ref: str = "origin/main", id_pattern: str = "") -> str:
    """Files on `ref` that mention the issue's backticked names (and the requirement ids
    id_pattern matches), most names first: `path (lines): name@line, ...`. A starting
    point for the worker: on some models workers read 39 files a session, half of them
    rereads, and ran 8 searches."""
    found = MAP_TOKEN.findall(text)
    if id_pattern:
        try:
            found += [m.group(0) for m in re.finditer(id_pattern, text)]
        except re.error:
            pass
    tokens = []
    for t in found:
        if t not in tokens and not t.startswith("http"):
            tokens.append(t)
    hits: dict[str, dict[str, int]] = {}
    for t in tokens[:MAP_TOKENS]:
        r = subprocess.run(["git", "-C", str(root), "grep", "-n", "-F", "--max-count=1", "-e", t, ref,
                            "--", ".", *MAP_EXCLUDE], capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            parts = line.split(":", 3)  # ref:path:line:text
            if len(parts) >= 3 and parts[2].isdigit():
                hits.setdefault(parts[1], {}).setdefault(t, int(parts[2]))
    rows = []
    for path, names in sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:MAP_FILES]:
        r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{path}"], capture_output=True,
                           text=True, timeout=30)
        n = r.stdout.count("\n") if r.returncode == 0 else "?"
        rows.append(f"{path} ({n} lines): "
                    + ", ".join(f"{t}@{ln}" for t, ln in sorted(names.items(), key=lambda x: x[1])))
    return "\n".join(rows)


def worker_prompt(c: dict, k: str, timeout: str, pool: str, wip: str = "", live: bool = False,
                  wrap_min: int = 35, issue: str = "", files: str = "", devcheck: bool = False,
                  accept: str = "", checklist: str = "", guard: bool = False) -> str:
    n, token = c["issue"], c["token"]
    live_text = ", labelled needs:live: write Refs #%s, not Fixes" % n if live and c.get("needs_live") else ""
    accept_text = (
        f"\nA pull request that changes {accept} writes `Refs #{n}`, not `Fixes`: after the merge #{n}\n"
        "waits at stage:accept until a tester who did not write it checks it on the deployed build.\n"
        if accept else "")
    if accept and guard:
        accept_text += (
            f"When #{n}'s acceptance is behavior a user drives, write its test as tests/accept/test_accept_{n}.py\n"
            f"(Playwright, scripted mode) instead of a new tests/e2e/test_issue_{n}.py: the supervisor runs\n"
            "tests/accept/ against every deploy.\n")
    if devcheck and checklist:
        accept_text += (
            "\nYour live check (devcheck.py) covers the acceptance checklist rows your change triggers.\n"
            "Declare them in the script, `# accept-rows: <ids>`, and a row that cannot apply as\n"
            "`# accept-skip: <id> <why>`; merge gate 7 refuses a check that misses one.\n"
            + "\n".join("    " + ln for ln in checklist.splitlines()) + "\n")
    devcheck_text = (" and, for a change a user can see, a passing live check\n"
                     "   (devcheck.py) of it") if devcheck else ""
    pr = c.get("pr") or {}
    branch = pr.get("branch") or f"issue-{n}"
    pr_text = ""
    if pr:
        pr_text = (
            f"\n#{n} already has open pull request #{pr['number']} on branch {branch}; it came back\n"
            "to the queue. Read the newest comments on the PR (gh pr view --comments) and on the\n"
            "issue (a review-gate or merge-gate refusal, or a report that the change does not work),\n"
            "check out that branch in your worktree, fix what they ask, re-sweep, push, and update\n"
            "the PR body (gh pr edit). Do not open a second pull request.\n"
        )
    elif wip:
        pr_text = (
            f"\nA previous worker on #{n} ended without a pull request and its work is on branch\n"
            f"origin/{branch} ({wip}). Start your worktree from it\n"
            f"(git worktree add -B {branch} <worktree> origin/{branch}), read what it did and the\n"
            f"release comment on #{n}, and continue from there instead of starting over. Rebase\n"
            "onto origin/main before the sweep.\n"
        )
    if issue:
        pr_text += (
            f"\nIssue #{n} as of the claim (body, then the newest comments; claim notices left\n"
            "out). Work from this; run gh issue view only for a comment newer than the claim:\n\n"
            + "\n".join("    " + ln for ln in issue.splitlines()) + "\n"
        )
    if files:
        pr_text += (
            "\nWhere the issue's names occur on origin/main (a text search for its backticked names\n"
            "and requirement ids: a starting point, not a promise). Read these before searching:\n\n"
            + "\n".join("    " + ln for ln in files.splitlines()) + "\n"
        )
    return f"""Use the issuelooper-worker skill and handle exactly one issue, then stop.

You are pool session {k}, one of the workers the supervisor (ccpool.py) runs side by
side, one per claimable issue (pool cap: {pool}). The supervisor has already claimed issue
#{n} for you: "{c.get("title", "")}" ({c.get("priority") or "no priority"}, came from {c.get("from")}, claim token {token}{live_text}).
Work on #{n} only; do not claim or touch any other issue. Read the issue and its newest
comments first: an issue that was closed and reopened says there why it came back.
{pr_text}
Your paths: worktree {scratch(f"wrk-{k}-{n}-wt")} on branch {branch}, scratch files {scratch(f"wrk-{k}-{n}-")}*.
{accept_text}{PROJECT} says how to set up a worktree, run the app and where tests go. Put your tests
in new files named for the issue (such as test_issue_{n}.py in the right test directory);
never append to a shared test file (that is what makes pull requests conflict).

End in one of two states:
1. A pull request for #{n} is open (or updated), in the PR format of your skill, with
   its sweep record written at its head{devcheck_text}, and #{n} moved to stage:pr with stage:doing
   cleared. Last line of your reply: WORKER-DONE pr #<number>
2. You could not finish: commit what you have on {branch} (the supervisor pushes it for
   the next worker), say on #{n} what blocked you and what is left, then run
   python3 .claude/hooks/claim.py release {n} --token {token} --reason "<one line>"
   (add --to stage:owner when it needs the owner's decision). Last line: WORKER-RELEASED

The supervisor merges your PR once the merge gates and a review gate pass. The review
gate rejects what {review_rules()} names, such as data made up and shown to users as
real when a source fails or a field is missing, and code that special-cases test or
sample inputs: show an honest empty state or error instead.
You never merge, close, push main, or touch .claude/ or .agents/.
If you start the app or any server, use a free port of your own, never the one the
project's own run command uses, and stop it before you finish.
Budget: hard wall-clock timeout of {timeout}. If {wrap_min} minutes pass without a green sweep,
commit your work and release the claim with what you learned rather than being killed
mid-work."""


def merger_prompt(u: Unit, timeout: str) -> str:
    pr, n = u.number, u.info["issue"]
    return f"""Use the issuelooper-merger skill and land exactly one pull request: #{pr}
(issue #{n}, head {u.info["head"][:8]}, GitHub said {u.info["mergeable"]}). Then stop.

The supervisor merges mergeable pull requests itself; this one conflicts with main, so
resolving it is yours. The merge stays the supervisor's: the guard refuses `gh pr merge`
from a merger session, and any write to a PR but #{pr}.
1. Rebase onto origin/main in {scratch(f"mrg-{pr}-wt")} and resolve the conflicts so that both this
   change and what landed on main survive ({PROJECT} says how to set up a worktree).
   Then run `python3 .claude/hooks/sweep_locked.py --update-pr {pr}` there and push with
   --force-with-lease. If the PR body has a `Live check:` line naming another sha than your
   new head, also run `python3 .claude/hooks/devcheck.py --rerun <that sha> --update-pr {pr}`
   there after the sweep (merge gate 7 refuses a head whose change moved since its check, and
   resolving a conflict moves it; the supervisor replays it too, but that costs a merge round).
2. Catch up with main, which moved while you worked: once the sweep is green,
   `git fetch -q origin main`, and if `git merge-base --is-ancestor origin/main HEAD` fails,
   `git rebase origin/main` again. A clean rebase: push it with --force-with-lease and do
   not sweep again (the supervisor sweeps a head that has no sweep record before its merge
   gates, and checks it merged with the current main). Conflicts: resolve them as in step 1,
   sweep, push, and catch up again; three rounds at most, then go on with what you pushed.
3. Run the review gate on the new head:
   `python3 "$SHARED_REPO_PATH/.claude/hooks/review_gate.py" --pr {pr}`. Exit 1: it rejected
   the PR and already handed it back (or sent it to the owner); stop. Exit 2: run it again.
4. Stop there, with #{n} still at stage:pr: do not merge. The supervisor merges the new
   head on its next poll, after checking it merged with the current main and running the
   merge gates, so every merge goes through the same checks. If main moved and the PR
   conflicts again, it starts another merger session (after three, a worker gets it).
5. Hand back only when the rebase needs a decision about behavior, or the sweep is red
   after a clean rebase: comment on the PR naming the hunk or the test, and swap stage:pr
   for stage:triage on #{n}. Once per PR.

Do not read, rebase or merge any other pull request.
Budget: hard wall-clock timeout of {timeout}."""


def ux_journeys_path() -> str:
    """Where a tester keeps its own journeys when the project has none: per repository,
    so they neither leak into another loop on the machine nor come from one."""
    return scratch("ux/journeys.py")


SCORE_LINE = re.compile(r"(?i)\bscore\b\W*(\d{1,3})\s*/\s*100\b")


def ux_score(report: pathlib.Path) -> int | None:
    """N from a UX report's `Score: N/100` (asked for on its first line; looked for in the
    first ten), or None when it has none."""
    try:
        lines = report.read_text(errors="replace").splitlines()[:10]
    except OSError:
        return None
    for ln in lines:
        m = SCORE_LINE.search(ln)
        if m and int(m.group(1)) <= 100:
            return int(m.group(1))
    return None


def tester_prompt(u: Unit, port: int, timeout: str, journeys: str = "") -> str:
    rules = (
        f"\n\nYour port is $TST_PORT={port}, yours alone (never the port the project's own run "
        f"command uses; scratch files and any app database under {scratch(f'tst-{port}-')}*). Stop every "
        "server you started before you finish. Stay inside your unit. Issues labeled ux:proposed "
        "are proposals waiting for the owner: do not test, comment on or relabel them."
        f"\nBudget: hard wall-clock timeout of {timeout}."
    )
    if u.kind == "live":
        return (
            "Use the issuelooper-tester skill (section Live check) and do exactly one unit of "
            "work, then stop.\n\n"
            f'Your unit: stage:live issue #{u.number} ("{u.info.get("title", "")}"). Read the '
            "issue and the merged PR, then verify the change against the real services, with the "
            f"app started from main on your port in live mode as {PROJECT} describes (it also "
            "says where the live credentials are). Post a live-verified: line, or say on the "
            "thread what you saw; if you could not run the check at all (credentials missing, the live "
            "server not starting), a `live-check-blocked: <what is missing>` line instead."
            + (
                f" Only a check of what is on main now counts: #{u.number} entered stage:live at "
                f"{u.info['since']}, so a live-verified: line older than that is stale."
                if u.info.get("since")
                else ""
            )
            + rules
        )
    sha = u.info["sha"]
    if u.kind == "accept":
        rows = "\n".join(
            f"  #{x['n']} \"{x['title']}\"" + (f", merged in PR #{x['pr']}" if x.get("pr") else "")
            + (", in live mode" if x.get("live") else "")
            for x in u.info["issues"])
        cl = config.checklist_text()
        return (
            "Use the issuelooper-tester skill (section Acceptance check) and do exactly one unit of work, "
            "then stop.\n\n"
            f"Your unit: these merged issues, accepted at commit {sha[:12]} ("
            + ("the deployed build" if u.info.get("deployed") else "origin/main; this repository does not deploy")
            + f"), started from that commit on your port as {PROJECT} says:\n{rows}\n\n"
            "Someone else wrote each change. For each issue, read it, its comments and its merged PR, then drive "
            "what it asked for on screen with a script, in scripted mode or in live mode where marked, through "
            "the checklist rows its change needs. Then comment once on that issue, signed `<!-- tester -->`: "
            "what you ran and what you saw (quote the screen), ending with a line of its own, "
            "`accept-verified: <what ran>` when every part works, or `accept-blocked: <what is missing>` when "
            "you could not run the check at all; neither when something is wrong or missing (say what)."
            + (f"\n\n{cl}" if cl else "")
            + "\n\nSave the script of each issue you verify as $LOGDIR/accept/test_accept_<N>.py, and write "
            "$LOGDIR/accept_report.md, one line per issue: its number and verdict."
            + rules
        )
    where = (
        f"the deployed app now runs {sha[:12]}" if u.info.get("deployed")
        else f"origin/main is at {sha[:12]} (this repository does not deploy)"
    )
    if u.kind == "spec":
        return (
            "Use the issuelooper-tester skill (section Spec check) and do exactly one check, then "
            "stop.\n\n"
            f"Your unit: {where}, and "
            + ("half of the pool's sessions are free" if u.info.get("spare") else "no worker has anything to do")
            + ". Start the app at that commit on "
            f"your port and check it against the product's spec ({PROJECT} says where it is), one "
            "requirement at a time, on screen, and file at most five gaps a user would meet. "
            "Write the check's report to $LOGDIR/spec_report.md."
            + rules
        )
    if journeys:
        walk = (f"run the project's journeys, `{journeys}`, from your extracted tree as {PROJECT} says "
                "(do not write a journeys file of your own; put any probes of your own in "
                f"{scratch(f'tst-{port}-probes.py')})")
    else:
        walk = (f"walk the user journeys in {ux_journeys_path()} (this repository's, kept from run to run; "
                "write or extend them first if they do not cover what the spec and the recent owner "
                "requests describe)")
    return (
        "Use the issuelooper-tester skill (section UX run) and do exactly one run, then stop.\n\n"
        f"Your unit: {where}. Start the app at that commit on your port in the mode {PROJECT} "
        f"names for UX runs, {walk}, save every "
        f"screenshot under .ux/{sha[:12]}/, read all of them together against the product's spec "
        f"({PROJECT} says where it is) as a harsh UX reviewer, and file at most five new "
        "findings. Write the run's report to $LOGDIR/ux_report.md. Its first line must be "
        "`Score: N/100`, N being the 0-100 score a user would give the app on the skill's scale; "
        "the supervisor reads that line."
        + rules
    )


# --- the supervisor -------------------------------------------------------------


def review_record(head: str) -> dict:
    """The review gate's record for HEAD ({} if none)."""
    for d in config.record_dirs("review"):
        try:
            rec = json.loads((d / f"{head}.json").read_text())
        except (OSError, ValueError):
            continue
        return rec if isinstance(rec, dict) else {}
    return {}


def review_reason(head: str) -> str:
    """The review gate's reason for its verdict on HEAD, from its record ('' if none)."""
    return str(review_record(head).get("reason") or "")[:300]


def review_cut(head: str) -> bool:
    """Whether the review gate saw a production file of HEAD only in part: its reject
    then says the diff was too long, not that the change is wrong."""
    return any(is_source(str(f).rsplit(" (", 1)[0]) for f in review_record(head).get("left_out") or [])


class BrokerLink:
    """The supervisor's link to a monitor hosted by broker.py (the headless launch)."""

    def __init__(self, sock: str):
        self.sock = sock

    def status(self) -> dict:
        return broker_call(self.sock, cmd="status")

    def say(self, text: str) -> dict:
        return broker_call(self.sock, cmd="say", text=text, tag="pool_feed")


def broker_call(sock: str, **req) -> dict:
    from broker_socket import socket_path
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(10)
    try:
        s.connect(str(socket_path(sock)))
        s.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data)
    finally:
        s.close()


def repo_root() -> pathlib.Path:
    shared = os.environ.get("SHARED_REPO_PATH", "").strip()
    return pathlib.Path(shared) if shared else HERE.parents[1]


RATE_LIMITED = re.compile(r"(?i)secondary rate limit|abuse detection|submitted too quickly")


def run_gh(cfg: "Config", args: tuple[str, ...], stdin: str | None = None,
           log: collections.abc.Callable[..., None] | None = None) -> subprocess.CompletedProcess:
    """gh, retried after GitHub's secondary rate limit, which blocks content creation for
    a minute or more after a burst of writes. `gh issue create` has exited 0 on it with
    no URL, so the text counts on either stream."""
    for attempt in range(4):
        r = subprocess.run([cfg.gh, *args], input=stdin, capture_output=True, text=True, timeout=120,
                           cwd=repo_root())
        if attempt == 3 or not RATE_LIMITED.search(r.stderr + r.stdout):
            return r
        wait = cfg.gh_rate_wait_s * 2 ** attempt
        if log:
            log("gh_rate_wait", command=" ".join(args[:3]), wait_s=wait, attempt=attempt + 1)
        time.sleep(wait)
    return r


SOURCE_EXT = (".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".scss", ".html", ".vue", ".svelte",
              ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".swift", ".cs")


def is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (path.startswith(("tests/", "test/")) or "/tests/" in path or "/test/" in path
            or name.startswith("test_") or ".test." in name or ".spec." in name
            or name.rsplit(".", 1)[0].endswith("_test"))


def touches_live(paths) -> bool:
    """Whether a change triggers the checklist's live rows ([accept.triggers])."""
    return any("live" in groups and any(config.path_matches(p, glob) for p in paths)
               for glob, groups in config.checklist()["triggers"])


def is_source(path: str) -> bool:
    prefixes = [x.strip() for x in os.environ.get("REVIEW_PATHS", "src/").split(",") if x.strip()]
    return (path.endswith(SOURCE_EXT) and not is_test(path)
            and (not prefixes or path.startswith(tuple(prefixes))))


# The monitor's message when the queue changed; POOL_MONITOR_FEED names a file to use instead.
FEED_HEADER = (
    "Pool update: the queue changed. Use the issuelooper-monitor skill and run its pass over "
    "what changed: triage the untriaged issues, re-rank the queue, hold or close low-value work, "
    "fix dependencies, act on stuck or failed work, and raise the owner's decisions at "
    "stage:owner (a question there waits for the owner's own comment: add context or a "
    "recommendation, never the answer). If nothing needs you, say so in one line. End with the "
    "owner's decisions, if any, as one question."
)

SESSION = (
    'set -o pipefail; "$@" 2>>"$POOL_ERR" | tee -a "$POOL_RAW" | $POOL_RENDER_CMD >> "$POOL_LOG"; '
    'exit "${PIPESTATUS[0]}"'
)


def parse_lsof(text: str) -> dict[int, str]:
    """pid -> working directory from `lsof -a -d cwd -Fn` (lines `p<pid>` then `n<path>`)."""
    out: dict[int, str] = {}
    pid = None
    for line in text.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def cpu_ticks(pid: int) -> int | None:
    """User and system CPU time a process has used, in clock ticks (None: unknown)."""
    try:
        fields = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        return None


def process_cwds() -> dict[int, str]:
    """pid -> working directory of every process this user can read: /proc on Linux,
    else lsof ({} when neither works)."""
    if os.path.isdir("/proc/self"):
        out = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    out[int(d)] = os.readlink(f"/proc/{d}/cwd")
                except OSError:
                    pass
        return out
    try:
        r = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_lsof(r.stdout)


def kill_in(prefixes: tuple[str, ...]) -> list[dict]:
    """Kill the processes whose working directory is at or under one of `prefixes`
    (matched as `cwd + "/"`, so a worktree path with a trailing / matches itself and
    nothing beside it): SIGTERM, then SIGKILL after 5 s. Matched by working directory
    alone, never by name; the supervisor is never one of them. Returns what was killed."""
    if not prefixes:
        return []
    spare = {1, os.getpid(), os.getppid()}
    found = {pid: cwd for pid, cwd in process_cwds().items()
             if pid not in spare and (cwd + "/").startswith(prefixes)}
    procs = []
    for pid, cwd in found.items():
        try:
            cmd = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            cmd = ""
        procs.append({"pid": pid, "cwd": cwd, "cmd": cmd.strip()[:200]})
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and any(pid_alive(p) for p in found):
        time.sleep(0.2)
    for pid in found:
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return procs


def rescue_worktree(wt: str, keep_existing: bool = False) -> bool:
    """Write the worktree's work that is not on its branch's remote (uncommitted, or
    committed and not pushed) to <wt>-rescued.diff beside it, unless keep_existing and
    that file is already there. Returns whether there was such work."""
    git = ["git", "-C", wt]
    subprocess.run(git + ["add", "-A", "-N", "."], capture_output=True)
    dirty = subprocess.run(git + ["status", "--porcelain"], capture_output=True, text=True).stdout
    ahead = subprocess.run(git + ["log", "--oneline", "@{u}..HEAD"], capture_output=True, text=True)
    if ahead.returncode != 0:
        ahead = subprocess.run(git + ["log", "--oneline", "origin/main..HEAD"], capture_output=True, text=True)
    if not (dirty.strip() or ahead.stdout.strip()):
        return False
    rescued = pathlib.Path(f"{wt}-rescued.diff")
    if not (keep_existing and rescued.exists()):
        rescued.write_text(subprocess.run(git + ["diff", "origin/main"], capture_output=True, text=True).stdout)
    return True


SESSION_SCRATCH = re.compile(r"^(?:wrk|mrg|tst|main|fresh)-")
UNWALKED = {".git", ".venv", "node_modules", "__pycache__"}
OTHER_ROOT = re.compile(r"^issuelooper-.+-[0-9a-f]{8}$")


def prune_scratch(root: pathlib.Path | None = None, stale_s: float = 2 * 86400) -> list[str]:
    """Remove what earlier runs left in scratch, once it is older than stale_s (nothing
    in it changed since): this repository's session worktrees and files (wrk-, mrg-,
    tst-, main-, fresh-), its live-check and UX output, and, when its scratch root is a
    default one (issuelooper-<name>-<hash> in the temporary directory), the other default
    roots beside it. A stale worktree that still holds work not on its remote (a
    supervisor that crashed never saved it) leaves <wt>-rescued.diff first, unless one
    is there; a rescued diff itself goes only once stale. The records the merge gates
    read (sweep/, review/, devcheck/*.json) stay. Run when nothing of this repository's
    supervisor runs: at its start, and by `issuelooper stop --all`. In run 9 /tmp ran out
    of inodes, mostly on what earlier runs and tests left."""
    root = root or config.scratch_root()
    now, gone = time.time(), []

    def newest(p: pathlib.Path, deep: bool = False) -> float:
        """The newest mtime of p and what is in it (all of a worktree but its .git,
        .venv and the like when deep; an edit deep in a tree leaves the top alone)."""
        try:
            times = [p.lstat().st_mtime]
            if p.is_dir() and not p.is_symlink():
                times += [c.lstat().st_mtime for c in p.iterdir()]
            if deep:
                for d, dirs, files in os.walk(p):
                    dirs[:] = [x for x in dirs if x not in UNWALKED]
                    times += [os.lstat(os.path.join(d, f)).st_mtime for f in files + dirs]
            return max(times)
        except OSError:
            return now

    def remove(p: pathlib.Path) -> None:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
        gone.append(str(p))

    if root.is_dir():
        kept = set()  # the rescued diffs of worktrees removed in this pass
        for p in sorted(root.iterdir()):  # a worktree before its <wt>-rescued.diff
            if not SESSION_SCRATCH.match(p.name) or p in kept:
                continue
            tree = p.is_dir() and (p / ".git").exists()
            if now - newest(p, deep=tree) <= stale_s:
                continue
            if tree:
                if rescue_worktree(str(p), keep_existing=True):
                    kept.add(p.with_name(p.name + "-rescued.diff"))
                subprocess.run(["git", "-C", str(repo_root()), "worktree", "remove", "--force", str(p)],
                               capture_output=True)
            if p.exists() or p.is_symlink():
                remove(p)
            else:
                gone.append(str(p))
        for sub in (root / "devcheck" / "out", root / "ux"):
            if sub.is_dir():
                for p in sub.iterdir():
                    if now - newest(p) > stale_s:
                        remove(p)
        subprocess.run(["git", "-C", str(repo_root()), "worktree", "prune"], capture_output=True)
    parent = root.parent
    if OTHER_ROOT.match(root.name) and parent.is_dir():
        for p in parent.iterdir():
            if (OTHER_ROOT.match(p.name) and p.is_dir() and p.resolve() != root.resolve()
                    and now - newest(p) > stale_s):
                remove(p)
    return gone


def remove_worktree(root: str, wt: str, log=None) -> None:
    """Remove one of the supervisor's own worktrees, after killing what still runs in
    it. Its checks left app servers behind (a test fixture stopped `uv run` but not the
    server under it): 109 at the end of run 9, 12.3 GB, in main-<sha>, mrg-<pr>-sweep
    and mrg-<pr>-auto, which worker cleanup never looked at."""
    procs = kill_in((wt.rstrip("/") + "/",))
    if procs and log:
        log("leftovers_killed", path=wt, processes=procs)
    subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)


def pid_alive(pid: int) -> bool:
    """Whether pid runs (a zombie, already dead and waiting for its parent, does not)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


# Session outcomes that put the claim back without counting a failed attempt.
NOT_A_FAILURE = ("startup crash", "quota error", "stopped with the supervisor")


class Session(threading.Thread):
    def __init__(self, pool: "Pool", unit: Unit, seq: int):
        super().__init__(daemon=True, name=f"{unit.role}-{seq}")
        self.pool, self.unit, self.seq = pool, unit, seq
        self.proc: subprocess.Popen | None = None
        self.started = time.time()
        self.port = 0
        self.left_pr = False  # a worker session that ended with its pull request open (claim.py finish)
        slug = re.sub(r"[^\w.-]+", "_", unit.key)[:60]
        self.dir = pool.cfg.logdir / f"{seq:04d}-{unit.role}-{slug}"
        try:
            self.cli = agent_cli.for_role(unit.role)
        except ValueError:  # session() names the bad setting when it starts
            self.cli = agent_cli.get(agent_cli.DEFAULT)

    def run(self):
        try:
            outcome = self.work()
        except Exception as exc:  # noqa: BLE001 - a session never takes the pool down
            outcome = f"error: {exc!r}"[:300]
        self.pool.finished(self, outcome)

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "CC_ROLE"}
        env.update(POOL_WORKER=str(self.seq), POOL_UNIT=self.unit.key, LOGDIR=str(self.dir))
        return env

    def sh(self, args: list[str], env: dict, name: str) -> int:
        with open(self.dir / f"{name}.out", "a") as out:
            return subprocess.run(
                args, env=env, cwd=repo_root(), stdout=out, stderr=subprocess.STDOUT, timeout=600
            ).returncode

    def session(self, prompt: str, env: dict) -> tuple[int, float, bool]:
        cfg = self.pool.cfg
        role = self.unit.role
        timeout = cfg.timeouts[role]
        cli = self.cli = agent_cli.for_role(role, env)
        (self.dir / "prompt.txt").write_text(prompt)
        env = dict(
            env,
            POOL_ERR=str(self.dir / "session.err"),
            POOL_RAW=str(self.dir / "session.jsonl"),
            POOL_LOG=str(self.dir / "session.log"),
            POOL_RENDER_CMD=cfg.render,
        )
        # ccrole.sh picks the CLI by the same rule; naming it keeps the two in step.
        env[f"CCROLE_{role.upper()}_CLI"] = cli.name
        base = ["bash", "-c", SESSION, "ccpool-session", cfg.ccrole, role, "--"]
        start = time.time()
        # The CLI's own limit (agy's print timeout) is the hard deadline; the soft one
        # ends a session that is past its timeout and has gone quiet.
        soft, hard = start + seconds(timeout), start + seconds(timeout) + 900
        ceiling = start + cfg.timeout_ceiling * seconds(timeout) if cfg.timeout_ceiling else float("inf")
        # The CLI's own limit leaves room for the clocks to stand still (below); the
        # supervisor enforces the rest.
        cmd = base + cli.first_turn(prompt, hard - start + cfg.retry_pause_max_s)
        with open(self.dir / "wrapper.err", "w") as err:
            # Under the pool's lock, after a look at stopping: stop() either sees this
            # process and kills it, or it is never started. A worker still claiming its
            # issue when `issuelooper stop --all` ran started its agent afterwards and
            # kept running with nobody to stop it.
            with self.pool.lock:
                if self.pool.stopping:
                    return 143, 0.0, False
                self.proc = subprocess.Popen(
                    cmd,
                    env=env,
                    cwd=repo_root(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=err,
                    start_new_session=True,
                )
            resumes, rc = 0, None
            quota_k = 0  # resumes after a quota error, each after a doubling wait
            stall_k = 0  # resumes after the model went silent between steps
            idle_k = 0  # resumes after the model's turn ended on a background task
            waited_on: list[int] = []  # processes the stuck-stream rule last waited for
            # A worker still going at POOL_WORKER_WRAP_S is cut and resumed in the same
            # conversation with a few minutes to commit and release: workers that ran
            # their full timeout were a quarter of all worker time, and most of them
            # ended with nothing saved.
            wrap_at = start + cfg.wrap_s() if role == "worker" and cfg.wrap_s() else None
            wrapped = False
            # Time the CLI spent retrying a failed model call inside a turn. Most worker
            # timeouts on one model were sessions waiting there; the clocks stop for it,
            # so the work gets its full budget.
            paused, tick = 0.0, time.time()

            def resume(conv: str, why: str, text: str = "", budget: float = 0) -> None:
                nonlocal resumes
                resumes += 1
                self.kill()
                again = base + cli.resume(
                    text or (
                        "Your last turn ended on a model quota error (429). "
                        "Continue the task from where you stopped."
                    ),
                    conv,
                    (budget or max(60, hard - time.time())) + cfg.retry_pause_max_s - paused,
                )
                with self.pool.lock:
                    if self.pool.stopping:
                        return  # the killed process stays; its exit ends the loop
                    with open(self.dir / "resumes.log", "a") as rl:
                        rl.write(f"{time.time():.0f} resume {resumes} conversation {conv} ({why})\n")
                    self.proc = subprocess.Popen(
                        again, env=env, cwd=repo_root(), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)

            while rc is None:
                try:
                    rc = self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    now = time.time()
                    step, tick = now - tick, now
                    retrying = paused < cfg.retry_pause_max_s and self.in_model_retry()
                    if retrying:
                        add = min(step, cfg.retry_pause_max_s - paused)
                        paused += add
                        soft, hard = soft + add, hard + add
                        if wrap_at and not wrapped:
                            wrap_at += add
                    quiet = self.quiet_s()
                    if time.time() > ceiling:
                        with open(self.dir / "resumes.log", "a") as rl:
                            rl.write(f"{time.time():.0f} killed at the ceiling, "
                                     f"{cfg.timeout_ceiling:g} x the {timeout} timeout\n")
                        self.kill()
                        rc = 124
                        break
                    if time.time() > hard + 300 or (time.time() > soft and quiet > 300):
                        self.kill()
                        rc = 124
                        break
                    if wrap_at and not wrapped and time.time() > wrap_at:
                        wrapped = True
                        conv = self.conversation()
                        if conv:
                            budget = cfg.worker_wrap_budget_s
                            soft = hard = time.time() + budget
                            resume(conv, "wrap-up", (
                                "Time is almost up. Within the next few minutes: if your sweep is green, "
                                "push and open (or update) the pull request as your skill says; otherwise "
                                "commit what you have on your branch, say on the issue what is done and "
                                "what is left, and release the claim with claim.py release. Then stop."
                            ), budget)
                            continue
                    # The model went silent after a step finished (no tool is running):
                    # workers have stopped for half an hour right after a test run
                    # printed its result, until they timed out. The CLI waiting between
                    # its own retries is not a stall (until the pause cap).
                    stall_s = 0 if retrying else self.stall_after(cfg)
                    conv = self.model_stalled(stall_s) if stall_s else None
                    if conv and stall_k < 2 and min(hard, ceiling) - time.time() > 120:
                        stall_k += 1
                        resume(conv, f"no step for {stall_s:.0f} s after the last one finished", (
                            f"Your session stopped responding for {stall_s / 60:.0f} minutes after its last "
                            "step and was restarted. Continue the task from where you stopped. If a server or "
                            "command you started is still running, stop it by its port or PID first."
                        ), max(120, hard - time.time()))
                        continue
                    # The model ended its turn while a command it started still runs, and
                    # agy waits for that task until its own limit; the model does not act
                    # again. Eight sessions sat like that until their 45-minute timeout.
                    conv = self.background_idle(cfg.background_idle_s) if cfg.background_idle_s else None
                    if conv and idle_k < 2 and min(hard, ceiling) - time.time() > 120:
                        idle_k += 1
                        resume(conv, f"turn over, waiting on a background task for {cfg.background_idle_s:.0f} s", (
                            "Your last turn ended while a command you started was still running in the background, "
                            "and the session was restarted, which stopped that command. Run what you still need in "
                            "the foreground and wait for it to finish (start servers with nohup and stop them by "
                            "port before you end), then finish the task."
                        ), max(120, hard - time.time()))
                        continue
                    if conv:
                        with open(self.dir / "resumes.log", "a") as rl:
                            rl.write(f"{time.time():.0f} ended: turn over, waiting on a background task\n")
                        self.kill()
                        rc = 0
                        break
                    # agy's stream stopped behind a command it lost track of while the agent
                    # went on, finished, and the process stayed: nine mergers in one run sat
                    # like that, unseen, until their 45-minute timeout. Its own record of
                    # the conversation shows the end; nothing is left to resume.
                    if cfg.unseen_idle_s and self.finished_unseen(cfg.unseen_idle_s):
                        # Not while something it started still works: two mergers in run 8
                        # had replied "waiting for the sweep" and the sweep was killed.
                        busy = self.busy_processes()
                        if busy:
                            if busy != waited_on:
                                with open(self.dir / "resumes.log", "a") as rl:
                                    rl.write(f"{time.time():.0f} conversation finished behind a stuck stream; "
                                             f"waiting for {len(busy)} process(es) still working in its scratch "
                                             f"paths ({', '.join(map(str, busy[:5]))})\n")
                            waited_on = busy
                            continue
                        with open(self.dir / "resumes.log", "a") as rl:
                            rl.write(f"{time.time():.0f} ended: the stream stopped behind a step; the "
                                     "conversation finished without it\n")
                        self.kill()
                        rc = 0
                        break
                    wait = quota_backoff(quota_k + 1, cfg.quota_wait_s, cfg.quota_wait_max_s)
                    conv = self.quota_stalled(quiet=wait)
                    if conv and quota_k < cfg.quota_resumes and min(hard, ceiling) - time.time() > 120:
                        quota_k += 1
                        self.pool.quota_seen(role)
                        resume(conv, f"stalled while alive, quota resume {quota_k} after {wait:.0f} s")
                    continue
                # The CLI exited. After a quota error it usually does, so also resume here
                # when the last event is that error and the unit's work is not done. The
                # wait doubles from POOL_QUOTA_WAIT_S up to POOL_QUOTA_WAIT_MAX_S: a
                # capacity shortage can last hours, and every session retrying at once
                # is the load the error asks to reduce.
                conv = self.quota_stalled(quiet=0)
                if conv and quota_k < cfg.quota_resumes and not self.work_done():
                    wait = quota_backoff(quota_k + 1, cfg.quota_wait_s, cfg.quota_wait_max_s)
                    self.pool.quota_seen(role)
                    if time.time() + wait < min(hard, ceiling) - 120:
                        quota_k += 1
                        with open(self.dir / "resumes.log", "a") as rl:
                            rl.write(f"{time.time():.0f} quota wait {wait:.0f} s before resume {quota_k}\n")
                        if self.pool.halt.wait(wait):
                            break  # stopped during the wait: wrap up now, not after it
                        resume(conv, f"exited with {rc}, quota resume {quota_k} after {wait:.0f} s")
                        rc = None
        elapsed = time.time() - start
        if paused:
            with open(self.dir / "resumes.log", "a") as rl:
                rl.write(f"{time.time():.0f} clocks paused {paused:.0f} s in the CLI's model-call retries\n")
        try:
            errtext = (self.dir / "session.err").read_text(errors="replace")
        except OSError:
            errtext = ""
        timed_out = (rc == 124 or bool(cli.timeout_text and cli.timeout_text in errtext)
                     or elapsed >= seconds(timeout) + 900 + paused or time.time() > ceiling)
        return rc, elapsed, timed_out

    def events(self) -> list[dict]:
        try:
            return agent_cli.parse((self.dir / "session.jsonl").read_text(errors="replace").splitlines())
        except OSError:
            return []

    def quiet_s(self) -> float:
        """Seconds since the session last wrote to its stream (0 when it has none)."""
        try:
            return time.time() - (self.dir / "session.jsonl").stat().st_mtime
        except OSError:
            return 0

    def conversation(self):
        """The session (conversation) id in this session's stream, or None."""
        return self.cli.session_id(self.events())

    def stall_after(self, cfg: "Config") -> float:
        """Seconds of model silence that count as a stall for this session's model:
        shorter for models whose steps are quick (POOL_STALL_FAST_MODELS, matched as
        substrings of the model name; Claude models answered a step about three times
        faster than others), so a stalled stream costs minutes, not ten."""
        if not cfg.stall_s:
            return 0
        model = self.cli.model(self.events()) or agent_cli.model_for(self.unit.role)
        fast = [m.strip() for m in cfg.stall_fast_models.split(",") if m.strip()]
        return cfg.stall_fast_s if any(m in model for m in fast) else cfg.stall_s

    def model_stalled(self, quiet: float):
        """The session id when nothing was written for `quiet` s and the stream's last
        event is a finished step (not a running tool, not a result), else None."""
        if not (self.dir / "session.jsonl").exists() or self.quiet_s() < quiet:
            return None
        events = self.events()
        return self.cli.session_id(events) if self.cli.model_stalled(events) else None

    def background_idle(self, quiet: float):
        """The session id when the CLI says the model's turn is over while it waits for a
        background task (its stderr's last line), and nothing was written for `quiet` s;
        else None."""
        pattern = self.cli.idle_wait
        err = self.dir / "session.err"
        if pattern is None or not err.exists():
            return None
        try:
            lines = [ln for ln in err.read_text(errors="replace")[-2000:].splitlines() if ln.strip()]
            last = max(err.stat().st_mtime, (self.dir / "session.jsonl").stat().st_mtime
                       if (self.dir / "session.jsonl").exists() else 0)
        except OSError:
            return None
        if not lines or not pattern.search(lines[-1]) or time.time() - last < quiet:
            return None
        return self.conversation()

    def finished_unseen(self, quiet: float) -> bool:
        """Whether the stream has been silent for `quiet` s behind a step while the
        conversation finished without it (agent_cli)."""
        return self.quiet_s() >= quiet and self.cli.finished_unseen(self.events(), quiet)

    def in_model_retry(self) -> bool:
        """Whether the CLI is retrying a failed model call inside the turn (agent_cli)."""
        return self.cli.in_model_retry(self.events())

    def quota_stalled(self, quiet: float = 60):
        """The session id when the stream's last event is a result that ended on a
        quota error and nothing has been written for `quiet` s, else None."""
        if not (self.dir / "session.jsonl").exists() or self.quiet_s() < quiet:
            return None
        events = self.events()
        if not events or not self.cli.quota_result(events[-1]):
            return None
        return self.cli.session_id(events)

    def ended_on_quota(self) -> bool:
        """Whether the session's last word was a quota error: its stream's last event, or,
        when the stream has no result at the end, the tail of its stderr."""
        events = self.events()
        if events and self.cli.quota_result(events[-1]):
            return True
        if events and self.cli.is_result(events[-1]):
            return False
        try:
            err = (self.dir / "session.err").read_text(errors="replace")[-3000:]
        except OSError:
            return False
        return bool(self.cli.quota.search(err))

    def work_done(self) -> bool:
        """Whether the unit's result is already there (on GitHub, or the tester's
        results file), so a quota error on the closing turn needs no resume. Unknown
        counts as not done."""
        u = self.unit
        if u.role == "worker" and not (self.dir / "claim").exists():
            return True  # the agent released its claim
        if u.kind == "ux":
            return (self.dir / "ux_report.md").exists()
        if u.kind == "spec":
            return (self.dir / "spec_report.md").exists()
        if u.kind == "accept":
            return (self.dir / "accept_report.md").exists()
        n = u.info.get("issue") if u.role == "merger" else u.number
        try:
            out = subprocess.run(
                [self.pool.cfg.gh, "issue", "view", str(n), "--json", "state,labels,comments"],
                capture_output=True, text=True, timeout=60, cwd=repo_root())
            data = json.loads(out.stdout) if out.returncode == 0 else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            data = {}
        if not data:
            return False
        labels = {lab.get("name") for lab in data.get("labels") or []}
        is_open = data.get("state") == "OPEN"
        if u.role == "worker":
            return not is_open or claimlib.DOING not in labels
        if u.role == "merger":
            return not is_open or "stage:pr" not in labels
        return any(  # live: the tester's signed comment since this session started
            "<!-- tester -->" in (c.get("body") or "")
            and claimlib.parse_time(c.get("createdAt") or "1970-01-01T00:00:00Z").timestamp() > self.started
            for c in data.get("comments") or []
        )

    def scratch_prefixes(self) -> list[str]:
        """Where this session's worktrees and scratch files live, as the prompts and
        skills name them: wrk-<seq>-*, mrg-<pr>-*, tst-<port>-* under $ISSUELOOPER_TMP."""
        u = self.unit
        if u.role == "worker":
            name = f"wrk-{self.seq}-"
        elif u.role == "merger" and u.number is not None:
            name = f"mrg-{u.number}-"
        elif u.role == "tester" and self.port:
            name = f"tst-{self.port}-"
        else:
            return []
        root = str(config.scratch_root())
        return sorted({f"{t}/{name}" for t in (root, os.path.realpath(root))})

    def busy_processes(self, sample: float = 2.0) -> list[int]:
        """Processes in this session's scratch paths that used CPU over `sample` seconds
        (a sweep, a test run), not idle ones (a server left running). Where CPU time
        cannot be read, every process found counts."""
        prefixes = tuple(self.scratch_prefixes())
        if not prefixes:
            return []
        spare = {1, os.getpid(), os.getppid()}
        found = [pid for pid, cwd in process_cwds().items() if pid not in spare and cwd.startswith(prefixes)]
        before = {pid: cpu_ticks(pid) for pid in found}
        if found:
            time.sleep(sample)
        busy = []
        for pid in found:
            if not pid_alive(pid):
                continue
            after = cpu_ticks(pid)
            if before[pid] is None or (after is not None and after - before[pid] >= 5):
                busy.append(pid)
        return sorted(busy)

    def kill_leftovers(self) -> list[int]:
        """Kill the processes still running in this session's scratch paths: servers and
        test runs a session started with setsid or nohup leave its process group and
        outlive it (five were found hours after their sessions). Matched by working
        directory alone, never by name; the supervisor is never one of them."""
        procs = kill_in(tuple(self.scratch_prefixes()))
        if not procs:
            return []
        self.pool.log("leftovers_killed", session=self.seq, role=self.unit.role, processes=procs)
        return sorted(p["pid"] for p in procs)

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass

    def work(self) -> str:
        u, cfg = self.unit, self.pool.cfg
        self.dir.mkdir(parents=True, exist_ok=True)
        env = self.env()
        if u.role == "auto":
            return {"merge": self.auto_merge, "rebase": self.auto_rebase, "requeue": self.requeue,
                    "handback": self.merger_handback}.get(u.kind, self.reclaim)(env)
        claim_file = self.dir / "claim"
        if u.role == "worker":
            pool = str(cfg.caps["worker"] or "unlimited")
            env.update(WRK_K=str(self.seq), WRK_CLAIM_FILE=str(claim_file), WORKER_POOL=pool)
            rc = self.sh(
                [
                    sys.executable,
                    str(HERE / "claim.py"),
                    "claim",
                    "--loop",
                    str(self.seq),
                    "--issue",
                    str(u.number),
                    "--claim-file",
                    str(claim_file),
                ],
                dict(env, CC_ROLE="worker", CLAIM_JITTER_S="0"),
                "claim",
            )
            if rc == 3:
                why = re.findall(r"is not claimable: (.*)", (self.dir / "claim.out").read_text(errors="replace"))
                self.pool.log("unclaimable", issue=u.number, why=why[-1] if why else "no reason given")
                return "unclaimable"
            if rc != 0 or not claim_file.exists():
                return f"claim failed (exit {rc})"
            claim = json.loads(claim_file.read_text())
            n = int(claim["issue"])
            wip = "" if claim.get("pr") else self.wip_branch(n)
            thread = self.issue_thread(n)
            try:
                files = file_map(repo_root(), (thread.get("body") or "") + "\n" + claim.get("title", ""),
                                 id_pattern=cfg.map_id_pattern)
            except (OSError, subprocess.SubprocessError):
                files = ""
            prompt = worker_prompt(claim, str(self.seq), cfg.timeouts["worker"], pool, wip,
                                   cfg.live, int(cfg.wrap_s() // 60) or 35,
                                   issue_text(thread) if thread else "", files, cfg.devcheck,
                                   ", ".join(cfg.accept_paths) if cfg.accept else "", config.checklist_text(),
                                   bool(cfg.accept_guard))
        elif u.role == "merger":
            env.update(MERGE_PR=str(u.number))
            prompt = merger_prompt(u, cfg.timeouts["merger"])
        else:
            self.port = self.pool.take_port()
            env.update(TST_PORT=str(self.port))
            prompt = tester_prompt(u, self.port, cfg.timeouts["tester"], cfg.ux_journeys)

        rc, elapsed, timed_out = self.session(prompt, env)
        if cfg.kill_leftovers:
            self.kill_leftovers()

        if self.pool.stopping:
            # The owner stopped the loop: the claim goes back without a failed attempt,
            # so the next start picks the issue up at once instead of after CLAIM_STALE_MIN.
            outcome = "stopped with the supervisor"
        elif timed_out:
            # Before the quota test: a session killed at its deadline (exit 124) whose
            # stderr still held an earlier 429 was counted as a quota error.
            outcome = f"timed out after {cfg.timeouts[u.role]}"
        elif rc != 0 and self.ended_on_quota():
            # Not the agent's failure: the claim goes back without a failed attempt, and
            # the pool holds new starts for a while (Pool.finished).
            outcome = f"quota error (exit {rc})"
        elif rc != 0 and elapsed < 120:
            outcome = f"startup crash (exit {rc})"
        elif rc != 0:
            outcome = f"failed (exit {rc})"
        else:
            outcome = "ok"

        if u.role == "worker":
            self.save_wip(scratch(f"wrk-{self.seq}-*-wt"))
            args = [
                sys.executable,
                str(HERE / "claim.py"),
                "finish",
                "--claim-file",
                str(claim_file),
                "--loop",
                str(self.seq),
            ]
            if outcome.startswith(NOT_A_FAILURE):
                args.append("--no-fail")
            if outcome != "ok":
                args += ["--reason", f"the worker session {outcome}"]
            self.sh(args, dict(env, CC_ROLE="worker"), "finish")
            try:
                self.left_pr = bool(re.search(r"#\d+: pr #\d+", (self.dir / "finish.out").read_text(errors="replace")))
            except OSError:
                pass
            self.cleanup(scratch(f"wrk-{self.seq}-*-wt"))
        elif u.role == "merger":
            self.cleanup(scratch(f"mrg-{u.number}-wt"))
        return outcome

    # --- units without an agent ---
    def gh(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return run_gh(self.pool.cfg, args, stdin, self.pool.log)

    def issue_thread(self, n: int) -> dict:
        """{"body": ..., "comments": [{"a": author, "b": body}, ...]} or {} if gh fails."""
        r = self.gh("issue", "view", str(n), "--json", "body,comments", "-q",
                    "{body: .body, comments: [.comments[] | {a: .author.login, b: .body}]}")
        try:
            out = json.loads(r.stdout or "{}")
        except ValueError:
            return {}
        return out if isinstance(out, dict) else {}

    def note(self, line: str):
        with open(self.dir / "auto.log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")

    def auto_merge(self, env: dict) -> str:
        """Land a MERGEABLE pull request: review gate, merge gates, squash merge, labels.

        A refusal by either gate hands the PR back to a worker (comment, stage:pr ->
        stage:triage); a merge that GitHub refuses (main moved under it) is left for the
        next poll, which sees it CONFLICTING and starts a merger session."""
        u = self.unit
        pr, n, head = u.number, u.info["issue"], u.info["head"]
        env = dict(env, CC_ROLE="merger", MERGE_PR=str(pr))
        r = subprocess.run(
            [*shlex.split(self.pool.cfg.review_cmd), "--pr", str(pr)],
            env=env, cwd=repo_root(), capture_output=True, text=True, timeout=900,
        )
        self.note(f"review gate exit {r.returncode}: {(r.stdout + r.stderr).strip()[-600:]}")
        if r.returncode == 1 and review_record(head).get("verdict") == "owner":
            self.pool.event(f"PR #{pr} (#{n}) waits for the owner after the review gate: {review_reason(head)}")
            return "waits for the owner (review gate)"
        if r.returncode == 1:
            self.pool.log("handback", pr=pr, issue=n, by="review", reason=review_reason(head))
            if not review_cut(head):
                self.pool.failed_try(n)  # a reject of a diff it read whole is the issue's own
            return "rejected by the review gate (handed back)"
        if r.returncode != 0:
            return f"held (the review gate exited {r.returncode})"  # an error: reviewed again later
        command = f"gh pr merge {pr} --squash --delete-branch --match-head-commit {head}"

        def gates() -> dict:
            """The guard's verdict: {"decision": "allow"} or a block/deny with its reason.
            Anything else (a crash, a timeout, an empty or broken reply, an unknown
            decision) is {"decision": "hold", ...}: no merge and no hand-back. That used to
            read as {} and the merge went ahead with no gate checked."""
            try:
                g = subprocess.run(
                    shlex.split(self.pool.cfg.guard_cmd),
                    input=json.dumps({"tool_input": {"command": command}, "session_id": "ccpool"}),
                    env=dict(env, GUARD_EXPLICIT_ALLOW="1"), cwd=repo_root(), capture_output=True,
                    text=True, timeout=300,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.note(f"merge gates did not run: {exc!r}")
                return {"decision": "hold", "reason": f"the guard did not run ({exc!r:.200})"}
            self.note(f"merge gates: exit {g.returncode} {g.stdout.strip()[-800:]} {g.stderr.strip()[-300:]}")
            if g.returncode != 0:
                return {"decision": "hold", "reason": f"the guard exited {g.returncode}"}
            try:
                verdict = json.loads(g.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                verdict = None
            if not isinstance(verdict, dict) or verdict.get("decision") not in ("allow", "block", "deny"):
                return {"decision": "hold", "reason": f"the guard gave no verdict ({g.stdout.strip()[-200:]!r})"}
            return verdict

        branch = u.info.get("branch") or ""
        held = self.red_main_hold(pr, head, branch) or self.starve_hold(pr, head, branch)
        if held:
            return held  # before the sweep: a held PR costs none
        swept = None  # this unit's re-sweep of the head: None (none yet), True or False
        recheck = ""  # the earlier head whose live check gate 7 wants replayed on this one
        want_sweep = False  # the gates asked for a sweep
        for attempt in range(3):
            # A missing or stale sweep record is mechanical: sweep the head here before
            # anything is handed back (most merge-gate hand-backs were a missing sweep
            # line or record). Outside the merge lock: a merger's clean re-rebase comes
            # without a sweep, and re-sweeping it (5-10 minutes) under the lock held every
            # other merge, whose PRs then conflicted again and got mergers of their own.
            if swept is None and (want_sweep or (attempt == 0 and self.needs_sweep(pr, head))):
                # The same head on the same main, held after a red re-sweep: sweeping it again
                # says the same. PR #157 was re-swept 15 times at one head in cohort 52.
                before = self.pool.book.swept_held.get(pr)
                if before and before[0] == u.key:
                    self.note(f"not re-swept: {before[1]} at this head and main already")
                    return before[1]
                swept, out = self.resweep(pr, branch, head, env)
                held = "" if swept else self.red_like_main(pr, out)
                if held:
                    return self.swept_hold(pr, held)
                if swept is False and "== sweep is red ==" in out:
                    # Once more before acting on it: single flaky failures handed PRs back.
                    # The failing tests alone first, when the project says how ([sweep] rerun):
                    # failing again, they are the PR's; passing, the whole sweep runs again
                    # for the record Gate 2 reads.
                    rerun, _ = self.pool.rerun_at(head, failing_tests(out), fetch=branch or "main")
                    if rerun is False:
                        return self.swept_hold(pr, self.sweep_hand_back(pr, n, head, out))
                    if rerun:
                        self.pool.report_flaky(f"PR #{pr}'s head {head[:8]}", failing_tests(out))
                    swept, again = self.resweep(pr, branch, head, env)
                    if swept:
                        self.note(f"the re-sweep passed on a second try (flaky: {', '.join(failing_tests(out)[:3])})")
                        if rerun is None:
                            self.pool.report_flaky(f"PR #{pr}'s head {head[:8]}", failing_tests(out))
                    elif not same_failure(out, again):
                        return self.swept_hold(pr, self.flaky_hold(pr, "re-sweep", out, again))
                    else:
                        return self.swept_hold(pr, self.sweep_hand_back(pr, n, head, again))
            if recheck:
                # Gate 7 refuses a head whose change moved since its live check, and a rebase
                # that resolves a conflict moves it; each such refusal cost a worker session.
                # Replay the recorded check on this head here, outside the lock, as the sweep.
                passed, out = self.devcheck_rerun(pr, branch, head, recheck, env)
                if passed is None:
                    self.pool.event(f"auto-merge of PR #{pr} held: its live check could not be replayed "
                                    f"({out.strip().splitlines()[-1][:200] if out.strip() else 'no output'})")
                    return "held (the live check could not be replayed)"
                if not passed:
                    return self.devcheck_hand_back(pr, n, head, recheck, out)
                recheck = "done"
            with self.pool.merge_lock:
                # A head swept on an older main says nothing about the merged result: one PR
                # moved an import, another (swept before it) still used it, GitHub saw no
                # conflict, and main stopped starting. A rebase and full sweep per merge would
                # serialize the merges (most were behind main), so a quick check runs on the
                # head merged with the current main instead. It runs under the lock, right
                # before the merge: a check made before the review went stale when other
                # merges landed meanwhile, and main broke three times in one run that way.
                # The merge train's full sweep of the merged tree replaces the quick check.
                if self.pool.cfg.fast_check_cmd and not self.pool.cfg.merge_train:
                    held = self.merged_check(pr, n, head, branch, env)
                    if held:
                        return held
                held = self.red_main_hold(pr, head, branch)  # main may have turned red meanwhile
                if held:
                    return held
                verdict = gates()
                moved = GATE7_MOVED.search(str(verdict.get("reason") or "")) \
                    if verdict.get("decision") in ("block", "deny") else None
                if moved and not recheck:
                    recheck = moved.group(1)
                    continue  # leave the lock, replay the live check on this head, ask again
                if not moved and swept is None and verdict.get("decision") in ("block", "deny") and re.search(
                    r"(?i)sweep", str(verdict.get("reason") or "")
                ):
                    want_sweep = True
                    continue  # the gates want a sweep after all: leave the lock, sweep, ask again
                if self.pool.cfg.merge_train and verdict.get("decision") == "allow":
                    return self.pool.enqueue(u)
                outcome = self.land(pr, n, head, verdict)
                if outcome:
                    return outcome
                break
        self.pool.after_merge(pr, n, head, bool(u.info.get("needs_live")), "the review gate and the merge gates passed")
        return "ok"

    def flaky_hold(self, pr: int, what: str, first: str, second: str) -> str:
        """Two red runs that failed differently: flaky, not the PR's fault. Held for a later try."""
        names = sorted(set(failing_tests(first)) | set(failing_tests(second)))[:4]
        self.pool.event(f"PR #{pr} not handed back: its {what} failed twice, differently "
                        f"({', '.join(names) or 'no test named'}): flaky; tried again on a later poll")
        return f"held (the {what} failed twice, differently: flaky)"

    def infra_hold(self, pr: int, what: str, out: str) -> str:
        """'' unless the check failed on the machine (INFRA): then the PR is held."""
        m = INFRA.search(out)
        if not m:
            return ""
        self.pool.event(f"PR #{pr} not handed back: its {what} failed on the machine ({m.group(0)}); "
                        "tried again on a later poll")
        return f"held (the {what} failed on the machine: {m.group(0)})"

    def known_flaky_hold(self, pr: int, what: str, out: str) -> str:
        """'' unless every test that failed has an open `Flaky test:` issue: then the PR
        is held for a later try, not handed back (PR #107 was handed back for one)."""
        failing = failing_tests(out)
        known = self.pool.flaky_tests()
        if not failing or not all(t in known for t in failing):
            return ""
        issues = ", ".join(f"#{n}" for n in sorted({known[t] for t in failing}))
        self.pool.event(f"PR #{pr} not handed back: its {what} failed only known flaky tests "
                        f"({', '.join(failing[:2])}; {issues}); tried again on a later poll")
        return f"held (the {what} failed only known flaky tests: {issues})"

    def swept_hold(self, pr: int, outcome: str) -> str:
        """Remember a hold that followed a red re-sweep, for this unit's key (head and
        main), so it is not swept again until one of them moves. Not a failure of the
        machine: that one may be gone by the next try."""
        if outcome.startswith("held") and "on the machine" not in outcome:
            with self.pool.lock:
                self.pool.book.swept_held[pr] = (self.unit.key, outcome)
        return outcome

    def sweep_hand_back(self, pr: int, n: int, head: str, out: str) -> str:
        """Hand the PR back for its red sweep, with what failed. Handed back by the gates
        instead, the comment said "does not match Sweep SHA" or "missing the green line",
        and the worker had to find the failing tests itself (PRs #110, #126, #140). When
        every failing test is one that failed on a red main and the head is behind main,
        the failure is main's, inherited: the supervisor rebases the PR instead."""
        held = self.infra_hold(pr, "re-sweep", out) or self.known_flaky_hold(pr, "re-sweep", out)
        if held:
            return held
        failing = failing_tests(out)
        branch = self.unit.info.get("branch") or ""
        if failing and all(t in self.pool.red_failed for t in failing) and self.behind_main(head, branch):
            with self.pool.lock:
                self.pool.book.rebase_wanted[pr] = head
            self.pool.event(f"PR #{pr} not handed back: it fails only where main failed before "
                            f"({', '.join(failing[:2])}), and main has moved on; the supervisor rebases it")
            return "held (its failure is main's: asked for a rebase onto main)"
        failing = failing_tests(out)
        self.hand_back(pr, n, (
            f"**Not merged: the sweep at this head ({head[:8]}) is red.** The supervisor ran the full "
            "check suite on it before the merge, and these fail (main does not fail them):\n\n"
            + ("\n".join(f"- `{t}`" for t in failing[:20]) or "- (no test named; see the output)")
            + "\n\nThe end of the output:\n\n```\n" + out.strip()[-2500:].replace("```", "'" * 3) + "\n```\n\n"
            "Fix it in your worktree, then sweep and push."))
        self.pool.log("handback", pr=pr, issue=n, by="sweep", reason=(failing or ["the sweep is red"])[0][:200])
        self.pool.failed_try(n)
        return "sweep red at the head (handed back)"

    def red_like_main(self, pr: int, out: str) -> str:
        """'' unless the re-sweep's failures are main's own: the latest main check is red
        and fails every test (or reason) this sweep failed. Then the PR is held, not handed
        back; a later poll sweeps it again. PR #159 was handed back for the two tests that
        had just turned main red, as the merged-tree check already avoids (main_ok)."""
        failing = failing_tests(out)
        last = self.pool.main_check_last
        if not failing or last.get("ok") is not False or not set(failing) <= set(last.get("failing") or []):
            return ""
        self.pool.event(f"PR #{pr} not handed back: its sweep fails only where main {str(last.get('sha'))[:8]} "
                        f"fails too ({', '.join(failing[:3])}); tried again on a later poll")
        return f"held (main {str(last.get('sha'))[:8]} is red with the same failures)"

    def land(self, pr: int, n: int, head: str, verdict: dict) -> str:
        """Act on the gates' verdict, under the merge lock: '' once merged, else the
        unit's outcome (held, handed back, or refused by GitHub)."""
        if verdict.get("decision") == "hold":
            self.pool.event(f"auto-merge of PR #{pr} held: {verdict['reason']}; tried again on a later poll")
            return f"held ({verdict['reason']})"
        if verdict.get("decision") in ("block", "deny"):
            reason = str(verdict.get("reason") or "")
            if "shared checkout" in reason.lower():
                self.pool.event(f"auto-merge of PR #{pr} refused by Gate 0 (shared checkout): {reason[:300]}")
                return "held (gate 0: the shared checkout is not clean)"
            if "try again later" in reason.lower():
                # The guard could not read GitHub or a file at the head: nothing about the
                # PR. Handed back, PR #110 sent its issue to triage over one slow `gh pr view`.
                self.pool.event(f"auto-merge of PR #{pr} held: the merge gates could not read what they "
                                f"check ({reason.splitlines()[0][:200]}); tried again on a later poll")
                return "held (the merge gates could not read GitHub)"
            self.hand_back(pr, n, "**Merge gates: refused.** Not merged.\n\n" + reason[:2500])
            gate = re.search(r"(?i)\bgate\s*(\d)", reason)
            self.pool.log("handback", pr=pr, issue=n, by=f"gate {gate.group(1)}" if gate else "gates",
                          reason=reason.strip().splitlines()[0][:200] if reason.strip() else "")
            return "refused by the merge gates (handed back)"
        m = self.gh("pr", "merge", str(pr), "--squash", "--delete-branch", "--match-head-commit", head)
        self.note(f"gh pr merge exit {m.returncode}: {(m.stdout + m.stderr).strip()[-400:]}")
        if m.returncode != 0:
            return f"merge refused by GitHub (exit {m.returncode})"
        return ""

    def fast_check(self, wt: str, env: dict) -> tuple[bool, str]:
        """POOL_FAST_CHECK_CMD in a worktree: (passed, the tail of its output)."""
        cfg = self.pool.cfg
        try:
            r = self.pool.run_owned(["bash", "-c", cfg.fast_check_cmd], cfg.fast_check_s, cwd=wt, env=env,
                                    stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, f"{FAST_TIMEOUT} {cfg.fast_check_s:.0f} s"
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-1500:]

    def merged_check(self, pr: int, n: int, head: str, branch: str, env: dict) -> str:
        """'' when the PR may go on to the gates: its head contains the current main, or
        the head merged with the current main passes fast_check. Otherwise the unit's
        outcome: handed back when the merge broke something. When main itself fails the
        check, nothing is blamed and the gates decide."""
        if not self.behind_main(head, branch):
            return ""
        root = str(repo_root())
        main = self.git(root, "rev-parse", "origin/main").stdout.strip()
        wt = scratch(f"fresh-{pr}")
        remove_worktree(root, wt, self.pool.log)
        try:
            if self.git(root, "worktree", "add", "-q", "--detach", wt, head).returncode != 0:
                return ""  # cannot tell; the gates decide
            m = self.git(wt, "-c", "user.name=ccpool", "-c", "user.email=ccpool@localhost",
                         "merge", "-q", "--no-edit", main)
            if m.returncode != 0:
                self.git(wt, "merge", "--abort")
                return "behind main with a conflict (the next poll sees it)"
            ok, why = self.fast_check(wt, env)
            self.note(f"merged-tree check on main {main[:12]}: {'ok' if ok else why[-400:]}")
            if not ok and why.startswith(FAST_TIMEOUT):
                # Under the merge lock: a timeout and its retry held every merge for ten
                # minutes (PR #108). Not retried now; held the first time on this head,
                # handed back the second (a head that hangs the check).
                with self.pool.lock:
                    first = (pr, head) not in self.pool.fast_timeouts
                    self.pool.fast_timeouts.add((pr, head))
                if first:
                    self.pool.event(f"PR #{pr}: the merged-tree check timed out; held, tried again on a later poll")
                    return "held (the merged-tree check timed out)"
            elif not ok:  # once more: a single flaky failure handed PRs #106 and #103 back
                ok, again = self.pool.rerun_tests(wt, failing_tests(why), env)  # the failing tests alone
                if ok is None:
                    ok, again = self.fast_check(wt, env)
                self.note(f"merged-tree check, second try: {'ok (flaky)' if ok else again[-400:]}")
                if ok:
                    self.pool.report_flaky(f"PR #{pr}'s head {head[:8]} merged with main {main[:8]}",
                                           failing_tests(why))
                if not ok and not same_failure(why, again):
                    return self.flaky_hold(pr, "merged-tree check", why, again)
                why = again if not ok else why
            if ok:
                return ""
            held = (self.infra_hold(pr, "merged-tree check", why)
                    or self.known_flaky_hold(pr, "merged-tree check", why))
            if held:
                return held
            if not self.main_ok(main, env):
                # Main fails too, so the check cannot blame this PR; holding every merge
                # would also hold the fix.
                self.note(f"main {main[:12]} fails the fast check itself; not blaming PR #{pr}")
                return ""
            self.hand_back(pr, n, (
                "**Not merged: it breaks main once merged.** The supervisor merged this head with "
                f"the current origin/main ({main[:8]}) and the result fails a quick check that "
                "main alone passes:\n\n```\n" + why[-1500:] + "\n```\n\n"
                "Rebase onto origin/main in your worktree, fix it, sweep again and push."))
            self.pool.log("handback", pr=pr, issue=n, by="merged-tree check", reason=why.strip().splitlines()[-1][:200]
                          if why.strip() else "")
            self.pool.failed_try(n)
            return "breaks main once merged (handed back)"
        finally:
            remove_worktree(root, wt, self.pool.log)

    def main_ok(self, main: str, env: dict) -> bool:
        """fast_check on main itself, once per sha."""
        cache = self.pool.main_checks
        if main not in cache:
            root, wt = str(repo_root()), scratch(f"fresh-main-{main[:12]}")
            remove_worktree(root, wt, self.pool.log)
            try:
                if self.git(root, "worktree", "add", "-q", "--detach", wt, main).returncode != 0:
                    return True
                cache[main] = self.fast_check(wt, env)[0]
            finally:
                remove_worktree(root, wt, self.pool.log)
        return cache[main]

    def starve_hold(self, pr: int, head: str, branch: str) -> str:
        """Pool.starve_hold for the files this head changes since it forked off main."""
        if not self.pool.starving:
            return ""
        self.behind_main(head, branch)  # fetches main and the branch
        root = str(repo_root())
        fork = self.git(root, "merge-base", "origin/main", head).stdout.strip()
        files = set(self.git(root, "diff", "--name-only", fork, head).stdout.split()) if fork else set()
        held = self.pool.starve_hold(pr, files)
        if held:
            self.note(held)
        return held

    def red_main_hold(self, pr: int, head: str, branch: str) -> str:
        """'' unless main's latest full check is red and this head is not built on that main.
        In run 7 main was red for 25 minutes and four PRs merged meanwhile; one added a
        second failure, which the merged-tree check could not blame because main failed
        too. While main is red, a PR built on it merges, since its own green sweep (Gate 2)
        then shows the result is not red: the fix itself, rebased onto main, merges, so the
        hold cannot outlast it. A head git cannot place is not held, as in behind_main."""
        last = self.pool.main_check_last
        red = str(last.get("sha") or "") if last.get("ok") is False else ""
        if not red or not self.pool.cfg.red_hold or self.contains(head, red, branch) is not False:
            return ""
        with self.pool.lock:
            first = (pr, red) not in self.pool.red_held
            self.pool.red_held.add((pr, red))
        if first:
            self.pool.event(f"PR #{pr} waits: main {red[:8]} is red, and until it is green only a PR built on it "
                            "(rebased onto it, its sweep green) merges")
        return f"held (main {red[:8]} is red; the PR is not built on it)"

    def contains(self, head: str, sha: str, branch: str) -> bool | None:
        """Whether sha is in head's history (None: git cannot tell)."""
        self.behind_main(head, branch)  # fetches main and the branch
        r = self.git(str(repo_root()), "merge-base", "--is-ancestor", sha, head).returncode
        return True if r == 0 else False if r == 1 else None

    def behind_main(self, head: str, branch: str) -> bool:
        """Whether origin/main has commits the PR head lacks. Unknown counts as not behind."""
        root = str(repo_root())
        for attempt in range(3):  # other units fetch into the same .git (ref lock)
            f = self.git(root, "fetch", "-q", "origin", "main", *([branch] if branch else []))
            if f.returncode == 0:
                break
            time.sleep(2 + 2 * attempt)
        return self.git(root, "merge-base", "--is-ancestor", "origin/main", head).returncode == 1

    def devcheck_rerun(self, pr: int, branch: str, head: str, earlier: str, env: dict) -> tuple[bool | None, str]:
        """`devcheck.py --rerun <earlier> --update-pr <pr>` in a worktree at head: (True
        passed and the PR body names head, False the check failed, None it could not run;
        its output)."""
        if not branch:
            return None, "no branch"
        root, wt = str(repo_root()), scratch(f"mrg-{pr}-devcheck")
        remove_worktree(root, wt, self.pool.log)
        try:
            self.git(root, "fetch", "-q", "origin", branch)
            if self.git(root, "worktree", "add", "-q", "--detach", wt, head).returncode != 0:
                return None, f"could not add a worktree at {head[:12]}"
            r = self.pool.run_owned(
                [sys.executable, str(HERE / "devcheck.py"), "--rerun", earlier, "--update-pr", str(pr)], 3600,
                cwd=wt, env=env, stdin=subprocess.DEVNULL,
            )
            out = r.stdout + r.stderr
            self.note(f"live check replay of {earlier[:12]} on {head[:12]}: exit {r.returncode}: {out.strip()[-400:]}")
            # devcheck.py: 0 a recorded pass, 1 a failed check, 2 or 3 it did not run.
            return (True if r.returncode == 0 else False if r.returncode == 1 else None), out
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, str(exc)
        finally:
            remove_worktree(root, wt, self.pool.log)

    def devcheck_hand_back(self, pr: int, n: int, head: str, earlier: str, out: str) -> str:
        """The replayed live check failed on this head: back to a worker, with what failed."""
        held = self.infra_hold(pr, "live check replay", out)
        if held:
            return held
        self.hand_back(pr, n, (
            f"**Not merged: the live check fails on this head ({head[:8]}).** A rebase changed what the "
            f"branch does since the live check at {earlier[:8]}, so the supervisor replayed its scripts on "
            "this head (`devcheck.py --rerun`), and they fail:\n\n```\n" + out.strip()[-2500:].replace("```", "'" * 3)
            + "\n```\n\nFix it in your worktree, check it again with devcheck.py, and push."))
        self.pool.log("handback", pr=pr, issue=n, by="live check replay", reason=f"the replay of {earlier[:12]} failed")
        self.pool.failed_try(n)
        return "live check fails on the rebased head (handed back)"

    def needs_sweep(self, pr: int, head: str) -> bool:
        """Whether the merge gates would ask for a sweep of this head (Gate 2): the PR body
        names another head, or there is no green sweep record for it and no waiver. A body
        that cannot be read says nothing; the gates decide."""
        r = self.gh("pr", "view", str(pr), "--json", "body", "-q", ".body")
        if r.returncode != 0:
            return False
        m = SWEEP_SHA.search(r.stdout)
        if not m or not head.lower().startswith(m.group(1).lower()):
            return True
        if SWEEP_WAIVED.search(r.stdout):
            return False
        return not any((d / f"{head}.json").exists() for d in config.record_dirs("sweep"))

    def resweep(self, pr: int, branch: str, head: str, env: dict) -> tuple[bool, str]:
        """Run the sweep at the PR's head in a worktree and update the PR body
        (sweep_locked.py --update-pr): (ended green at that head, its output)."""
        if not branch:
            return False, ""
        root, wt = str(repo_root()), scratch(f"mrg-{pr}-sweep")
        remove_worktree(root, wt, self.pool.log)
        try:
            self.git(root, "fetch", "-q", "origin", branch)
            if self.git(root, "worktree", "add", "-q", "--detach", wt, head).returncode != 0:
                return False, ""
            r = self.pool.run_owned(
                [sys.executable, str(HERE / "sweep_locked.py"), "--update-pr", str(pr)], 2400, cwd=wt,
                env=dict(env, SWEEP_CALLER="resweep"),
            )
            self.note(f"resweep exit {r.returncode}: {(r.stdout + r.stderr).strip()[-400:]}")
            return r.returncode == 0 and "green" in r.stdout, r.stdout + r.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        finally:
            remove_worktree(root, wt, self.pool.log)

    def git(self, wt: str, *args: str, check: bool = False, **kw) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True, timeout=300, **kw)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[-300:]}")
        return r

    def auto_rebase(self, env: dict) -> str:
        """Rebase a CONFLICTING pull request onto main when every conflict hunk only adds
        lines on both sides (two workers appending rules, functions or blocks at the same
        place): keep both, sweep, push. The next poll then sees it MERGEABLE and merges it
        behind the gates (the review gate reads the new diff). Any other conflict, or a red
        sweep, leaves the PR to a merger session. Conflicted files are recorded for
        split_hot_files. (Limited to test files it landed nothing, because per-issue test
        files had already removed those conflicts; the conflicts left were mostly two
        blocks appended to the same stylesheet or page.)"""
        u = self.unit
        pr, branch = u.number, u.info.get("branch") or ""
        if not branch:
            return "skipped (no branch)"
        root = str(repo_root())
        wt = scratch(f"mrg-{pr}-auto")
        remove_worktree(root, wt, self.pool.log)
        try:
            for attempt in range(3):  # other units fetch into the same .git (ref lock)
                f = self.git(root, "fetch", "-q", "origin", "main", branch)
                if f.returncode == 0:
                    break
                time.sleep(3 + 3 * attempt)
            else:
                raise RuntimeError(f"git fetch: {(f.stderr or f.stdout).strip()[-200:]}")
            self.git(root, "worktree", "add", "-q", "-B", f"auto-rebase-{pr}", wt, f"origin/{branch}", check=True)
            for k, v in (("user.name", "ccpool"), ("user.email", "ccpool@localhost")):
                self.git(wt, "config", k, v)
            gitenv = dict(os.environ, GIT_EDITOR="true")
            diff3 = ("-c", "merge.conflictStyle=diff3")
            r = self.git(wt, *diff3, "rebase", "origin/main", env=gitenv)
            files_seen: set[str] = set()
            rounds = 0
            while r.returncode != 0:
                rounds += 1
                conflicted = self.git(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()
                if not conflicted or rounds > 20:
                    self.git(wt, "rebase", "--abort")
                    return f"rebase stopped ({(r.stderr or r.stdout).strip()[-120:]})"
                files_seen.update(conflicted)
                bad = [f for f in conflicted if not self.additive_conflict(wt, f)]
                if bad:
                    self.git(wt, "rebase", "--abort")
                    self.pool.record_conflicts(pr, files_seen, hard=True)
                    self.note(f"left to a merger: conflicts in {', '.join(bad)}")
                    return "left to a merger (a hunk changes existing lines)"
                for f in conflicted:
                    self.git(wt, "add", f, check=True)
                r = self.git(wt, *diff3, "rebase", "--continue", env=gitenv)
            self.pool.record_conflicts(pr, files_seen)
            sweep = self.pool.run_owned(
                [sys.executable, str(HERE / "sweep_locked.py"), "--update-pr", str(pr)], 2400, cwd=wt,
                env=dict(env, SWEEP_CALLER="auto-rebase"),
            )
            self.note(f"sweep exit {sweep.returncode}: {(sweep.stdout + sweep.stderr).strip()[-600:]}")
            if sweep.returncode != 0 or "green" not in sweep.stdout:
                return "left to a merger (sweep red after the rebase)"
            push = self.git(wt, "push", "-q", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}")
            if push.returncode != 0:
                return f"push refused ({push.stderr.strip()[-120:]})"
            self.note(f"rebased onto main, kept both sides in {', '.join(sorted(files_seen)) or 'nothing'}")
            return "ok"
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            return f"failed ({str(exc)[:160]})"
        finally:
            remove_worktree(root, wt, self.pool.log)
            subprocess.run(["git", "-C", root, "branch", "-D", f"auto-rebase-{pr}"], capture_output=True)

    def additive_conflict(self, wt: str, path: str) -> bool:
        """A conflict whose hunks only add lines on both sides: resolve it by keeping both
        sides (main's, then the PR's) and check that a Python result still parses (the
        sweep checks the rest). Writes the resolution; False leaves the file untouched."""
        if not path.endswith((".py", ".js", ".mjs", ".css", ".html", ".md", ".txt", ".toml")):
            return False
        try:
            text = (pathlib.Path(wt) / path).read_text()
        except OSError:
            return False
        out, i, lines = [], 0, text.splitlines(keepends=True)
        while i < len(lines):
            if not lines[i].startswith("<<<<<<< "):
                out.append(lines[i])
                i += 1
                continue
            ours, base, theirs, part = [], None, [], "ours"
            i += 1
            while i < len(lines) and not lines[i].startswith(">>>>>>> "):
                ln = lines[i]
                if ln.startswith("||||||| "):
                    part, base = "base", []
                elif ln.startswith("======="):
                    part = "theirs"
                elif part == "ours":
                    ours.append(ln)
                elif part == "base":
                    base.append(ln)
                else:
                    theirs.append(ln)
                i += 1
            i += 1
            if base is None or any(x.strip() for x in base):
                return False  # no base section, or one side changed existing lines
            out += ours + (["\n"] if ours and theirs and ours[-1].strip() else []) + theirs
        merged = "".join(out)
        if path.endswith(".py"):
            try:
                ast.parse(merged)
            except SyntaxError:
                return False
        (pathlib.Path(wt) / path).write_text(merged)
        return True

    def hand_back(self, pr: int, n: int, text: str):
        self.pool.hand_back(pr, n, self.unit.info.get("head") or "", text)

    def merger_handback(self, env: dict) -> str:
        """A PR that conflicted again after POOL_MERGER_MAX merger sessions: back to a worker."""
        u = self.unit
        pr, n, k = u.number, u.info["issue"], u.info.get("mergers", 0)
        self.hand_back(pr, n, (
            f"**Not merged: it keeps conflicting with main.** {k} merger sessions resolved this "
            "pull request's conflicts, and each time main moved on before the merge and it "
            "conflicted again. Rebase it onto the current origin/main in your worktree and finish it."))
        self.pool.log("handback", pr=pr, issue=n, by="merger loop", reason=f"{k} merger sessions without a merge")
        return "handed back after repeated conflicts"

    def requeue(self, env: dict) -> str:
        """An issue at stage:pr with no open pull request: back to stage:triage, so the
        next worker opens a new one (the branch keeps what was done)."""
        n = self.unit.number
        # The plan read a snapshot: the supervisor may have merged the PR seconds before
        # (its label edit follows the merge). Look again, on GitHub, before moving it.
        issue = self.gh("issue", "view", str(n), "--json", "state,labels")
        prs = self.gh("pr", "list", "--state", "all", "--head", f"issue-{n}", "--json", "state")
        try:
            labels = {x["name"] for x in json.loads(issue.stdout)["labels"]}
            open_ = json.loads(issue.stdout)["state"] == "OPEN"
            states = {x["state"] for x in json.loads(prs.stdout or "[]")}
        except (ValueError, KeyError, TypeError):
            return "skipped (could not read the issue or its PRs)"
        if not open_ or "stage:pr" not in labels:
            return "skipped (no longer at stage:pr)"
        if states & {"OPEN", "MERGED"}:
            return f"skipped (its PR is {'open' if 'OPEN' in states else 'merged'})"
        self.gh("issue", "edit", str(n), "--remove-label", "stage:pr", "--add-label", "stage:triage")
        self.gh("issue", "comment", str(n), "--body",
                f"<!-- merger -->\nBack to stage:triage: #{n} was at stage:pr with no open pull request "
                f"for over {self.pool.cfg.orphan_pr_min} minutes (it was closed without a merge). The next "
                f"worker opens a new one; the branch `issue-{n}` keeps what was done.")
        self.pool.log("requeue", issue=n)
        return "ok"

    def reclaim(self, env: dict) -> str:
        """Put stale stage:doing claims (no PR, no running worker) back in the queue."""
        rc = self.sh(
            [sys.executable, str(HERE / "claim.py"), "stale", "--release"],
            dict(env, CC_ROLE="worker"), "reclaim",
        )
        return "ok" if rc == 0 else f"failed (exit {rc})"

    # --- unfinished work survives the session ---
    def wip_branch(self, n: int) -> str:
        """'<sha> <subject>' when origin has branch issue-<n> (an earlier worker's
        unfinished work), else ''."""
        root = str(repo_root())
        f = subprocess.run(["git", "-C", root, "fetch", "-q", "origin", f"issue-{n}"],
                           capture_output=True, text=True, timeout=120)
        if f.returncode != 0:
            return ""
        log = subprocess.run(["git", "-C", root, "log", "-1", "--format=%h %s", "FETCH_HEAD"],
                             capture_output=True, text=True)
        return log.stdout.strip()

    def save_wip(self, pattern: str):
        """A worker that ends without a pull request: commit what its worktree holds and
        push it to issue-<n>, so the next worker on the issue starts from it."""
        for wt in glob.glob(pattern):
            m = re.search(r"wrk-\d+-(\d+)-wt$", wt)
            if not m:
                continue
            n = m.group(1)
            if self.gh("pr", "list", "--head", f"issue-{n}", "--json", "number", "-q", ".[].number").stdout.strip():
                continue  # it has a PR: the branch is already where the PR says
            git = ["git", "-C", wt]
            subprocess.run(git + ["add", "-A", "--", ".", ":!.claude", ":!.agents", ":!.ux"],
                           capture_output=True)
            subprocess.run(git + ["-c", "user.name=ccpool", "-c", "user.email=ccpool@localhost",
                                  "commit", "-q", "-m", f"WIP #{n}: unfinished worker session"],
                           capture_output=True)
            ahead = subprocess.run(git + ["rev-list", "--count", "origin/main..HEAD"],
                                   capture_output=True, text=True).stdout.strip()
            if ahead and ahead != "0":
                r = subprocess.run(git + ["push", "-q", "-f", "origin", f"HEAD:refs/heads/issue-{n}"],
                                   capture_output=True, text=True, timeout=120)
                self.pool.log("wip_saved", session=self.seq, issue=n, ok=r.returncode == 0)

    def cleanup(self, pattern: str):
        """Remove the session's worktrees, rescuing unpushed work to <wt>-rescued.diff beside them."""
        root = str(repo_root())
        for wt in glob.glob(pattern):
            if rescue_worktree(wt):
                self.pool.log("rescued", session=self.seq, path=wt)
            if (
                subprocess.run(
                    ["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True
                ).returncode
                != 0
            ):
                subprocess.run(["rm", "-rf", wt])
        subprocess.run(["git", "-C", root, "worktree", "prune"], capture_output=True)


class Pool:
    def __init__(self, cfg: Config, fetcher=None, spawn=True):
        self.cfg = cfg
        self.fetcher = fetcher or (lambda: fetch(cfg))
        self.spawn_workers = spawn
        self.book = Book()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.sessions: dict[str, Session] = {}
        self.seq = 0
        self.ports: set[int] = set()
        self.events: list[str] = []
        self.flagged: set[str] = set()
        self.prev: dict | None = None
        self.idle_flagged = False
        self.last_fetch = 0.0
        self.hurry_pr: int | None = None  # a merger just ended on it: poll fast while it reads UNKNOWN
        self.hurry_until = 0.0
        self.last_sent = 0.0
        self._herdr = None  # herdr_link.Monitor, made on first use
        self.pending_since = 0.0  # when the oldest unsent monitor update was queued
        self.last_queue: dict = {}  # the queue counts last logged
        self.monitor_state = ""  # the monitor's last state seen by feed()
        self.crashes: collections.Counter = collections.Counter()
        self.deploying = False
        self.merge_lock = threading.Lock()
        self.deploy_tried: set[str] = set()
        self.main_checks: dict[str, bool] = {}  # main sha -> its fast check passed
        self.main_check_sha = ""  # the newest main a full check started on
        self.main_check_running = False
        self.main_check_thread: threading.Thread | None = None
        self.owned: set[subprocess.Popen] = set()  # the supervisor's own long commands (run_owned)
        self.main_check_last: dict = {}  # {sha, ok, t, failing}: the last full check that finished
        self.train_running = False  # the merge train runs (it shares main_check_running: one main lane)
        self.train_landing = False  # it is merging its batch now (deploy_check waits)
        self.train_until = 0.0  # no train before this after one that was held
        self.train_red_main: tuple = ()  # (main, heads) of a train that failed only where main fails
        self.guard_sha = ""  # the last build the acceptance guard ran on (guard_due)
        self.main_green = ""  # the last main whose full check was green
        self.red_issue: int | None = None  # the open `main is red` issue
        self.red_looked = False  # whether GitHub was asked for one since this supervisor started
        self.red_failing: tuple = ()  # the failing tests last reported on it
        self.red_held: set = set()  # (pr, red main) pairs the monitor was told about
        self.disk_low = False  # the scratch root's file system was last seen nearly full
        self.starving: dict[int, dict] = {}  # PR -> {"files", "since", "rank"}: see check_starving
        self.fast_timeouts: set = set()  # (pr, head) whose merged-tree check timed out once
        self.flaky_known: tuple[float, dict[str, int]] = (0.0, {})  # (read at, test -> its open Flaky issue)
        self.flaky_rows: list[dict] = []  # the open `Flaky test:` issues as last read (flaky_deadlines)
        self.red_failed: dict[str, str] = {}  # test -> the red main it failed on (sweep_hand_back)
        self.own_deployed = ""
        self.ux_scores: list[dict] = []  # {t, sha, score} per UX run, score None when its report had none
        self.undeployed_since = 0.0  # when main first moved past the last deploy started
        self.stopping = False
        self.halt = threading.Event()  # set with stopping, for sessions that wait
        self.clock = time.time
        cfg.logdir.mkdir(parents=True, exist_ok=True)
        config.scratch_root().mkdir(parents=True, exist_ok=True)
        seqf = cfg.logdir / "seq"
        try:
            self.seq = int(seqf.read_text())
        except (OSError, ValueError):
            self.seq = 0
        self.load_deploys()
        self.load_ux_scores()

    # --- bookkeeping ---
    def log(self, kind: str, **kv):
        rec = {"t": round(time.time(), 1), "kind": kind, **kv}
        with open(self.cfg.logdir / "pool.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        detail = " ".join(f"{k}={v}" for k, v in kv.items())
        print(f"{time.strftime('%H:%M:%S')}  {kind} {detail}", flush=True)

    def event(self, text: str):
        with self.lock:
            self.events.append(text)
        self.log("event", text=text)

    def record_conflicts(self, pr: int, files, hard: bool = False) -> None:
        """hard: the rebase was left to a merger (a hunk changes existing lines)."""
        now = self.clock()
        with self.lock:
            self.book.conflicts += [(now, f, pr) for f in files]
            if hard:
                self.book.hard_conflicts.setdefault(pr, []).append((now, frozenset(files)))
                self.check_starving(pr, now)
        self.split_hot_files(now)

    def check_starving(self, pr: int, now: float) -> None:
        """A P0/P1 PR whose rebase was left to a merger POOL_STARVE_AFTER times on the same
        files is starving: every merge that touches them moves main under it. In cohort
        52, #143 ("near here", P1) waited 5 h 40 min through 3 merger sessions while 77
        commits landed. Until it lands, is closed, or POOL_STARVE_HOLD_S passes, merges of
        other PRs that touch those files wait (starve_hold)."""
        if pr in self.starving or not self.cfg.starve_after:
            return
        counts = collections.Counter(f for _, files in self.book.hard_conflicts.get(pr, []) for f in files)
        hot = {f for f, k in counts.items() if k >= self.cfg.starve_after}
        st = self.prev or {}
        p = next((x for x in st.get("prs", []) if x["number"] == pr), None)
        issue = next((i for i in st.get("open", []) if p and i["number"] == pr_issue(p)), None)
        prio = priority(issue["labels"]) if issue else 9
        if not hot or prio > 1:
            return
        first = min(t for t, _ in self.book.hard_conflicts[pr])
        self.starving[pr] = {"files": hot, "since": now, "rank": (prio, first, pr)}
        self.log("starving", pr=pr, files=sorted(hot), priority=f"P{prio}")
        self.event(f"PR #{pr} (P{prio}) conflicted {self.cfg.starve_after} times on {', '.join(sorted(hot)[:4])}: "
                   "merges of other PRs that touch those files wait until it lands, is closed, or "
                   f"{self.cfg.starve_hold_s / 60:.0f} minutes pass")

    def track_starving(self, st: dict, now: float) -> None:
        """End a starving PR's hold when it is no longer an open PR, or at the time limit."""
        open_prs = {p["number"] for p in st.get("prs", [])}
        for pr, s in list(self.starving.items()):
            why = ("it is no longer open (merged or closed)" if pr not in open_prs else
                   f"{self.cfg.starve_hold_s / 60:.0f} minutes passed" if now - s["since"] > self.cfg.starve_hold_s
                   else "")
            if why:
                del self.starving[pr]
                self.book.hard_conflicts.pop(pr, None)
                self.log("starving_end", pr=pr, why=why)
                self.event(f"merges that touch PR #{pr}'s files no longer wait for it: {why}")

    def starve_hold(self, pr: int, files: set) -> str:
        """'' unless the PR touches the files of a starving PR that is not itself and does
        not rank below it (P0 before P1, then the one that started conflicting first):
        two starving PRs on the same files never wait for each other."""
        with self.lock:
            mine = self.starving.get(pr)
            for other, s in sorted(self.starving.items(), key=lambda kv: kv[1]["rank"]):
                if other == pr or not files & s["files"] or (mine and mine["rank"] < s["rank"]):
                    continue
                return f"held (it touches {', '.join(sorted(files & s['files'])[:3])}, which starving PR #{other} needs)"
        return ""

    def record_large(self, files: dict) -> None:
        """Source files on main over POOL_SPLIT_LINES lines (path -> lines), seen after a merge."""
        with self.lock:
            self.book.large.update(files)
        self.split_hot_files(self.clock())

    def split_hot_files(self, now: float) -> list[str]:
        """File an issue to split a source file into smaller files with no behavior change,
        when it conflicted in POOL_SPLIT_AFTER different pull requests within
        POOL_SPLIT_WINDOW_S (parallel workers keep colliding there), or when it has grown
        past POOL_SPLIT_LINES lines. At most POOL_SPLIT_PER_WINDOW per window and
        POOL_SPLIT_MAX per run, conflicts first (a cap of 3 per run was spent in 90
        minutes, and the file that kept conflicting until the end was never split).
        Test files are left out: workers write per-issue test files."""
        cfg, b = self.cfg, self.book
        with self.lock:
            prs: dict[str, set[int]] = collections.defaultdict(set)
            for t, f, pr in b.conflicts:
                if now - t <= cfg.split_window_s and not is_test(f):
                    prs[f].add(pr)
            hot = [f for f, s in sorted(prs.items(), key=lambda x: -len(x[1]))
                   if len(s) >= cfg.split_after and f not in b.split_filed]
            big = [f for f, n in sorted(b.large.items(), key=lambda x: -x[1])
                   if n > cfg.split_lines and f not in b.split_filed and f not in hot
                   and not is_test(f)]
            recent = sum(1 for t in b.split_times if now - t <= cfg.split_window_s)
            room = min(cfg.split_per_window - recent, cfg.split_max - len(b.split_filed))
            due = (hot + big)[: max(0, room)]
            b.split_filed.update(due)
            b.split_times += [now] * len(due)
        for f in due:
            why = (
                f"conflicted in {len(prs[f])} pull requests in the last {cfg.split_window_s / 60:.0f} "
                f"minutes ({', '.join(f'#{n}' for n in sorted(prs[f]))}): parallel workers keep "
                "editing the same file, and every conflict costs a rebase or a merger session"
                if f in prs and len(prs[f]) >= cfg.split_after
                else f"has {b.large.get(f)} lines on main, and long files are where parallel work collides"
            )
            body = (
                "<!-- merger -->\n<!-- split -->\n"
                f"`{f}` {why}.\n\n"
                f"Split `{f}` into smaller files along its existing seams (one module per "
                "component or concern; long functions into named helpers), with no behavior "
                "change. Keep every public name and every hook the tests use working, and keep "
                "the sweep green. Move code as it is, changing only imports and the lines that join "
                "the pieces: the review gate summarizes blocks moved unchanged, while a block "
                "rewritten on the way counts as new code it must read whole. Add unit tests for the "
                "extracted pieces in a new test file and record them as Fail-before (a new file "
                "counts from zero). Do nothing else in this PR.\n\n"
                "This is checked from the repository, not from the app: in the PR body list "
                "every new file with its line count, the old file's line count before and "
                "after, and the output of `git diff --stat origin/main...HEAD`."
            )
            r = self.gh("issue", "create", "--title", f"Split {f}: it is where parallel work collides",
                        "--label", "P1", "--label", "stage:triage", "--body", body)
            # Filed only with the new issue's URL: one split logged ok had none (the secondary
            # rate limit) and was never filed. A failed one is due again on a later look.
            url = re.search(r"https://\S+/issues/\d+", r.stdout)
            self.log("split_filed", file=f, prs=len(prs.get(f, ())), lines=b.large.get(f), ok=bool(url))
            if url:
                self.event(f"filed a split issue for {f}: {url.group(0)}")
                continue
            with self.lock:
                b.split_filed.discard(f)
                b.split_times.remove(now)
            self.event(f"could not file a split issue for {f} (tried again later): "
                       f"{(r.stderr or r.stdout).strip()[-200:] or f'gh exited {r.returncode}'}")
        return [f for f in due if f in b.split_filed]

    def take_port(self) -> int:
        with self.lock:
            k = 0
            while self.cfg.tst_port + k * self.cfg.tst_port_step in self.ports:
                k += 1
            port = self.cfg.tst_port + k * self.cfg.tst_port_step
            self.ports.add(port)
            return port

    def start(self, u: Unit):
        with self.lock:
            if self.stopping:
                return None  # a plan made just before stop() starts nothing
            self.seq += 1
            (self.cfg.logdir / "seq").write_text(str(self.seq))
            b = self.book
            b.running[u.family] = u
            b.tries[u.key] += 1
            b.family_starts[u.family] += 1
            if u.kind == "ux":
                b.last_ux_t = self.clock()
            if u.kind == "spec":
                b.last_spec_t = self.clock()
            if u.info.get("idle") or u.kind == "spec":
                b.extra_runs += 1
            w = Session(self, u, self.seq)
            self.sessions[u.family] = w
        self.log("start", session=self.seq, role=u.role, unit=u.key)
        if self.spawn_workers:
            w.start()
        return w

    def quota_seen(self, role: str) -> None:
        """A session hit a model quota error: hold new agent sessions, for a time that
        doubles with each pause in a row (reset when a session ends ok)."""
        now = self.clock()
        with self.lock:
            b = self.book
            if b.quota_until > now:
                return
            b.quota_streak += 1
            wait = quota_backoff(b.quota_streak, self.cfg.quota_wait_s, self.cfg.quota_wait_max_s)
            b.quota_until = now + wait
            self.events.append(
                f"model quota errors ({role} session): no new agent sessions for {wait:.0f} s "
                f"(pause {b.quota_streak} in a row); running sessions wait and resume"
            )
        self.log("quota_pause", role=role, seconds=wait, streak=b.quota_streak)

    def finished(self, w: Session, outcome: str):
        u, now = w.unit, self.clock()
        with self.lock:
            b = self.book
            b.running.pop(u.family, None)
            self.sessions.pop(u.family, None)
            self.ports.discard(w.port)
            if outcome == "ok":
                b.clean.add(u.key)
                if u.kind == "live":
                    b.clean.add(u.info["base"])  # this entry into stage:live is checked
            if outcome == "ok" and u.role != "auto":
                b.quota_streak = 0
            if outcome == "ok":
                # Only failures count toward POOL_FAMILY_MAX: a merged PR that a live check
                # or a reopen sends back is progress, not a loop.
                b.family_starts[u.family] -= 1
            if outcome.startswith("quota error"):
                # The model's capacity, not the unit: no try spent, so it is due again
                # when the pause ends.
                b.tries[u.key] -= 1
                b.family_starts[u.family] -= 1
            if outcome.startswith("held"):
                # Waiting on something outside the PR (a red main, the guard, the review
                # gate): no try spent, and due again after POOL_HOLD_RETRY_S. Counted as
                # failures, red-main holds took PRs #82, #88 and #89 to POOL_FAMILY_MAX,
                # and they stayed stopped after main was green again.
                b.tries[u.key] -= 1
                b.family_starts[u.family] -= 1
                b.held_until[u.key] = now + self.cfg.hold_retry_s
            if outcome == "unclaimable" and u.number is not None:
                # claim.py's own read refused it (a native dependency, attempts): no
                # try spent; the memo holds it back until the issue or main changes, or
                # POOL_UNCLAIMABLE_RETRY_S passes. Without the expiry, issues refused
                # because GitHub's read lagged right after they were filed waited 36
                # minutes for main to move, with slots free.
                b.tries[u.key] -= 1
                b.family_starts[u.family] -= 1
                updated = u.key.split("@", 1)[1] if "@" in u.key else ""
                b.unclaimable[u.number] = (updated, (self.prev or {}).get("main", ""), now)
            if u.kind == "rebase" and outcome.startswith(("left to a merger", "rebase stopped")):
                b.rebase_failed[u.number] = u.info.get("head") or ""
            if u.role == "merger" and not outcome.startswith(NOT_A_FAILURE):
                b.merger_runs[u.number] += 1
                # The resolved head merges on the next poll; GitHub may still be computing
                # its mergeability, so read again soon rather than after POOL_POLL_S.
                self.hurry_pr, self.hurry_until = u.number, now + 60
            if (u.kind == "merge" and outcome == "ok") or (u.kind == "handback" and outcome.startswith("handed back")):
                b.merger_runs.pop(u.number, None)
            if u.kind == "handback" and outcome.startswith("handed back"):
                b.reset_pr(u.number)
            if outcome.startswith("startup crash"):
                self.crashes[u.role] += 1
                if self.crashes[u.role] >= self.cfg.crash_max:
                    b.paused[u.role] = now + self.cfg.crash_pause_s
                    self.crashes[u.role] = 0
                    self.events.append(
                        f"{self.cfg.crash_max} {u.role} sessions in a row died at startup (see "
                        f"{w.dir}/session.err); no new {u.role} starts for {self.cfg.crash_pause_s:.0f} s"
                    )
            elif outcome != "unclaimable" and not outcome.startswith("quota error"):
                self.crashes[u.role] = 0
            # Only the issue's own failures count toward asking for a split: a session that
            # never got the claim, or ended on quota, a crash or a stop, says nothing about
            # the issue, and one that left a pull request delivered. Every one of seven
            # split requests in run 7 counted those (and gate 7 or merger-loop hand-backs).
            if u.role == "worker" and u.number is not None and (w.dir / "claim").exists() and not w.left_pr \
                    and not outcome.startswith(NOT_A_FAILURE):
                b.failed_tries[u.number] += 1
            # A hold has its own event, once, and is retried every POOL_HOLD_RETRY_S.
            if outcome not in ("ok", "unclaimable") and not outcome.startswith(("quota error", "held")):
                self.events.append(
                    f"{u.role} session {w.seq} on {u.kind} {self.label(u)} {outcome} (log {w.dir})"
                )
        if outcome.startswith("quota error"):
            self.quota_seen(u.role)
        if u.role == "tester" and u.kind == "live" and outcome == "ok":
            self.settle_live(u)
        if u.role == "tester" and u.kind == "accept" and (outcome == "ok" or outcome.startswith(("timed out", "failed"))):
            verified = self.settle_accept(u, w.started, outcome == "ok")  # one that timed out may have settled some
            self.accept_chore(w.dir, verified)
        if u.kind == "ux":
            self.record_ux_score(w)
        extra = {}
        if (w.dir / "session.jsonl").exists():
            extra["usage"] = w.cli.usage(w.events())
            extra["cli"] = w.cli.name
        self.log(
            "end",
            session=w.seq,
            role=u.role,
            unit_kind=u.kind,
            number=u.number,
            unit=u.key,
            outcome=outcome,
            seconds=round(time.time() - w.started),
            **extra,
        )
        self.wake.set()

    def record_ux_score(self, w: Session):
        """The score from the UX run's report, for pool.jsonl and the dashboard. 7 of 10
        reports in one run had none, so a run's score could not be followed."""
        score = ux_score(w.dir / "ux_report.md")
        rec = {"t": round(self.clock(), 1), "sha": str(w.unit.info.get("sha", ""))[:12], "score": score}
        with self.lock:
            self.ux_scores = (self.ux_scores + [rec])[-20:]
        if score is None:
            self.log("ux_score", session=w.seq, sha=rec["sha"], score=None,
                     missing="no `Score: N/100` line in ux_report.md")
        else:
            self.log("ux_score", session=w.seq, sha=rec["sha"], score=score)

    def load_ux_scores(self):
        """The scores this run already logged, so a restarted supervisor keeps them."""
        try:
            lines = (self.cfg.logdir / "pool.jsonl").read_text(errors="replace").splitlines()
        except OSError:
            return
        for line in lines:
            if '"ux_score"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "ux_score":
                self.ux_scores.append({"t": rec.get("t", 0), "sha": rec.get("sha", ""), "score": rec.get("score")})
        self.ux_scores = self.ux_scores[-20:]

    @staticmethod
    def label(u: Unit) -> str:
        if u.role == "merger" or u.kind == "merge":
            return f"PR #{u.number}"
        return f"#{u.number}" if u.number is not None else u.key

    # --- deploys ---
    def load_deploys(self):
        """What this run already deployed or tried (deploys.jsonl), so a restarted
        supervisor does not deploy the same sha again."""
        try:
            lines = (self.cfg.logdir / "deploys.jsonl").read_text().splitlines()
        except OSError:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("sha"):
                self.deploy_tried.add(rec["sha"])
                if rec.get("ok"):
                    self.own_deployed = rec["sha"]

    def deployments(self) -> tuple[str, set[str]]:
        """(last good sha, shas tried)."""
        return self.own_deployed, set(self.deploy_tried)

    def ux_target(self, st: dict) -> str:
        """The sha a UX run tests: the last good deploy, or main's head when this
        repository does not deploy."""
        return self.own_deployed if self.cfg.deploy_cmd else st.get("main", "")

    def deploy_check(self, st: dict, now: float):
        main = st["main"]
        if not main or self.deploying or not self.cfg.deploy_cmd:
            return
        good, tried = self.deployments()
        if main == good or main in tried:
            return
        # The grace counts from the first undeployed main, not from each new one: while
        # mergers kept running and main kept moving, the deploy waited 24 minutes.
        self.undeployed_since = self.undeployed_since or now
        mergers = self.book.role_counts()["merger"] or self.train_landing
        if mergers and now - self.undeployed_since < self.cfg.deploy_grace_s:
            return
        self.undeployed_since = 0.0
        self.deploying = True
        self.deploy_tried.add(main)
        threading.Thread(target=self.deploy, args=(main,), daemon=True).start()

    def deploy(self, sha: str):
        self.log("deploy", sha=sha[:12])
        root = repo_root()
        subprocess.run(["git", "-C", str(root), "fetch", "-q", "origin", "main"],
                       capture_output=True, timeout=300)
        # A deploy command that fetches shares the .git that worktrees and other units
        # fetch into; a concurrent fetch fails it with "cannot lock ref", and production
        # stays a merge behind until main moves again. Try again after a pause.
        for attempt in range(3):
            try:
                r = subprocess.run(
                    ["bash", "-c", self.cfg.deploy_cmd], cwd=root, env=dict(os.environ, DEPLOY_SHA=sha),
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=self.cfg.deploy_timeout_s,
                )
                ok, out = r.returncode == 0, (r.stdout + r.stderr)[-1500:]
            except (OSError, subprocess.TimeoutExpired) as exc:
                ok, out = False, str(exc)
            if ok or "cannot lock ref" not in out:
                break
            time.sleep(DEPLOY_LOCK_PAUSE_S * (attempt + 1))
        with open(self.cfg.logdir / "deploy.log", "a") as f:
            f.write(f"--- {time.strftime('%H:%M:%S')} {sha[:12]} ok={ok}\n{out}\n")
        with open(self.cfg.logdir / "deploys.jsonl", "a") as f:
            f.write(json.dumps({"t": round(time.time(), 1), "sha": sha, "ok": ok}) + "\n")
        with self.lock:
            self.deploying = False
            if ok:
                self.own_deployed = sha
        self.log("deployed" if ok else "deploy_failed", sha=sha[:12])
        if not ok:
            # Not tried again for this sha: until main moves, production stays on the
            # last good deploy, and main may be broken.
            self.event(
                f"deploy of {sha[:12]} FAILED, so production stays on "
                f"{(self.own_deployed or 'nothing deployed yet')[:12]} (see {self.cfg.logdir}/deploy.log): "
                f"{out.strip().splitlines()[-1] if out.strip() else ''}"
            )
        self.wake.set()

    # --- main's own check ---
    def gh(self, *args: str) -> subprocess.CompletedProcess:
        return run_gh(self.cfg, args, log=self.log)

    def main_check_due(self, st: dict):
        """Start the full check of the newest origin/main, unless one is running. The
        merged-tree check runs only the quick command, so a semantic conflict in the
        slower tests reached main twice in one run and nobody noticed for half an hour."""
        main = st.get("main", "")
        with self.lock:
            if not self.cfg.main_check or not main or self.main_check_running or main == self.main_check_sha:
                return
            self.main_check_running, self.main_check_sha = True, main
        self.main_check_thread = threading.Thread(target=self.check_main, args=(main,), daemon=True)
        self.main_check_thread.start()

    def check_main(self, sha: str):
        try:
            ok, out = self.sweep_main(sha)
            if ok is False:
                ok, out = self.second_look(sha, out)
            self.main_checked(sha, ok, out)
            self.flaky_deadlines()
            self.consolidation_chore(sha)
        except Exception as exc:  # noqa: BLE001 - a failed check is not a failed supervisor
            self.log("main_check_error", sha=sha[:12], error=str(exc)[:300])
        finally:
            with self.lock:
                self.main_check_running = False
            self.wake.set()  # main may have moved meanwhile

    def sweep_main(self, sha: str) -> tuple[bool | None, str]:
        """The sweep at sha in a worktree: (True green, False red, None when it could not
        run; its output)."""
        root = str(repo_root())
        wt = scratch(f"main-{sha[:12]}")

        def git(*a):
            return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=300)
        remove_worktree(root, wt, self.log)
        try:
            for attempt in range(3):  # other units fetch into the same .git (ref lock)
                if git("fetch", "-q", "origin", "main").returncode == 0:
                    break
                time.sleep(2 + 2 * attempt)
            if git("worktree", "add", "-q", "--detach", wt, sha).returncode != 0:
                return None, f"could not add a worktree at {sha[:12]}"
            return self.sweep_tree(wt, "main-check")
        finally:
            remove_worktree(root, wt, self.log)

    def sweep_tree(self, wt: str, caller: str) -> tuple[bool | None, str]:
        """The full sweep in a worktree that exists, in the main lane: (True green, False
        red, None when it could not run; its output). In its own lane: the main check ran
        after every merge, and in a sweep slot it held one of the sessions' slots for over
        half the run."""
        try:
            r = self.run_owned([sys.executable, str(HERE / "sweep_locked.py"), "--lane", "main", "--full"],
                               self.cfg.main_check_s, cwd=wt,
                               env=dict(os.environ, SWEEP_CALLER=caller), stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, str(exc)
        out = r.stdout + r.stderr
        if r.returncode == 0 and "green" in r.stdout:
            return True, out
        # 1 is a red sweep; 2 and 3 mean it did not run (no runner, no slot).
        return (False if r.returncode == 1 else None), out

    def commits_since(self, old: str, new: str) -> list[str]:
        root = str(repo_root())
        args = ["log", "--oneline", "--no-decorate"] + ([f"{old}..{new}"] if old else ["-10", new])
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=60)
        return r.stdout.splitlines()[:30] if r.returncode == 0 else []

    def find_red_issue(self) -> int | None:
        r = self.gh("issue", "list", "--state", "open", "--search", "main is red in:title",
                    "--json", "number,title")
        try:
            found = [i["number"] for i in json.loads(r.stdout or "[]") if i["title"].startswith("main is red")]
        except (ValueError, KeyError, TypeError):
            found = []
        return min(found) if found else None

    def main_checked(self, sha: str, ok: bool | None, out: str):
        failing = failing_tests(out) if ok is False else []
        with self.lock:
            self.main_check_last = {"sha": sha, "ok": ok, "t": round(time.time(), 1), "failing": failing[:50]}
        self.log("main_check", sha=sha[:12], ok=ok, failing=failing[:5])
        if ok is None:
            return  # it could not run; the next main gets a check
        if not self.red_looked:  # one left open by an earlier run
            self.red_issue, self.red_looked = self.find_red_issue(), True
        if ok:
            self.main_green = sha
            n, self.red_issue, self.red_failing = self.red_issue, None, ()
            if n:
                self.gh("issue", "comment", str(n), "--body",
                        f"<!-- merger -->\nmain is green again at {sha[:8]}: the full check suite passes there.")
                self.gh("issue", "close", str(n))
                self.event(f"main is green again at {sha[:8]}; closed #{n}")
            return
        for t in failing:
            self.red_failed[t] = sha
        self.report_red(sha, failing, out)

    def flaky_tests(self, max_age: float = 600) -> dict[str, int]:
        """The tests the supervisor recorded as flaky (a body or comment of an open `Flaky
        test:` issue that carries FLAKY_MARK, so they failed and passed at one commit),
        each with its issue; read again after max_age seconds. An issue filed under the
        old rule (red, then green at a later commit) names a main break as often as a
        flake (#194 in cohort 52), and does not count."""
        at, known = self.flaky_known
        if time.time() - at < max_age:
            return known
        r = self.gh("issue", "list", "--state", "open", "--search", "Flaky test in:title",
                    "--json", "number,title,body,comments,createdAt,labels")
        try:
            rows = [i for i in json.loads(r.stdout or "[]") if str(i.get("title", "")).startswith("Flaky test")]
        except (ValueError, TypeError):
            return known  # keep what was known; read again next time
        self.flaky_rows = rows
        known = {}
        for i in rows:
            for text in [str(i.get("body") or "")] + [str(c.get("body") or "") for c in i.get("comments") or []]:
                if FLAKY_MARK not in text:
                    continue
                for t in re.findall(r"`([^`\s]+::[^`]+)`", text):
                    known.setdefault(t, i["number"])
        self.flaky_known = (time.time(), known)
        return known

    def rerun_tests(self, wt: str, tests: list[str], env: dict | None = None) -> tuple[bool | None, str]:
        """[sweep] rerun with just the failing tests, in a worktree that exists: (passed,
        output), or (None, "") when there is no such command or no test to name, and the
        caller runs the whole check again (in cohort 52 a red main check's second full run
        took 0.7 h a cohort, and each held PR's second full re-sweep as long again)."""
        tests = [t for t in tests if "::" in t]
        if not self.cfg.rerun_cmd or not tests or "{tests}" not in self.cfg.rerun_cmd:
            return None, ""
        cmd = self.cfg.rerun_cmd.replace("{tests}", " ".join(shlex.quote(t) for t in tests))
        try:
            r = self.run_owned(["bash", "-c", cmd], 900, cwd=wt, env=env or dict(os.environ),
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, str(exc)
        return r.returncode == 0, (r.stdout + r.stderr)[-6000:]

    def rerun_at(self, ref: str, tests: list[str], fetch: str = "main") -> tuple[bool | None, str]:
        """rerun_tests in a fresh worktree at ref (None: not run, or git could not)."""
        if not self.cfg.rerun_cmd or not [t for t in tests if "::" in t]:
            return None, ""
        root, wt = str(repo_root()), scratch(f"fresh-rerun-{ref[:12]}")
        remove_worktree(root, wt, self.log)
        try:
            subprocess.run(["git", "-C", root, "fetch", "-q", "origin", fetch], capture_output=True, timeout=300)
            if subprocess.run(["git", "-C", root, "worktree", "add", "-q", "--detach", wt, ref],
                              capture_output=True).returncode != 0:
                return None, ""
            return self.rerun_tests(wt, tests)
        finally:
            remove_worktree(root, wt, self.log)

    def flaky_deadlines(self) -> None:
        """A test recorded as flaky (FLAKY_MARK) must be fixed or deleted within
        POOL_FLAKY_MERGES merges. Past that, its issue gets one comment saying so
        (FLAKY_DEADLINE, which also keeps it from being said twice) and P1 if it had a
        lower priority. In cohort 52 test_issue_96 failed 29 runs, and fails 1 in 3 even
        alone, and nobody fixed it."""
        if not self.cfg.flaky_merges:
            return
        self.flaky_tests()
        for i in self.flaky_rows:
            texts = [(i.get("createdAt") or "", str(i.get("body") or ""))] + [
                (c.get("createdAt") or "", str(c.get("body") or "")) for c in i.get("comments") or []]
            since: dict[str, str] = {}
            noted: set[str] = set()
            for at, text in texts:
                names = re.findall(r"`([^`\s]+::[^`]+)`", text)
                if FLAKY_DEADLINE in text:
                    noted.update(names)
                elif FLAKY_MARK in text:
                    for t in names:
                        since.setdefault(t, at)
            due = []
            for t, at in since.items():
                if t in noted or not at:
                    continue
                merges = self.merges_since(at)
                if merges >= self.cfg.flaky_merges:
                    due.append((t, merges))
            if not due:
                continue
            n = i["number"]
            self.gh("issue", "comment", str(n), "--body", (
                f"<!-- merger -->\n{FLAKY_DEADLINE}\nThese tests were recorded as flaky and are still flaky after "
                f"{self.cfg.flaky_merges} or more merges:\n\n" + "\n".join(f"- `{t}` ({k} merges)" for t, k in due)
                + "\n\nFix each one so it passes reliably, or delete it if another test already covers what it "
                "checks. Every failure costs a sweep and a second try, and a known-flaky test no longer turns main red, "
                "so it hides real failures too."))
            labels = {str(x.get("name") if isinstance(x, dict) else x) for x in i.get("labels") or []}
            if not labels & {"P0", "P1"}:
                self.gh("issue", "edit", str(n), "--add-label", "P1",
                        *[a for p in sorted(labels & {"P2", "P3"}) for a in ("--remove-label", p)])
            self.log("flaky_deadline", issue=n, tests=[t for t, _ in due][:5])
            self.event(f"flaky test(s) {', '.join(t for t, _ in due[:2])} past their deadline of "
                       f"{self.cfg.flaky_merges} merges: #{n} says so, at P1")
            self.flaky_known = (0.0, self.flaky_known[1])  # its comments changed

    def consolidation_chore(self, sha: str) -> None:
        """File one chore when main holds POOL_CONSOLIDATE_AT (40) per-issue test files
        (POOL_CONSOLIDATE_GLOB): merge or parametrize them by flow. Not while one is open.
        On cohort 52's final main, 104 of 251 e2e tests were a coverage subset of another,
        most of them one new test_issue_<N>.py per issue for a flow that already had one."""
        if not self.cfg.consolidate_at:
            return
        r = subprocess.run(["git", "-C", str(repo_root()), "ls-tree", "-r", "--name-only", sha],
                           capture_output=True, text=True, timeout=60)
        listed = r.stdout.split() if r.returncode == 0 else []
        files = [f for f in listed if fnmatch.fnmatch(f, self.cfg.consolidate_glob)]
        if len(files) < self.cfg.consolidate_at:
            return
        o = self.gh("issue", "list", "--state", "open", "--search", "Consolidate per-issue tests in:title",
                    "--json", "number,title")
        try:
            if o.returncode != 0 or any(str(i.get("title", "")).startswith("Consolidate per-issue tests")
                                        for i in json.loads(o.stdout or "[]")):
                return
        except ValueError:
            return
        body = (
            f"<!-- merger -->\nmain at {sha[:8]} has {len(files)} files matching `{self.cfg.consolidate_glob}`, one per "
            "issue. Many test a flow that another file already tests, and every sweep runs them all.\n\n"
            "Merge or parametrize them by flow, without losing what they check:\n"
            "- **delete** a test whose steps and assertions another test already covers (a strict subset), and any "
            "test that checks no behavior (a lint or file-layout check, a source-text grep, a page that only loads);\n"
            "- **merge** two tests that start from the same fixture and take the same steps into one;\n"
            "- **parametrize** copies of one test across viewports, languages or settings into one test;\n"
            "- **replace** a structural check (a grep of JS or HTML source) with a test that drives the behavior.\n\n"
            "Keep every assertion that is not covered elsewhere. Do it in a few PRs, one flow each, with the sweep "
            "green after each; give the merged tests flow names (`test_settings.py`, `test_reload.py`)."
        )
        title = f"Consolidate per-issue tests: {len(files)} {self.cfg.consolidate_glob} files"
        c = self.gh("issue", "create", "--title", title, "--label", "P2", "--label", "stage:triage", "--body", body)
        m = re.search(r"/issues/(\d+)", c.stdout)
        self.log("consolidate_filed", files=len(files), ok=bool(m))
        if m:
            self.event(f"filed #{m.group(1)}: consolidate {len(files)} per-issue test files by flow")

    def merges_since(self, at: str) -> int:
        """Commits on origin/main since an ISO time: the merges (each lands as one squash commit)."""
        r = subprocess.run(["git", "-C", str(repo_root()), "rev-list", "--count", f"--since={at}", "origin/main"],
                           capture_output=True, text=True, timeout=60)
        return int(r.stdout.strip() or 0) if r.returncode == 0 else 0

    def second_look(self, sha: str, out: str) -> tuple[bool | None, str]:
        """A red main check, looked at again before it holds merges. When it fails only
        tests with an open `Flaky test:` issue, main counts as green and that issue gets
        a comment; otherwise the check runs once more, and a pass then makes the failures
        flaky. Tests the supervisor had filed as flaky turned main red again twice in run
        8, holding merges and handing PR #107 back. A failure of the machine (no space
        left, say) means the check could not run: None, and no `main is red` issue."""
        if INFRA.search(out):
            self.event(f"main {sha[:8]}: the check failed on the machine, not the code "
                       f"({INFRA.search(out).group(0)}); main counts as unchecked")
            return None, out
        failing = failing_tests(out)
        known = self.flaky_tests()
        if failing and all(t in known for t in failing):
            for n in sorted({known[t] for t in failing}):
                self.gh("issue", "comment", str(n), "--body", (
                    f"<!-- merger -->\nThese known flaky tests failed again on main at {sha[:8]}:\n\n"
                    + "\n".join(f"- `{t}`" for t in failing if known[t] == n)
                    + "\n\nNothing else failed, so main counts as green for merges. Each such failure "
                    "still costs a full check run: make them reliable."))
            self.log("main_check_flaky", sha=sha[:12], tests=failing[:5])
            self.event(f"main {sha[:8]} failed only known flaky tests ({', '.join(failing[:2])}); "
                       "counted as green, and their issue has a comment")
            return True, out
        again, out2 = self.rerun_at(sha, failing)
        if again is False:
            return False, out  # the failing tests fail again: the full output tells the most
        if again is None:
            again, out2 = self.sweep_main(sha)
        if again:
            self.report_flaky(f"main at {sha[:8]}", failing)
            return True, out2
        return (False, out2) if again is False else (False, out)

    def report_flaky(self, where: str, failing: list[str]):
        """Tests that failed and then passed at one commit (`where`: main at a sha, a PR's
        head, a merged tree) are flaky. File an issue for them (or comment on the open
        one), so someone makes them reliable: an e2e flake turned main red four times in
        one run and nobody filed a fix. Only the same commit proves it: "red, then green
        at a later commit with no change to the test" labeled a main break as flaky (#194
        in cohort 52), and the known-flaky hold then re-swept PRs built on it for an hour."""
        tests = [t for t in failing if "::" in t]
        if not tests:
            return
        names = ", ".join(tests[:2]) + (f" and {len(tests) - 2} more" if len(tests) > 2 else "")
        body = (f"<!-- merger -->\n{FLAKY_MARK}\nThese tests failed on {where} and passed when they ran again "
                "at the same commit, so they fail by chance:\n\n"
                + "\n".join(f"- `{t}`" for t in tests[:20])
                + "\n\nMake them reliable (a fixed wait, a shared port or file, an order dependence, a race with "
                "a server starting). Each flake turns main red, and the merged-tree check and re-sweeps run "
                "twice before they act on a failure.")
        r = self.gh("issue", "list", "--state", "open", "--search", "Flaky test in:title", "--json", "number,title")
        try:
            open_ = [i["number"] for i in json.loads(r.stdout or "[]") if i["title"].startswith("Flaky test")]
        except (ValueError, KeyError, TypeError):
            open_ = []
        if open_:
            self.gh("issue", "comment", str(open_[0]), "--body", body)
            what = f"commented on #{open_[0]}"
        else:
            c = self.gh("issue", "create", "--title", f"Flaky test: {names}"[:200], "--label", "type:bug",
                        "--label", "P1", "--label", "stage:triage", "--body", body)
            m = re.search(r"/issues/(\d+)", c.stdout)
            what = f"filed #{m.group(1)}" if m else "could not file an issue"
        self.log("flaky", tests=tests[:5], where=where)
        self.flaky_known = (0.0, self.flaky_known[1])  # read the issues again next time
        self.event(f"flaky test(s) {names}: failed and passed at one commit ({where}); {what}")

    def report_red(self, sha: str, failing: list[str], out: str):
        """File the `main is red` issue, or update the open one when what fails changed."""
        names = ", ".join(failing[:2]) + (f" and {len(failing) - 2} more" if len(failing) > 2 else "")
        title = f"main is red: {names or 'the check suite fails'}"[:200]
        commits = self.commits_since(self.main_green, sha)
        since = (f"Commits since the last green main ({self.main_green[:8]}):" if self.main_green
                 else "No green main seen since the supervisor started; the newest commits:")
        body = (
            f"<!-- merger -->\nThe full check suite (`{os.environ.get('SWEEP_COMMAND', '')}`) fails on "
            f"origin/main at {sha[:8]}, so every pull request is now checked against a broken main. "
            "Until it is green, only a pull request built on this main (rebased onto it, its sweep green) "
            "merges; fix this first.\n\nFailing:\n"
            + ("\n".join(f"- `{t}`" for t in failing[:20]) or "- (no test named; see the output)")
            + f"\n\n{since}\n" + ("\n".join(f"- {c}" for c in commits) or "- (could not list them)")
            + "\n\nThe end of the output:\n\n```\n" + out.strip()[-3000:].replace("```", "'''")
            + "\n```\n"
        )
        if self.red_issue is None:
            r = self.gh("issue", "create", "--title", title, "--label", "type:bug", "--label", "P0",
                        "--label", "stage:triage", "--body", body)
            m = re.search(r"/issues/(\d+)", r.stdout)
            self.red_issue = int(m.group(1)) if m else None
            what = f"filed #{self.red_issue}" if m else f"could not file an issue ({r.stderr.strip()[-200:]})"
        elif tuple(failing) != self.red_failing:
            self.gh("issue", "comment", str(self.red_issue), "--body", body)
            self.gh("issue", "edit", str(self.red_issue), "--title", title)
            what = f"updated #{self.red_issue}"
        else:
            return  # still red on the same tests: the issue already says so
        self.red_failing = tuple(failing)
        self.event(f"main {sha[:8]} is RED: the full check suite fails ({names or 'no test named'}); {what}. "
                   "Until it is green only a PR built on it merges; the fix is P0")

    # --- merging ---
    def git(self, *args: str, wt: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", wt or str(repo_root()), *args], capture_output=True, text=True,
                              timeout=300)

    def hand_back(self, pr: int, n: int, head: str, text: str) -> None:
        """Send a PR back to a worker: a comment on the PR, its issue to stage:triage. The
        head is remembered, so a poll read before the hand-back does not merge it."""
        if head:
            with self.lock:
                self.book.handed_back[pr] = [head, False]
        self.gh("pr", "comment", str(pr), "--body", f"<!-- merger -->\n{text}\n\n"
                "Fix this on this branch, re-sweep, push and update the PR body; the supervisor "
                "tries the merge again on the new head.")
        self.gh("issue", "edit", str(n), "--remove-label", "stage:pr", "--add-label", "stage:triage")

    def after_merge(self, pr: int, n: int, head: str, needs_live: bool, why: str) -> None:
        """A merged PR's issue: off stage:pr, to stage:accept when it changed [accept] paths
        or needs a live check (POOL_ACCEPT), else to stage:live when it needs a live check;
        a comment saying why it merged, and the long files it left on main."""
        merged = self.merged_pr(pr)
        files = merged["files"]
        accept = self.cfg.accept and (needs_live or any(config.path_matches(f, g) for f in files
                                                         for g in self.cfg.accept_paths))
        stage = ACCEPT_STAGE if accept else "stage:live" if needs_live else ""
        args = ["issue", "edit", str(n), "--remove-label", "stage:pr"]
        if stage:
            args += ["--add-label", stage]
        self.gh(*args)
        if accept:
            with self.lock:
                self.book.accept_merged[n] = {"pr": pr, "sha": merged["sha"], "t": self.clock(),
                                              "live": needs_live or touches_live(files)}
        self.gh(
            "issue", "comment", str(n), "--body",
            (f"<!-- merger -->\nMerged PR #{pr} (head {head[:8]}): {why}, so the supervisor landed it. "
             + ("It stays open at stage:accept until a tester who did not write it checks it on the deployed "
                "build." if accept
                else "It stays open at stage:live for the tester's live check." if needs_live
                else "The supervisor deploys main next." if self.cfg.deploy_cmd else "")).strip(),
        )
        self.note_large_files(pr, files)
        self.log("merged", pr=pr, issue=n, head=head[:12])

    def merged_pr(self, pr: int) -> dict:
        """A PR's changed files and merge commit ({"files": [...], "sha": ""} when unread)."""
        r = self.gh("pr", "view", str(pr), "--json", "files,mergeCommit")
        try:
            data = json.loads(r.stdout or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        return {"files": [str(f.get("path") or "") for f in data.get("files") or [] if isinstance(f, dict)],
                "sha": str((data.get("mergeCommit") or {}).get("oid") or "")}

    def note_large_files(self, pr: int, files: list[str] | None = None) -> None:
        """Measure the source files this PR touched on main; report the long ones. Source
        files are those under REVIEW_PATHS (the production paths, default src/)."""
        if files is None:
            files = self.merged_pr(pr)["files"]
        self.git("fetch", "-q", "origin", "main")
        large = {}
        for f in files:
            if not is_source(f):
                continue
            text = self.git("show", f"origin/main:{f}").stdout
            if text.count("\n") > self.cfg.split_lines:
                large[f] = text.count("\n")
        if large:
            self.record_large(large)

    # --- the merge train ---
    # Each PR's own sweep runs only the tests its change touches (sweep_run.py --affected).
    # A PR that passes the review and the merge gates waits in a queue. The train merges up
    # to POOL_TRAIN_SIZE of them onto main in a scratch worktree and runs the full sweep on
    # that tree once, in the main lane. Green: the PRs merge in that order, each checked to
    # give main the tree the train swept, and that sweep is main's check. Red: the failing
    # tests run again on the same tree (flaky?), then on main alone (main's own failure?),
    # then on the trees in between, halving, to find the first PR that fails them; it goes
    # back to its worker, and the PRs before it merge. In cohort 52 every PR head and every
    # main got a full sweep: 43.6 h of sweeps in 6 h.
    def enqueue(self, u: Unit) -> str:
        """Put a PR that passed the gates in the train's queue (the auto-merge unit's outcome)."""
        e = {"pr": u.number, "issue": u.info["issue"], "head": u.info["head"],
             "branch": u.info.get("branch") or "", "needs_live": bool(u.info.get("needs_live")),
             "queued": self.clock()}
        with self.lock:
            self.book.train[u.number] = e
            self.train_until = 0.0
            size = len(self.book.train)
        self.log("train_queued", pr=u.number, head=e["head"][:12], queue=size)
        self.wake.set()
        return "held (queued for the merge train)"

    def train_due(self, st: dict) -> None:
        """Start the merge train when PRs wait in its queue and the main lane is free: the
        train and the main check share it (main_check_running), one full sweep at a time."""
        with self.lock:
            if (not self.cfg.merge_train or not self.book.train or self.main_check_running or self.stopping
                    or self.clock() < self.train_until):
                return
            self.main_check_running = self.train_running = True
        self.main_check_thread = threading.Thread(target=self.run_train, daemon=True)
        self.main_check_thread.start()

    def run_train(self) -> None:
        try:
            self.train()
        except Exception as exc:  # noqa: BLE001 - a failed train is not a failed supervisor
            self.log("train_error", error=str(exc)[:300])
            self.train_wait()
        finally:
            with self.lock:
                self.main_check_running = self.train_running = False
            self.wake.set()

    def train_wait(self) -> None:
        """No train for POOL_HOLD_RETRY_S (one that was held would hold again at once)."""
        self.train_until = self.clock() + self.cfg.hold_retry_s

    def train_batch(self) -> list[dict]:
        """Up to POOL_TRAIN_SIZE queued PRs: P0 first, then in the order they were queued.
        While main's full check is red, only PRs built on that main (as red_main_hold)."""
        prio = {i["number"]: priority(i["labels"]) for i in (self.prev or {}).get("open", [])}
        with self.lock:
            entries = sorted(self.book.train.values(), key=lambda e: (prio.get(e["issue"], 9), e["queued"], e["pr"]))
        last = self.main_check_last
        red = str(last.get("sha") or "") if last.get("ok") is False and self.cfg.red_hold else ""
        if red:
            entries = [e for e in entries if self.git("merge-base", "--is-ancestor", red, e["head"]).returncode == 0]
        return entries[: max(1, self.cfg.train_size)]

    def train(self) -> None:
        with self.lock:
            branches = sorted({e["branch"] for e in self.book.train.values() if e["branch"]})
        for attempt in range(3):  # other units fetch into the same .git (ref lock)
            if self.git("fetch", "-q", "origin", "main", *branches).returncode == 0:
                break
            time.sleep(2 + 2 * attempt)
        batch = self.train_batch()
        if not batch:
            self.train_wait()
            return
        root = str(repo_root())
        base = self.git("rev-parse", "origin/main").stdout.strip()
        wt = scratch(f"main-train-{base[:12]}")
        remove_worktree(root, wt, self.log)
        try:
            if self.git("worktree", "add", "-q", "--detach", wt, base).returncode != 0:
                self.log("train_error", error=f"could not add a worktree at {base[:12]}")
                self.train_wait()
                return
            cars = []
            for e in batch:
                m = self.git("-c", "user.name=ccpool", "-c", "user.email=ccpool@localhost", "merge", "-q", "--no-ff",
                             "--no-edit", "-m", f"merge train: PR #{e['pr']}", e["head"], wt=wt)
                if m.returncode != 0:
                    # It conflicts with main or with a PR ahead of it: it waits for a later
                    # train, and GitHub calls it CONFLICTING once those are on main.
                    self.git("merge", "--abort", wt=wt)
                    self.log("train_skip", pr=e["pr"], why=(m.stderr or m.stdout).strip()[-200:])
                    continue
                cars.append({**e, "commit": self.git("rev-parse", "HEAD", wt=wt).stdout.strip(),
                             "tree": self.git("rev-parse", "HEAD^{tree}", wt=wt).stdout.strip()})
            if not cars or (base, frozenset(c["head"] for c in cars)) == self.train_red_main:
                # The same PRs on the same red main fail the same way: they wait for a fix
                # to join them (it is P0, so it goes first) or for main to move.
                self.train_wait()
                return
            self.log("train_start", base=base[:12], prs=[c["pr"] for c in cars])
            ok, out = self.sweep_tree(wt, "merge-train")
            if ok is False:
                ok, out = self.train_second_look(wt, base, cars, out)
            if ok is None:
                self.event(f"merge train of {self.train_label(cars)} held: its check could not run "
                           f"({out.strip().splitlines()[-1][:200] if out.strip() else 'no output'}); tried again later")
                self.train_wait()
            elif ok:
                self.train_land(base, cars, out, checked=True)
            else:
                self.train_bisect(wt, base, cars, out)
        finally:
            remove_worktree(root, wt, self.log)

    @staticmethod
    def train_label(cars: list[dict]) -> str:
        return "PR " + ", ".join(f"#{c['pr']}" for c in cars)

    def train_second_look(self, wt: str, base: str, cars: list[dict], out: str) -> tuple[bool | None, str]:
        """A red train, looked at again as second_look does for main: a failure of the
        machine is None; only known flaky tests count as green; otherwise the failing tests
        (or the whole sweep) run again on the same tree, and a pass makes them flaky."""
        if INFRA.search(out):
            return None, out
        failing = failing_tests(out)
        known = self.flaky_tests()
        if failing and all(t in known for t in failing):
            self.event(f"merge train of {self.train_label(cars)} failed only known flaky tests "
                       f"({', '.join(failing[:2])}); counted as green")
            return True, out
        again, out2 = self.rerun_tests(wt, failing)
        if again is False:
            return False, out
        if again is None:
            again, out2 = self.sweep_tree(wt, "merge-train")
        if again:
            self.report_flaky(f"main {base[:8]} with {self.train_label(cars)} merged", failing)
            return True, out2
        return (None, out2) if again is None else (False, out2)

    def train_red_at(self, wt: str, commit: str, failing: list[str]) -> tuple[bool | None, str]:
        """Whether the train's failing tests fail at one of its commits (None: unknown)."""
        if self.git("checkout", "-q", "--detach", commit, wt=wt).returncode != 0:
            return None, f"could not check out {commit[:12]}"
        ok, out = self.rerun_tests(wt, failing)
        if ok is None:
            ok, out = self.sweep_tree(wt, "merge-train")
        if ok is None or (ok is False and INFRA.search(out)):
            return None, out
        return not ok, out

    def train_bisect(self, wt: str, base: str, cars: list[dict], out: str) -> None:
        """A red train: main alone first (its own failure blames no PR), then halve the
        cars to find the first one whose tree fails. It goes back to its worker; the ones
        before it merge (main's check covers them); the ones after it wait for the next."""
        failing = failing_tests(out)
        red, base_out = self.train_red_at(wt, base, failing)
        if red is None:
            self.event(f"merge train of {self.train_label(cars)} held: it failed, and main alone could not be "
                       "checked; tried again later")
            self.train_wait()
            return
        if red:
            self.event(f"merge train of {self.train_label(cars)}: main {base[:8]} fails the same tests by itself; "
                       "no PR is blamed, and until main is green only PRs built on it go")
            with self.lock:
                self.main_check_sha = base
                self.train_red_main = (base, frozenset(c["head"] for c in cars))
            self.main_checked(base, False, base_out)
            self.train_wait()
            return
        lo, hi = 0, len(cars)  # main (no car) passes; all the cars fail
        while hi - lo > 1:
            mid = (lo + hi) // 2
            red, _ = self.train_red_at(wt, cars[mid - 1]["commit"], failing)
            if red is None:
                self.event(f"merge train of {self.train_label(cars)} held: it failed, and the PRs could not be "
                           "told apart; tried again later")
                self.train_wait()
                return
            lo, hi = (lo, mid) if red else (mid, hi)
        self.train_hand_back(cars[hi - 1], cars[:hi - 1], base, failing, out)
        if hi > 1:
            self.train_land(base, cars[:hi - 1], out, checked=False)

    def train_hand_back(self, car: dict, ahead: list[dict], base: str, failing: list[str], out: str) -> None:
        pr, n, head = car["pr"], car["issue"], car["head"]
        with self.lock:
            self.book.train.pop(pr, None)
        after = f", after {self.train_label(ahead)}, which pass without it" if ahead else ""
        self.hand_back(pr, n, head, (
            f"**Not merged: the full check fails with this PR merged.** The supervisor merged this head "
            f"({head[:8]}) onto main {base[:8]}{after}, ran the full check suite on the result, and these fail "
            "there (main alone passes them):\n\n"
            + ("\n".join(f"- `{t}`" for t in failing[:20]) or "- (no test named; see the output)")
            + "\n\nThe end of the output:\n\n```\n" + out.strip()[-2500:].replace("```", "'" * 3) + "\n```\n\n"
            "Your own sweep ran only the tests your change touches; run these too (or the full suite with "
            "`sweep_locked.py --full`). Rebase onto origin/main in your worktree, fix it, sweep and push."))
        self.log("handback", pr=pr, issue=n, by="merge train", reason=(failing or ["the full check is red"])[0][:200])
        self.failed_try(n)
        self.event(f"merge train: PR #{pr} (#{n}) fails the full check merged with main "
                   f"({', '.join(failing[:2]) or 'no test named'}); handed back")

    def fetch_main(self, old: str = "") -> str:
        """origin/main after a fetch; when old is given, fetched again a few times while it
        still reads old (the merge has just been made on GitHub)."""
        sha = ""
        for attempt in range(5):
            self.git("fetch", "-q", "origin", "main")
            sha = self.git("rev-parse", "origin/main").stdout.strip()
            if not old or sha != old:
                break
            time.sleep(1 + attempt)
        return sha

    def train_land(self, base: str, cars: list[dict], out: str, checked: bool) -> None:
        """Merge the cars on GitHub in the train's order, under the merge lock. Not when main
        has moved since the train built its tree. After each merge main must have the tree
        the train made for that point; otherwise the rest wait. checked: the train's full
        sweep was green on the last car's tree, so that main needs no check of its own."""
        merged: list[int] = []
        final = base
        with self.merge_lock:
            self.train_landing = True
            try:
                now_main = self.fetch_main()
                if now_main != base:
                    self.event(f"merge train of {self.train_label(cars)} not merged: main moved from {base[:8]} to "
                               f"{now_main[:8]} while it ran; the next train runs on the new main")
                    return
                for c in cars:
                    args = ("pr", "merge", str(c["pr"]), "--squash", "--delete-branch", "--match-head-commit", c["head"])
                    m = self.gh(*args)
                    if m.returncode != 0:
                        time.sleep(5)  # GitHub may still be computing mergeability after the last merge
                        m = self.gh(*args)
                    if m.returncode != 0:
                        self.event(f"merge train: GitHub refused to merge PR #{c['pr']} (exit {m.returncode}: "
                                   f"{(m.stderr or m.stdout).strip()[-200:]}); it and the PRs after it wait")
                        break
                    with self.lock:
                        self.book.train.pop(c["pr"], None)
                        self.book.merger_runs.pop(c["pr"], None)
                    merged.append(c["pr"])
                    final = self.fetch_main(final)
                    self.after_merge(c["pr"], c["issue"], c["head"], c["needs_live"],
                                     "the review gate and the merge gates passed, and the full check passed on main "
                                     "with it merged (the merge train)")
                    if self.git("rev-parse", f"{final}^{{tree}}").stdout.strip() != c["tree"]:
                        self.event(f"merge train stopped after PR #{c['pr']}: main {final[:8]} is not the tree the "
                                   "train checked; the PRs after it wait, and main gets its own full check")
                        break
            finally:
                self.train_landing = False
        covered = checked and len(merged) == len(cars) and \
            self.git("rev-parse", f"{final}^{{tree}}").stdout.strip() == cars[-1]["tree"]
        self.log("train", base=base[:12], merged=merged, main=final[:12], checked=covered)
        if covered:
            with self.lock:
                self.main_check_sha = final  # main_check_due: this main is checked
            self.main_checked(final, True, out)
            self.flaky_deadlines()
            self.consolidation_chore(final)

    # --- monitor feed ---
    def observe(self, st: dict, now: float, started: list[Unit], stuck: list[Unit]):
        """Turn the change since the last snapshot into monitor events."""
        prev = self.prev
        if prev is not None:
            was_open = {i["number"]: i for i in prev["open"]}
            was_closed = {c["number"] for c in prev["closed"]}
            for i in st["open"]:
                n, labels = i["number"], set(i["labels"])
                old = was_open.get(n)
                if labels & IGNORED:
                    # A proposal waiting for the owner: not for the monitor to triage, but to
                    # screen. Of 127 UX proposals in one run the owner rejected 94, 59 of them
                    # as duplicates of open issues.
                    if old is None and n not in was_closed and self.cfg.ux_screen:
                        self.event(f"new UX proposal #{n}: {i['title']}. Screen it before the owner reads it "
                                   "(issuelooper-monitor skill, Screen UX proposals)")
                    continue
                if old is not None and IGNORED & set(old["labels"]):
                    self.event(f"#{n} is no longer ux:proposed (the owner accepted it): triage it: {i['title']}")
                    continue
                if old is None and n in was_closed:
                    self.event(f"#{n} was reopened: its newest comments say why: {i['title']}")
                elif old is None:
                    self.event(f"new issue #{n}: {i['title']}")
                elif "stage:owner" in labels and "stage:owner" not in old["labels"]:
                    self.event(f"#{n} moved to stage:owner (the owner's decision: wait for their comment, "
                               f"do not answer it yourself): {i['title']}")
                elif "stage:owner" in labels and i.get("comments", 0) > old.get("comments", 0):
                    # The owner answers stage:owner on the issue; say so, or the monitor keeps asking.
                    self.event(f"#{n} (stage:owner) has a new comment: read it with `gh issue view {n} --comments`; "
                               "act only on an answer from the owner (a comment with no role marker, "
                               "by someone with write access)")
                elif "stage:triage" in labels and "stage:pr" in old["labels"]:
                    self.event(f"#{n} was handed back from its pull request to stage:triage")
            for c in st["closed"]:
                if c["number"] in was_open and not IGNORED & set(c["labels"]):
                    self.event(f"#{c['number']} closed: {c['title']}")
            if st["main"] and prev["main"] and st["main"] != prev["main"]:
                self.event(f"main moved {prev['main'][:8]} -> {st['main'][:8]} (a merge)")
        for u in started:
            if u.kind == "reclaim":
                self.event(f"#{u.number}'s claim went stale; the supervisor is freeing it")
        for u in stuck:
            if u.key not in self.flagged:
                self.flagged.add(u.key)
                why = self.book.why_not(u, self.cfg)
                # Name the limit that holds it: "after 0 session(s) (family)" sent the
                # monitor looking at a unit that had never run.
                held = {
                    "tries": f"{self.book.tries[u.key]} session(s) on this state reached POOL_KEY_TRIES "
                             f"({self.cfg.key_tries})",
                    "family": f"{self.book.family_starts[u.family]} failed session(s) of {u.family} in all "
                              f"reached POOL_FAMILY_MAX ({self.cfg.family_max})",
                    "done": "a session on this same state already ended cleanly",
                }.get(why, why)
                self.event(f"stuck: {u.role} unit {u.kind} {self.label(u)} is still due, held: {held}")
        idle = not self.book.running and not started and not self.deploying
        if idle and not self.idle_flagged:
            self.idle_flagged = True
            if st["open"]:
                self.event(
                    f"pipeline idle: {len(st['open'])} open issues and no session has anything "
                    "to do (blocked, held, at stage:owner, or exhausted)"
                )
            else:
                self.event("backlog empty: no open issues and no session running")
        elif not idle:
            self.idle_flagged = False

    def summary(self, st: dict) -> str:
        c = collections.Counter()
        for i in st["open"]:
            labels = set(i["labels"])
            if labels & IGNORED:
                c["ux:proposed"] += 1
                continue
            stage = next((x for x in i["labels"] if x.startswith("stage:")), "none")
            c[stage] += 1
            if labels & HOLDS:
                c["held"] += 1
        stages = ", ".join(f"{k.replace('stage:', '')} {v}" for k, v in sorted(c.items()))
        good, _ = self.deployments()
        deployed = f"deployed {(good or 'nothing yet')[:8]}" if self.cfg.deploy_cmd else "no deploys"
        run = self.book.role_counts()
        sessions = ", ".join(f"{r} {run[r]}" for r in (*ROLES, "auto"))
        untriaged = [
            f"#{i['number']}"
            for i in st["open"]
            if not any(x.startswith("stage:") for x in i["labels"]) and not IGNORED & set(i["labels"])
        ]
        now = self.clock()
        old = [f"#{n} ({(now - t) / 60:.0f} min)" for n, t in sorted(self.book.holds.items())
               if now - t >= self.cfg.hold_review_s]
        waits = [f"#{n} ({(now - t) / 60:.0f} min)" for n, t in sorted(self.book.blocked.items())
                 if now - t >= self.cfg.blocked_review_s]
        return (
            f"Untriaged (yours): {', '.join(untriaged) or 'none'}. "
            + (f"Your holds older than {self.cfg.hold_review_s / 60:.0f} min: {', '.join(old)}. " if old else "")
            + (f"P0/P1 issues blocked over {self.cfg.blocked_review_s / 60:.0f} min: {', '.join(waits)}. "
               if waits else "")
            + f"Queue: {len(st['open'])} open ({stages}); {len(st['prs'])} open PRs; recently closed "
            f"{len(st['closed'])}. main {st['main'][:8]}, {deployed}. Sessions running: {sessions}. "
            f"Pool log: {self.cfg.logdir}/pool.jsonl, state: {self.cfg.logdir}/state.json."
        )

    def monitor_link(self):
        """How queue updates reach the monitor: a herdr pane (POOL_HERDR_MONITOR, the
        default launch) or the broker's socket (POOL_BROKER_SOCK); None when neither."""
        if self.cfg.herdr_monitor:
            if self._herdr is None:
                self._herdr = herdr_link.Monitor(self.cfg.herdr_monitor, self.cfg.herdr_quiet_s)
            return self._herdr
        if self.cfg.broker_sock:
            return BrokerLink(self.cfg.broker_sock)
        return None

    def feed(self, st: dict, now: float):
        link = self.monitor_link()
        if link is None or not self.events or now - self.last_sent < self.cfg.monitor_min_gap_s:
            return
        try:
            status = link.status()
        except (OSError, ValueError, herdr_link.HerdrError) as exc:
            self.log("feed_error", error=str(exc))
            return
        state = status.get("state", "")
        if state != self.monitor_state:
            self.monitor_state = state
            if state in ("blocked", "gone"):
                # A question dialog or an exited agent: the owner has to look, and until
                # then the updates pile up here.
                self.log("monitor_" + state, target=self.cfg.herdr_monitor)
        if status.get("busy") or int(status.get("done") or 0) < 1:
            return
        header = ""
        if self.cfg.feed_file:
            try:
                header = pathlib.Path(self.cfg.feed_file).read_text().strip()
            except OSError:
                header = ""
        header = header or FEED_HEADER
        with self.lock:
            lines, self.events = self.events, []
        text = header + "\n\nSince the last message:\n" + "\n".join(f"- {e}" for e in lines)
        text += "\n\n" + self.summary(st)
        try:
            r = link.say(text)
            self.last_sent = now
            self.log("feed", turn=r.get("turn"), events=len(lines))
        except (OSError, ValueError, herdr_link.HerdrError) as exc:
            with self.lock:
                self.events = lines + self.events
            self.log("feed_error", error=str(exc))

    def track_accept(self, st: dict) -> None:
        """Which issues waiting for acceptance (stage:accept, or stage:live from before)
        are in the build the tester checks: the deployed sha, or main when nothing
        deploys. An issue's merge commit comes from after_merge, or once from GitHub (a
        restarted supervisor; a failed read is tried again after ten minutes)."""
        b = self.book
        if not self.cfg.accept:
            b.accept_ready = []
            return
        target = self.own_deployed if self.cfg.deploy_cmd else st.get("main", "")
        now = self.clock()
        waiting = [i["number"] for i in st["open"] if set(i["labels"]) & set(ACCEPTING)]
        ready, fetched = [], False
        for n in waiting if target else []:
            if b.accept_in.get(n) == target:
                ready.append(n)
                continue
            m = b.accept_merged.get(n)
            if not m or (not m["sha"] and now - m["t"] > 600):
                m = b.accept_merged[n] = self.accept_merge_info(n)
            if not m["sha"]:
                continue
            r = self.git("merge-base", "--is-ancestor", m["sha"], target)
            if r.returncode not in (0, 1) and not fetched:
                fetched = True
                self.git("fetch", "-q", "origin", "main")
                r = self.git("merge-base", "--is-ancestor", m["sha"], target)
            if r.returncode == 0:
                b.accept_in[n] = target
                ready.append(n)
        for n in set(b.accept_merged) - set(waiting):
            if now - b.accept_merged[n]["t"] > 600:  # after_merge's entry precedes the label's poll
                b.accept_merged.pop(n, None)
                b.accept_in.pop(n, None)
                b.accept_rounds.pop(n, None)
        b.accept_ready, b.accept_target = ready, target

    def accept_merge_info(self, n: int) -> dict:
        """Issue n's merged PR from GitHub: {"pr", "sha", "live", "t"} ("sha" empty when unread)."""
        r = self.gh("pr", "list", "--state", "merged", "--head", f"issue-{n}", "--json", "number,mergeCommit,files",
                    "-L", "1")
        try:
            p = (json.loads(r.stdout or "[]") or [{}])[0]
        except (ValueError, IndexError, TypeError):
            p = {}
        files = [str(f.get("path") or "") for f in p.get("files") or [] if isinstance(f, dict)]
        return {"pr": p.get("number"), "sha": str((p.get("mergeCommit") or {}).get("oid") or ""),
                "live": touches_live(files), "t": self.clock()}

    def settle_accept(self, u: Unit, started: float, finished: bool) -> list[int]:
        """Act on the acceptance tester's verdict on each issue of its batch, as settle_live
        does: `accept-verified:` (or `live-verified:`) closes it, `accept-blocked:` sends it
        to the owner. A failure is checked once more by a fresh session before it counts:
        the harness's QA tester retried each failure, and a false failure costs a worker
        session. A counted failure goes back to stage:triage with the report; a second goes
        to the owner. An issue with no verdict is in the next batch. Returns the verified."""
        try:
            login = bare_login(claimlib.loop_login())
        except claimlib.GhError:
            which = ", ".join("#" + str(x["n"]) for x in u.info["issues"])
            self.event(f"acceptance check of {which} finished: read the tester's comments and settle each issue "
                       "(close, owner, or hand back)")
            return []
        start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
        verified = []
        for x in u.info.get("issues") or []:
            n, entry = x["n"], x.get("since") or ""
            try:
                r = self.gh("issue", "view", str(n), "--json", "state,labels,comments")
                data = json.loads(r.stdout) if r.returncode == 0 else None
            except (OSError, ValueError, subprocess.SubprocessError):
                data = None
            if not isinstance(data, dict):
                self.event(f"acceptance check of #{n} finished: read the tester's comment and settle it")
                continue
            labels = {y.get("name") for y in data.get("labels") or []}
            stage = next((s for s in ACCEPTING if s in labels), "")
            if data.get("state") != "OPEN" or not stage:
                continue  # somebody already settled it
            comments = [c for c in data.get("comments") or []
                        if bare_login(str((c.get("author") or {}).get("login") or "")) == login]
            verdicts = [c for c in comments if str(c.get("body", "")).lstrip().startswith("<!-- tester -->")
                        and str(c.get("createdAt", "")) >= max(entry, start)]
            if not verdicts:
                if finished:
                    with self.lock:
                        self.book.accept_rounds[n] += 1
                    self.event(f"#{n}: the acceptance tester left no verdict on it; it goes in the next batch")
                continue
            body, url = str(verdicts[-1].get("body", "")), verdicts[-1].get("url", "")
            if ACCEPT_BLOCKED.search(body):
                self.gh("issue", "edit", str(n), "--remove-label", stage, "--add-label", "stage:owner")
                self.gh("issue", "comment", str(n), "--body",
                        f"<!-- merger -->\nThe tester could not run the acceptance check ({url}): it needs what that "
                        "comment names, which only the owner can provide. Waiting at stage:owner.")
                self.event(f"#{n}: the acceptance check could not run (accept-blocked); moved to stage:owner. "
                           "Tell the owner what is missing.")
                how = "blocked"
            elif ACCEPT_VERIFIED.search(body):
                self.gh("issue", "edit", str(n), "--remove-label", stage)
                self.gh("issue", "close", str(n), "--reason", "completed", "--comment",
                        f"<!-- merger -->\nClosed: the tester's acceptance check passed ({url}).")
                verified.append(n)
                how = "verified"
            elif not any(ACCEPT_RETRY in str(c.get("body", "")) and str(c.get("createdAt", "")) >= entry
                         for c in comments):
                self.gh("issue", "comment", str(n), "--body",
                        f"<!-- merger -->\n{ACCEPT_RETRY}\nThe acceptance check did not show the behavior ({url}). "
                        "A fresh tester session checks it once more before it counts.")
                with self.lock:
                    self.book.accept_rounds[n] += 1
                how = "retry"
            elif sum(ACCEPT_HANDBACK in str(c.get("body", "")) or LIVE_HANDBACK in str(c.get("body", ""))
                     for c in comments) >= 1:
                self.gh("issue", "edit", str(n), "--remove-label", stage, "--add-label", "stage:owner")
                self.gh("issue", "comment", str(n), "--body",
                        f"<!-- merger -->\n{ACCEPT_HANDBACK}\nThe acceptance check failed again ({url}), after a second "
                        "fix. Whether the acceptance holds as written is the owner's call. Waiting at stage:owner.")
                self.event(f"#{n}: its acceptance check failed after a second fix; moved to stage:owner. Add one "
                           "comment listing what each fix changed and what each check saw.")
                how = "owner"
            else:
                self.gh("issue", "edit", str(n), "--remove-label", stage, "--add-label", "stage:triage")
                self.gh("issue", "comment", str(n), "--body",
                        f"<!-- merger -->\n{ACCEPT_HANDBACK}\nBack to stage:triage: two acceptance checks after the "
                        f"merge did not show the behavior ({url}). The next worker starts from what that comment saw.")
                self.event(f"#{n}: the acceptance check failed twice; handed back to stage:triage.")
                self.failed_try(n)
                how = "handback"
            self.log("accept_settled", issue=n, verdict=how)
        return verified

    def accept_chore(self, logdir: pathlib.Path, verified: list[int]) -> None:
        """One chore issue for the scripts of the issues this acceptance unit verified
        ($LOGDIR/accept/test_accept_<N>.py), with [accept] guard set: a worker turns them
        into tests/accept/test_accept_<N>.py through the usual gates. Not for an issue
        whose test is already on main (its own worker wrote one)."""
        if not self.cfg.accept_guard or not verified:
            return
        self.git("fetch", "-q", "origin", "main")
        found = []
        for n in verified:
            path = logdir / "accept" / f"test_accept_{n}.py"
            if path.is_file() and self.git("cat-file", "-e", f"origin/main:tests/accept/test_accept_{n}.py").returncode:
                found.append((n, path.read_text(errors="replace")))
        if not found:
            return
        names = ", ".join(f"#{n}" for n, _ in found)
        body = (
            f"<!-- merger -->\nThe tester verified {names} on the deployed build with these scripts. Add each as "
            "`tests/accept/test_accept_<N>.py`: Playwright, in scripted mode wherever the issue does not need live "
            "mode, starting the app the way the project's e2e tests do, and keeping every check the script makes. "
            "The supervisor runs `tests/accept/` against each deploy with no model, and a failure files "
            "`Regression of #N`; such a test guards what the tester saw work.\n"
        )
        for n, text in found:
            room = 60000 - len(body)
            if room < 2000:
                body += f"\n#{n}: `{logdir / 'accept' / f'test_accept_{n}.py'}` (too long to quote here)\n"
                continue
            body += (f"\n### #{n}\n\n```python\n" + text[: min(12000, room - 200)].replace("```", "'" * 3)
                     + "\n```\n")
        r = self.gh("issue", "create", "--title", f"Acceptance tests for {names}"[:200], "--label", "P2",
                    "--label", "stage:triage", "--body", body)
        m = re.search(r"/issues/(\d+)", r.stdout)
        self.log("accept_chore", issues=[n for n, _ in found], ok=bool(m))
        if m:
            self.event(f"filed #{m.group(1)}: turn the acceptance scripts of {names} into tests/accept/ guards")

    def guard_due(self, st: dict) -> None:
        """Run [accept] guard (tests/accept/) on each new build, the deployed sha or main
        when nothing deploys, in the main lane it shares with the train and the main
        check. No model is involved."""
        target = self.own_deployed if self.cfg.deploy_cmd else st.get("main", "")
        with self.lock:
            if (not self.cfg.accept_guard or not target or target == self.guard_sha or self.main_check_running
                    or self.stopping):
                return
            self.main_check_running, self.guard_sha = True, target
        self.main_check_thread = threading.Thread(target=self.run_guard, args=(target,), daemon=True)
        self.main_check_thread.start()

    def run_guard(self, sha: str) -> None:
        root, wt = str(repo_root()), scratch(f"main-guard-{sha[:12]}")
        try:
            remove_worktree(root, wt, self.log)
            self.git("fetch", "-q", "origin", "main")
            if self.git("worktree", "add", "-q", "--detach", wt, sha).returncode != 0:
                self.log("accept_guard", sha=sha[:12], ok=None, why="no worktree")
                return
            if not (pathlib.Path(wt) / "tests" / "accept").is_dir():
                return  # no guard on main yet
            try:
                r = self.run_owned(["bash", "-c", self.cfg.accept_guard], self.cfg.main_check_s, cwd=wt,
                                   env=dict(os.environ, ACCEPT_SHA=sha), stdin=subprocess.DEVNULL)
                ok, out = r.returncode == 0, r.stdout + r.stderr
                if r.returncode == 5 and "no tests ran" in out:
                    ok = True  # pytest found nothing to run: tests/accept/ holds no test yet
            except (OSError, subprocess.TimeoutExpired) as exc:
                ok, out = None, str(exc)
            failing = failing_tests(out) if ok is False else []
            if ok is False and INFRA.search(out):
                ok = None
            if ok is False:
                again, _ = self.rerun_tests(wt, failing)  # the same commit: a pass makes them flaky
                if again:
                    self.report_flaky(f"the acceptance guard at {sha[:8]}", failing)
                    ok = True
            self.log("accept_guard", sha=sha[:12], ok=ok, failing=failing[:5])
            if ok is False:
                self.report_regressions(sha, failing, out)
        except Exception as exc:  # noqa: BLE001 - a failed guard is not a failed supervisor
            self.log("accept_guard_error", sha=sha[:12], error=str(exc)[:300])
        finally:
            remove_worktree(root, wt, self.log)
            with self.lock:
                self.main_check_running = False
            self.wake.set()

    def report_regressions(self, sha: str, failing: list[str], out: str) -> None:
        """File `Regression of #N: <test>` (P1) per failing acceptance test, unless an open
        issue already names that test."""
        r = self.gh("issue", "list", "--state", "open", "--search", "in:title Regression", "--json", "number,title")
        try:
            titles = [str(i.get("title", "")) for i in json.loads(r.stdout or "[]")]
        except (ValueError, TypeError, AttributeError):
            titles = []
        for t in (failing or [""])[:10]:
            m = re.search(r"test_accept_(\d+)", t)
            title = (f"Regression of #{m.group(1)}: {t}" if m
                     else f"Acceptance guard fails at {sha[:8]}: {t or 'see the output'}")[:200]
            if any((t or title) in x for x in titles):
                continue
            body = (f"<!-- merger -->\nThe acceptance guard (`{self.cfg.accept_guard}`) fails on the build at "
                    f"{sha[:8]}" + (f": `{t}`, the test that guards #{m.group(1)}, which the tester had verified."
                                    if m else ".")
                    + " It failed again when it ran alone at the same commit, so it is not a flake.\n\n"
                    "The end of the output:\n\n```\n" + out.strip()[-3000:].replace("```", "'" * 3) + "\n```\n")
            c = self.gh("issue", "create", "--title", title, "--label", "type:bug", "--label", "P1",
                        "--label", "stage:triage", "--body", body)
            n = re.search(r"/issues/(\d+)", c.stdout)
            self.log("regression_filed", test=t[:200], ok=bool(n))
            self.event(f"acceptance guard at {sha[:8]}: {t or 'it fails'}; "
                       + (f"filed #{n.group(1)}" if n else "could not file an issue"))

    def settle_live(self, u: Unit) -> None:
        """Act on the tester's verdict on a stage:live issue: close it on a `live-verified:`
        line, send it to the owner on `live-check-blocked:`, hand it back to stage:triage
        otherwise, and to the owner on its second failed check. The monitor did this by
        hand, one turn each (24 closes in one run); it is told what happened. When the
        verdict cannot be read, the monitor is asked to act, as before."""
        n = u.number
        ask = (f"live check of #{n} finished: read the tester's comment on #{n}; if it says "
               "live-check-blocked:, move it to stage:owner for the owner; otherwise close the issue "
               "if it passed, or hand it back if it failed")
        try:
            r = self.gh("issue", "view", str(n), "--json", "state,labels,comments")
            data = json.loads(r.stdout) if r.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            data = None
        if not isinstance(data, dict):
            self.event(ask)
            return
        labels = {x.get("name") for x in data.get("labels") or []}
        if data.get("state") != "OPEN" or "stage:live" not in labels:
            return  # somebody already settled it
        try:
            login = bare_login(claimlib.loop_login())
        except claimlib.GhError:
            self.event(ask)
            return
        # Only the loop's own account speaks for the tester and the supervisor.
        comments = [c for c in data.get("comments") or []
                    if bare_login(str((c.get("author") or {}).get("login") or "")) == login]
        since = u.info.get("since") or ""
        verdicts = [c for c in comments if str(c.get("body", "")).lstrip().startswith("<!-- tester -->")
                    and str(c.get("createdAt", "")) >= since]
        if not verdicts:
            self.event(ask)
            return
        body, url = str(verdicts[-1].get("body", "")), verdicts[-1].get("url", "")
        if LIVE_BLOCKED.search(body):
            self.gh("issue", "edit", str(n), "--remove-label", "stage:live", "--add-label", "stage:owner")
            self.gh("issue", "comment", str(n), "--body",
                    f"<!-- merger -->\nThe tester could not run the live check ({url}): it needs what that "
                    "comment names, which only the owner can provide. Waiting at stage:owner.")
            self.event(f"#{n}: the live check could not run (live-check-blocked); moved to stage:owner. "
                       "Tell the owner what is missing.")
            how = "blocked"
        elif LIVE_VERIFIED.search(body):
            self.gh("issue", "edit", str(n), "--remove-label", "stage:live")
            self.gh("issue", "close", str(n), "--reason", "completed", "--comment",
                    f"<!-- merger -->\nClosed: the tester's live check passed ({url}).")
            self.event(f"#{n}: the live check passed; the supervisor closed it.")
            how = "verified"
        elif sum(LIVE_HANDBACK in str(c.get("body", "")) for c in comments) >= 1:
            self.gh("issue", "edit", str(n), "--remove-label", "stage:live", "--add-label", "stage:owner")
            self.gh("issue", "comment", str(n), "--body",
                    f"<!-- merger -->\n{LIVE_HANDBACK}\nThe second live check failed too ({url}). Two fixes "
                    "merged and neither worked live: whether the acceptance holds as written is the owner's "
                    "call. Waiting at stage:owner.")
            self.event(f"#{n}: its second live check failed; moved to stage:owner. Add one comment listing what "
                       "each fix changed and what each check saw.")
            how = "owner"
        else:
            self.gh("issue", "edit", str(n), "--remove-label", "stage:live", "--add-label", "stage:triage")
            self.gh("issue", "comment", str(n), "--body",
                    f"<!-- merger -->\n{LIVE_HANDBACK}\nBack to stage:triage: the live check after the merge "
                    f"did not show the behavior ({url}). The next worker starts from what that comment saw.")
            self.event(f"#{n}: the live check failed; handed back to stage:triage.")
            how = "handback"
        self.log("live_settled", issue=n, verdict=how)

    def track_holds(self, st: dict, now: float) -> None:
        """Remember when each hold:monitor appeared, and remind the monitor once of a hold
        older than POOL_HOLD_REVIEW_MIN: it kept its own hold on #54 for 3 h 40 min after
        the conflict wave it was set for had passed, and the last issues waited for it."""
        b = self.book
        held = {i["number"] for i in st["open"] if "hold:monitor" in i["labels"]}
        for n in held:
            b.holds.setdefault(n, now)
        for n in set(b.holds) - held:
            del b.holds[n]
            b.holds_told.discard(n)
        for n in sorted(held):
            if n not in b.holds_told and now - b.holds[n] >= self.cfg.hold_review_s:
                b.holds_told.add(n)
                self.event(f"#{n} has been at hold:monitor for {(now - b.holds[n]) / 60:.0f} min: read the condition "
                           "your hold comment names and lift the hold if it is met")

    def track_blocked(self, st: dict, now: float) -> None:
        """Point the monitor, once, to a claimable P0 or P1 issue that has waited
        POOL_BLOCKED_REVIEW_MIN on open blockers. One P1 fix waited 6.7 hours on three
        dependencies while workers took lower-ranked issues around it, and nobody
        looked at whether it still needed them."""
        b = self.book
        done = accepted(st)  # a blocker at stage:accept is merged: nothing to review
        native = {i["number"]: native_blockers(i, done) for i in st["open"]}
        waiting = {
            i["number"]: i for i in st["open"]
            if native[i["number"]] and set(i["labels"]) & {"P0", "P1"} and set(i["labels"]) & CLAIMABLE
            and not set(i["labels"]) & (HOLDS | IGNORED)
        }
        for n in waiting:
            b.blocked.setdefault(n, now)
        for n in set(b.blocked) - set(waiting):
            del b.blocked[n]
            b.blocked_told.discard(n)
        for n, i in sorted(waiting.items()):
            if n not in b.blocked_told and now - b.blocked[n] >= self.cfg.blocked_review_s:
                b.blocked_told.add(n)
                prio = next(p for p in ("P0", "P1") if p in i["labels"])
                which = ", ".join(f"#{x}" for x in native[n] if x != "?")
                self.event(
                    f"#{n} ({prio}) has waited {(now - b.blocked[n]) / 60:.0f} min on {len(native[n])} open "
                    f"blocker(s){' (' + which + ')' if which else ''} (`gh api repos/{{owner}}/{{repo}}/issues/{n}/dependencies/blocked_by`): keep "
                    f"each only if #{n} cannot work or would conflict without it, and remove the rest "
                    "(issuelooper-monitor skill, Dependencies)"
                )

    def failed_try(self, n: int | None) -> None:
        """Count a failure of issue n's own toward asking for its split (ask_decompose)."""
        if n is not None:
            with self.lock:
                self.book.failed_tries[n] += 1

    def ask_decompose(self, st: dict) -> None:
        """Ask the monitor, once per issue, to split one that has failed POOL_DECOMPOSE_AFTER
        times (Book.failed_tries) and is waiting for the next worker. The monitor may split
        any issue, and in a 12-hour run split none: one issue took eleven sessions."""
        b, k = self.book, self.cfg.decompose_after
        if not k:
            return
        open_ = {i["number"]: i for i in st["open"]}
        for n in list(b.failed_tries):
            i = open_.get(n)
            if i is None:
                del b.failed_tries[n]  # closed: done with
                continue
            labels = set(i["labels"])
            if (n in b.decompose_told or b.failed_tries[n] < k or not labels & (CLAIMABLE | {"stage:owner"})
                    or labels & (HOLDS | IGNORED) or PART_OF.search(i.get("body") or "")):
                continue
            b.decompose_told.add(n)
            self.event(
                f"#{n} has failed {b.failed_tries[n]} times and is not merged: {i['title']} (worker sessions that "
                "ended without a pull request, or pull requests handed back for red tests or the review). Read why "
                "each ended (the release and hand-back comments): if it is too big for one session, split it into "
                "sub-issues (issuelooper-monitor skill, Split a large issue); if it failed for another reason, "
                "re-scope it or leave it"
            )

    def check_disk(self) -> None:
        """Tell the monitor once when the scratch root's file system has under 5% of its
        space or inodes free, and once when it recovers. In run 9 /tmp ran out of inodes
        with 44% of its space free, and checks failed for a quarter of an hour unnoticed."""
        try:
            root = config.scratch_root()
            v = os.statvfs(root if root.exists() else root.parent)
        except OSError:
            return
        low = [what for what, free, total in (("space", v.f_bavail, v.f_blocks), ("inodes", v.f_favail, v.f_files))
               if total and free / total < 0.05]
        if low and not self.disk_low:
            self.event(f"the file system under {root} is nearly full ({' and '.join(low)} under 5% free): "
                       "checks will fail there until it is cleared")
        elif self.disk_low and not low:
            self.event(f"the file system under {root} has room again")
        self.disk_low = bool(low)

    def check_files(self) -> None:
        """Tell the monitor when a file the loop relies on goes missing from the shared
        checkout, and when it is back: POOL_REQUIRED_FILES, by default .env when live
        checks are on. In the sixth run .env disappeared; the live deploy stayed on an old
        commit for 4.5 hours and nobody was told."""
        root = repo_root()
        missing = [f for f in self.cfg.required_files if not (root / f).exists()]
        gone = [f for f in missing if f not in self.book.missing]
        back = [f for f in self.book.missing if f not in missing]
        self.book.missing = missing
        if gone:
            self.log("files_missing", files=gone)
            self.event(f"the shared checkout is missing {', '.join(gone)} (only the owner has it): live checks are "
                       "held and the live deploy cannot start; ask the owner to restore it, and do not close "
                       "needs:live issues meanwhile")
        if back:
            self.log("files_back", files=back)
            self.event(f"{', '.join(back)} is back in the shared checkout: live checks start again")

    def track_live(self, st: dict):
        """Remember each stage:live issue's updatedAt from when it entered stage:live;
        the live unit's key carries it. Leaving stage:live (a hand-back, a reopen, a new
        pull request: each moves the label and so updatedAt) forgets it. A read older than
        the entry (a stale replica) changes nothing, and neither do later comments."""
        entry = self.book.live_entry
        open_now = set()
        for i in st["open"]:
            n = i["number"]
            open_now.add(n)
            if set(i["labels"]) & set(ACCEPTING):
                entry.setdefault(n, i["updated"])
            elif n in entry and i["updated"] > entry[n]:
                del entry[n]
        for n in set(entry) - open_now:
            del entry[n]

    # --- the loop ---
    def step(self, now: float | None = None) -> list[Unit]:
        now = time.time() if now is None else now
        st = self.fetcher()
        self.last_fetch = now
        good, _ = self.deployments()
        self.track_accept(st)  # git and gh reads: outside the lock
        with self.lock:
            self.track_live(st)
            self.track_holds(st, now)
            self.track_blocked(st, now)
            self.ask_decompose(st)
            self.check_files()
            self.check_disk()
            self.track_starving(st, now)
            due, stuck = plan(st, self.book, self.cfg, now, self.ux_target(st))
            started = admit(due, self.book, self.cfg, now)
        started = [u for u in started if self.start(u)]  # none once stopping
        self.observe(st, now, started, stuck)
        self.prev = st
        if self.hurry_pr is not None and not any(
            p["number"] == self.hurry_pr and p["mergeable"] == "UNKNOWN" for p in st["prs"]
        ):
            self.hurry_pr = None
        self.deploy_check(st, now)
        self.train_due(st)  # first: its sweep covers main too
        self.main_check_due(st)
        self.guard_due(st)
        self.feed(st, now)
        if not self.events:
            self.pending_since = 0.0
        elif not self.pending_since:
            self.pending_since = now
        state = {
            "t": now,
            "signature": signature(st),
            "main": st["main"],
            "deployed": good,
            "running": {f: u.key for f, u in self.book.running.items()},
            "due_not_started": [u.key for u in due if u not in started],
            "stuck": [u.key for u in stuck],
            "paused": self.book.paused,
            "queue": queue_snapshot(st),
            "sessions": [
                {"seq": w.seq, "role": w.unit.role, "kind": w.unit.kind, "number": w.unit.number,
                 "unit": w.unit.key, "started": w.started, "dir": str(w.dir),
                 "timeout_s": seconds(self.cfg.timeouts[w.unit.role]) if w.unit.role in self.cfg.timeouts else 0,
                 "cli": w.cli.name}
                for w in list(self.sessions.values())
            ],
            "feed_pending": len(self.events),
            "feed_pending_since": self.pending_since,
            "main_check": self.main_check_last,
            "train": {"running": self.train_running, "queue": sorted(self.book.train)},
            "accept": {"ready": self.book.accept_ready, "build": self.book.accept_target[:12]},
            "missing_files": self.book.missing,
            "ux_scores": self.ux_scores[-10:],
        }
        q = state["queue"]
        brief = {"open": q["open"], **q["stages"], "prs": sum(q["prs"].values())}
        if brief != self.last_queue:
            self.last_queue = brief
            self.log("queue", **brief)
        (self.cfg.logdir / "state.json").write_text(json.dumps(state, indent=1))
        return started

    def run(self):
        if self.cfg.accept:  # a repository installed before the acceptance check lacks the label
            self.gh("label", "create", ACCEPT_STAGE, "--color", "c5def5", "--force",
                    "--description", "Merged; the acceptance check is owed")
        pruned = prune_scratch()
        if pruned:
            self.log("scratch_pruned", count=len(pruned), paths=pruned[:20])
        self.log(
            "supervisor_start",
            caps=self.cfg.caps,
            global_cap=self.cfg.global_cap,
            poll_s=self.cfg.poll_s,
            clis={r: agent_cli.cli_for(r) for r in ROLES},
            deploys=bool(self.cfg.deploy_cmd),
            main_check=self.cfg.main_check,
            live=self.cfg.live,
            ux=self.cfg.ux,
        )
        while not self.stopping:
            poll = self.cfg.poll_s
            if self.hurry_pr is not None and time.time() < self.hurry_until:
                poll = min(poll, self.cfg.min_fetch_s)
            wait = max(poll - (time.time() - self.last_fetch), 0)
            self.wake.wait(timeout=wait)
            self.wake.clear()
            if self.stopping:
                break
            gap = self.cfg.min_fetch_s - (time.time() - self.last_fetch)
            if gap > 0:
                time.sleep(gap)
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - a failed poll is not a change
                self.log("poll_error", error=str(exc)[:300])
                self.last_fetch = time.time()
        self.drain()

    def drain(self, timeout: float = 60.0):
        """After stop(): wait for the killed sessions to save their work and release
        their claims. They are daemon threads, so without this the process exits under
        them and their issues stay at stage:doing until the claims go stale."""
        deadline = time.time() + timeout
        for w in list(self.sessions.values()):
            w.join(timeout=max(0.0, deadline - time.time()))
        if self.main_check_thread is not None:  # so it removes its worktree
            self.main_check_thread.join(timeout=max(0.0, deadline - time.time()))

    def stop(self, *_):
        with self.lock:  # Session.session and run_owned start their processes under this lock
            self.stopping = True
            self.halt.set()
            sessions = list(self.sessions.values())
            owned = list(self.owned)
        self.wake.set()
        for w in sessions:
            w.kill()
        for proc in owned:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass

    def run_owned(self, args: list[str], timeout: float, **kw) -> subprocess.CompletedProcess:
        """subprocess.run for the supervisor's own long commands (the main check, re-sweeps,
        fast checks), in a process group of their own that stop() ends: a main check went
        on for six minutes after `issuelooper stop --all`. Output captured as text; once
        stopping, nothing starts."""
        kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE)
        with self.lock:
            if self.stopping:
                return subprocess.CompletedProcess(args, -signal.SIGTERM, "", "stopped with the supervisor")
            proc = subprocess.Popen(args, start_new_session=True, text=True, **kw)
            self.owned.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # SIGTERM first, so a sweep records itself as killed (runs.jsonl), then SIGKILL.
            for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, None)):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    pass
                try:
                    proc.communicate(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
            raise
        finally:
            with self.lock:
                self.owned.discard(proc)
            # What it started and left in its process group goes with it (an app server
            # a test fixture started and did not stop).
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        return subprocess.CompletedProcess(args, proc.returncode, out or "", err or "")


def cmd_run(_a) -> int:
    os.environ.setdefault("SHARED_REPO_PATH", str(repo_root()))
    cfg = Config.from_env()
    # Sessions and the review gate find the pool's logs here.
    os.environ["POOL_LOGDIR"] = str(cfg.logdir)
    # Sweeps (sweep_locked.py) and the merge gates (role_guard.py) follow the train's state.
    os.environ["POOL_MERGE_TRAIN"] = "1" if cfg.merge_train else "0"
    os.environ["POOL_ACCEPT"] = "1" if cfg.accept else "0"
    pool = Pool(cfg)
    signal.signal(signal.SIGTERM, pool.stop)
    signal.signal(signal.SIGINT, pool.stop)
    pool.run()
    return 0


def cmd_prune(_a) -> int:
    gone = prune_scratch()
    print(f"scratch: removed {len(gone)} left from earlier runs" if gone else "scratch: nothing to remove")
    return 0


def cmd_plan(a) -> int:
    cfg = Config.from_env()
    st = normalize(json.loads(pathlib.Path(a.fixture).read_text())) if a.fixture else fetch(cfg)
    book = Book()
    due, stuck = plan(st, book, cfg, time.time(), a.ux_sha or (st["main"] if cfg.ux and not cfg.deploy_cmd else ""))
    for u in admit(due, book, cfg, time.time()):
        print(f"{u.role}\t{u.kind}\t{u.key}")
    return 0


def cmd_probe(a) -> int:
    """One session with a role's environment, for a headless check of the CLI setup."""
    cfg = Config.from_env()
    pool = Pool(cfg, fetcher=lambda: {}, spawn=False)
    u = Unit(a.role, "probe", f"probe:{a.role}", f"probe:{a.role}", None, (9,), {})
    w = Session(pool, u, 0)
    w.dir = cfg.logdir / f"probe-{a.role}-{int(time.time())}"
    w.dir.mkdir(parents=True, exist_ok=True)
    env = w.env()
    if a.role == "worker":
        env.update(WRK_K="0", WRK_CLAIM_FILE=str(w.dir / "claim"), WORKER_POOL="unlimited")
    rc, elapsed, _ = w.session(a.prompt, env)
    print(
        (w.dir / "session.log").read_text(errors="replace")
        if (w.dir / "session.log").exists()
        else ""
    )
    print(f"probe {a.role}: exit {rc} after {elapsed:.0f} s; logs in {w.dir}")
    return rc


def main(argv: list[str] | None = None) -> int:
    config.apply()
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    p = sub.add_parser("plan")
    p.add_argument("--fixture")
    p.add_argument("--ux-sha", default="", help="the sha a UX run would test (POOL_UX=1)")
    sub.add_parser("prune", help="remove what earlier runs left in scratch (while the supervisor is stopped)")
    p = sub.add_parser("probe")
    p.add_argument("role", choices=ROLES)
    p.add_argument("--prompt", default="Report your CC_ROLE and the skills you can see, then stop.")
    a = ap.parse_args(argv)
    return {"run": cmd_run, None: cmd_run, "plan": cmd_plan, "probe": cmd_probe, "prune": cmd_prune}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
