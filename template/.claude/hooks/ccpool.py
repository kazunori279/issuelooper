#!/usr/bin/env python3
"""The supervisor of the issue loop: one process per repository.

    ccpool.py run                 supervise until killed (`issuelooper start` starts it)
    ccpool.py plan [--fixture F]  print what would start now; writes nothing
    ccpool.py probe ROLE [--prompt TEXT]
                                  one session with ROLE's environment

One process reads GitHub every POOL_POLL_S seconds (one GraphQL request) and turns
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
          squash-merged; a refusal hands the PR back to a worker (a refusal over
          the sweep record is re-swept here first). With POOL_FAST_CHECK_CMD set, a
          PR whose head is behind main is first merged with main in a worktree and
          the command runs there, under the merge lock right before the merge: a
          failure hands the PR back, unless main alone fails it too. rebase: a CONFLICTING PR is first rebased here, once per
          head and main, and not again for a head whose rebase failed; when every
          conflict hunk, in any text file, only adds lines on both sides, both are
          kept, the PR is swept and pushed, and the next poll merges it. The conflicted files are
          counted: a source file that conflicts in POOL_SPLIT_AFTER PRs within
          POOL_SPLIT_WINDOW_S, or grows past POOL_SPLIT_LINES on main, gets an
          issue to split it (at most POOL_SPLIT_PER_WINDOW per window,
          POOL_SPLIT_MAX per run). reclaim: a stage:doing claim older than
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
          /tmp/issuelooper-<repository directory>/ux/; the score on its report's
          first line (`Score: N/100`) is logged and shown on the dashboard.

Triage (an issue with no stage label), priorities, holds and dependencies are the
monitor's; no unit is started for them, and none for an issue with an open blocker
(a `Blocked by:` line or a GitHub issue dependency). Issues labeled ux:proposed are proposals
waiting for the owner: no unit and no triage. A reopened issue is an open issue
like any other; its newest comments say why it came back.

A unit's key carries the state it was computed from (the issue's updatedAt, the
PR's head and main). A key never runs twice at once, never again after a clean
exit, and at most POOL_KEY_TRIES times after failures; a role+number family runs
one unit at a time and fails at most POOL_FAMILY_MAX times per supervisor lifetime
(sessions that end ok do not count; workers, bounded by claim.py's failed attempts,
and UX runs, one family per sha, are exempt).

SESSIONS

Each role runs on the agent CLI agent_cli.py picks for it (agy or Claude Code,
CCROLE_<ROLE>_CLI > CCROLE_CLI > agy), started through ccrole.sh. A worker still
going at POOL_WORKER_WRAP_S is resumed in the same conversation with a wrap-up
prompt and POOL_WORKER_WRAP_BUDGET_S. A session whose model went silent after a
finished step for POOL_STALL_S (POOL_STALL_FAST_S for a model whose name contains
one of POOL_STALL_FAST_MODELS) is resumed (twice at most). A session that ends on a
model quota error is resumed in the same conversation up to POOL_QUOTA_RESUMES
times, each after a wait that doubles from POOL_QUOTA_WAIT_S up to
POOL_QUOTA_WAIT_MAX_S; one that still ends on it is "quota error": no try spent and
the claim released without a failed attempt. Every quota error also holds new
agent sessions pool-wide for a doubling pause (quota_seen), reset when a session
ends ok. While the CLI itself retries a failed model call inside a turn (agy: the
stream's last event is an error_message step), the session's clocks (timeout,
wrap-up, stall) stand still, up to POOL_RETRY_PAUSE_MAX_S per session.

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
tried again after 5 s and 10 s. While a merger session is running it waits up to
POOL_DEPLOY_GRACE_S, so one deploy carries both merges. Empty: no deploys.

MAIN CHECK

With POOL_MAIN_CHECK (on by default when SWEEP_COMMAND is set), each time origin/main
moves the supervisor runs the full check suite on it in a worktree (sweep_locked.py,
so it takes a sweep slot like any sweep), in the background, one run at a time: a
main that moved during a run is not checked, only the newest. A red main tells the
monitor and files an issue `main is red: <first failing tests>` (type:bug, P0,
stage:triage) with the failing tests, the commits since the last green main and the
end of the output, or comments on the one already open; the next green main closes
it. Merges never wait for it.

MONITOR

The monitor is broker.py's long-lived session: it triages and re-prioritizes. When
the queue changed in a way it acts on (a new, closed or reopened issue, a merge,
stage:owner, a hand-back, a failed or stuck unit, a failed deploy, a stalled or
drained queue) and the monitor is idle, the supervisor sends it one message through
the broker socket with everything since the last one. Not before the monitor's
first turn (the owner's kickoff), and not more often than POOL_MONITOR_MIN_GAP_S.

Logs: POOL_LOGDIR (default $ISSUELOOPER_RUN/pool): pool.jsonl, state.json, one
directory per session, deploy.log and deploys.jsonl.
"""

from __future__ import annotations

import argparse
import ast
import collections
import dataclasses
import datetime as dt
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
BRANCH = re.compile(r"^issue-(\d+)\b")

QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    open: issues(states: OPEN, first: 100) {
      nodes { number title body updatedAt labels(first: 30) { nodes { name } } comments { totalCount }
              issueDependenciesSummary { blockedBy } }
    }
    closed: issues(states: CLOSED, first: 60, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { number title closedAt stateReason labels(first: 30) { nodes { name } } }
    }
    pullRequests(states: OPEN, first: 60) {
      nodes { number headRefName headRefOid body isDraft mergeable }
    }
    main: ref(qualifiedName: "refs/heads/main") { target { oid } }
  }
}
"""


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
    ux: bool = False
    broker_sock: str = ""
    herdr_monitor: str = ""
    herdr_quiet_s: float = 3
    feed_file: str = ""
    monitor_min_gap_s: float = 60
    deploy_grace_s: float = 300
    key_tries: int = 2
    family_max: int = 6
    crash_max: int = 3
    crash_pause_s: float = 120
    quota_resumes: int = 8
    quota_wait_s: float = 60
    quota_wait_max_s: float = 900
    stall_s: float = 600
    stall_fast_s: float = 240
    stall_fast_models: str = "claude"
    # The most a session's clocks stand still for the CLI's own retries of a model call;
    # 20 min was not enough for a model whose calls kept failing for a while.
    retry_pause_max_s: float = 2400
    fast_check_cmd: str = ""  # run on a behind PR merged with main; empty = no check
    main_check: bool = False  # run the full sweep on each new origin/main (Pool.main_check_due)
    fast_check_s: float = 300
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
        return cls(
            poll_s=float(e("POOL_POLL_S", "15")),
            min_fetch_s=float(e("POOL_MIN_FETCH_S", "3")),
            caps={r: env_int(f"POOL_CAP_{r.upper()}", 0) for r in ROLES},
            global_cap=env_int("POOL_MAX_WORKERS", 0),
            timeouts={
                "worker": e("POOL_TIMEOUT_WORKER", "45m"),
                "merger": e("POOL_TIMEOUT_MERGER", "45m"),
                # 60 min: a UX run walks every journey and reads every screenshot;
                # testers kept timing out at 45.
                "tester": e("POOL_TIMEOUT_TESTER", "60m"),
            },
            logdir=pathlib.Path(e("POOL_LOGDIR", "") or run_dir() / "pool"),
            deploy_cmd=e("POOL_DEPLOY_CMD", "").strip(),
            deploy_timeout_s=float(e("POOL_DEPLOY_TIMEOUT_S", "") or 420),
            live=env_flag("POOL_LIVE"),
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
            crash_max=env_int("POOL_CRASH_MAX", 3),
            crash_pause_s=float(e("POOL_CRASH_PAUSE_S", "120")),
            quota_resumes=env_int("POOL_QUOTA_RESUMES", 8),
            quota_wait_s=float(e("POOL_QUOTA_WAIT_S", "60")),
            quota_wait_max_s=float(e("POOL_QUOTA_WAIT_MAX_S", "900")),
            stall_s=float(e("POOL_STALL_S", "600")),
            stall_fast_s=float(e("POOL_STALL_FAST_S", "") or 240),
            stall_fast_models=e("POOL_STALL_FAST_MODELS", "claude"),
            retry_pause_max_s=float(e("POOL_RETRY_PAUSE_MAX_S", "") or 2400),
            fast_check_cmd=e("POOL_FAST_CHECK_CMD", "").strip(),
            main_check=env_flag("POOL_MAIN_CHECK", True) and bool(e("SWEEP_COMMAND", "").strip()),
            fast_check_s=float(e("POOL_FAST_CHECK_S", "") or 300),
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
            split_window_s=float(e("POOL_SPLIT_WINDOW_S", "5400")),
            split_max=env_int("POOL_SPLIT_MAX", 8),
            split_per_window=env_int("POOL_SPLIT_PER_WINDOW", 1),
            split_lines=env_int("POOL_SPLIT_LINES", 900),
            worker_wrap_s=float(e("POOL_WORKER_WRAP_S", "2100")),
            # 12 min: workers on slower models ran out of 8 before they had pushed.
            worker_wrap_budget_s=float(e("POOL_WORKER_WRAP_BUDGET_S", "720")),
            tst_port=env_int("TST_PORT", 8021),
            tst_port_step=env_int("POOL_TESTER_PORT_STEP", 100),
            ccrole=e("POOL_CCROLE", str(HERE / "ccrole.sh")),
            render=e("POOL_RENDER", f"python3 {HERE / 'ccloop_render.py'}"),
            gh=e("POOL_GH", e("CLAIM_GH", "gh")),
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
                # open issues blocking this one through GitHub's own dependencies
                "blocked_by": (n.get("issueDependenciesSummary") or {}).get("blockedBy", 0),
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


STAGES = ("stage:triage", "stage:ready", "stage:doing", "stage:pr", "stage:live", "stage:owner")


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
    out = subprocess.run(
        [
            cfg.gh,
            "api",
            "graphql",
            "-f",
            f"query={QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"graphql: {out.stderr.strip()[-300:]}")
    return normalize(json.loads(out.stdout))


def signature(st: dict) -> str:
    """ccwake_sig.py's idea, for the whole queue: a hash of what the units read."""
    items = [f"main@{st['main']}"]
    items += [
        f"o{i['number']}@{i['updated']}:{','.join(i['labels'])}:{i.get('blocked_by', 0)}" for i in st["open"]
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


def blocked(issue: dict, open_numbers: set[int]) -> bool:
    """An open blocker, a GitHub dependency or a `Blocked by:` line. claim.py refuses
    both too, but leaving the native ones to it started a unit per issue per poll that
    it only refused."""
    if issue.get("blocked_by"):
        return True
    for m in claimlib.BLOCKED_LINE.finditer(issue.get("body") or ""):
        if any(int(x) in open_numbers for x in claimlib.NUM.findall(m.group(1))):
            return True
    return False


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
        self.rebase_failed: dict[int, str] = {}  # PR -> head whose auto-rebase failed
        self.merger_runs: collections.Counter = collections.Counter()  # PR -> merger sessions since its last merge or hand-back
        self.paused: dict[str, float] = {}  # role -> until (crash breaker)
        self.quota_until = 0.0  # no new agent sessions before this (model quota errors)
        self.quota_streak = 0  # pauses in a row without a session ending ok
        self.live_entry: dict[int, str] = {}  # issue -> updatedAt when it entered stage:live
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

    def why_not(self, u: Unit, cfg: Config) -> str:
        if u.family in self.running:
            return "running"
        if u.key in self.clean:
            return "done"
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
            if blocked(i, open_numbers) or (
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
        elif "stage:live" in labels and cfg.live:
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
    seen_prs = set()
    for pr in sorted(st["prs"], key=lambda p: p["number"]):
        seen_prs.add(pr["number"])
        n = pr_issue(pr)
        issue = open_by.get(n) if n is not None else None
        if pr["draft"] or issue is None:
            continue
        labels = set(issue["labels"])
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
        if state == "MERGEABLE":
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
        u.role in ("worker", "merger") and not book.why_not(u, cfg) for u in units
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
        due_agents = sum(1 for u in units if u.role != "auto" and not book.why_not(u, cfg))
        if cfg.global_cap and cfg.global_cap - agents - due_agents >= cfg.global_cap / 2:
            units.append(Unit("tester", "spec", f"spec:{ux_sha[:12]}:{book.extra_runs}", "tst:spec", None,
                              (2, 0, 1), {"sha": ux_sha, "deployed": bool(cfg.deploy_cmd), "spare": True}))

    units.sort(key=lambda u: u.rank)
    due, stuck = [], []
    for u in units:
        why = book.why_not(u, cfg)
        if not why:
            due.append(u)
        elif u.kind != "ux" and (why in ("tries", "family") or (why == "done" and u.kind != "live")):
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
                  wrap_min: int = 35, issue: str = "", files: str = "") -> str:
    n, token = c["issue"], c["token"]
    live_text = ", labelled needs:live: write Refs #%s, not Fixes" % n if live and c.get("needs_live") else ""
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
Your paths: worktree /tmp/wrk-{k}-{n}-wt on branch {branch}, scratch files /tmp/wrk-{k}-{n}-*.
{PROJECT} says how to set up a worktree, run the app and where tests go. Put your tests
in new files named for the issue (such as test_issue_{n}.py in the right test directory);
never append to a shared test file (that is what makes pull requests conflict).

End in one of two states:
1. A pull request for #{n} is open (or updated), in the PR format of your skill, with
   its sweep record written at its head, and #{n} moved to stage:pr with stage:doing
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
1. Rebase onto origin/main in /tmp/mrg-{pr}-wt and resolve the conflicts so that both this
   change and what landed on main survive ({PROJECT} says how to set up a worktree).
   Then run `python3 .claude/hooks/sweep_locked.py --update-pr {pr}` there and push with
   --force-with-lease.
2. Run the review gate on the new head:
   `python3 "$SHARED_REPO_PATH/.claude/hooks/review_gate.py" --pr {pr}`. Exit 1: it rejected
   the PR and already handed it back; stop. Exit 2: run it again.
3. Stop there, with #{n} still at stage:pr: do not merge. The supervisor merges the new
   head on its next poll, after checking it merged with the current main and running the
   merge gates, so every merge goes through the same checks. If main moved and the PR
   conflicts again, it starts another merger session (after three, a worker gets it).
4. Hand back only when the rebase needs a decision about behavior, or the sweep is red
   after a clean rebase: comment on the PR naming the hunk or the test, and swap stage:pr
   for stage:triage on #{n}. Once per PR.

Do not read, rebase or merge any other pull request.
Budget: hard wall-clock timeout of {timeout}."""


def ux_journeys_path() -> str:
    """Where a tester keeps its own journeys when the project has none: per repository,
    so they neither leak into another loop on the machine nor come from one."""
    return f"/tmp/issuelooper-{repo_root().name}/ux/journeys.py"


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
        f"command uses; scratch files and any app database under /tmp/tst-{port}-*). Stop every "
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
            "thread what you saw or that the rig cannot reach it."
            + (
                f" Only a check of what is on main now counts: #{u.number} entered stage:live at "
                f"{u.info['since']}, so a live-verified: line older than that is stale."
                if u.info.get("since")
                else ""
            )
            + rules
        )
    sha = u.info["sha"]
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
                f"/tmp/tst-{port}-probes.py)")
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


def review_reason(head: str) -> str:
    """The review gate's reason for its verdict on HEAD, from its record ('' if none)."""
    d = pathlib.Path(os.environ.get("REVIEW_DIR", "") or "/tmp/issuelooper-review")
    try:
        return str(json.loads((d / f"{head}.json").read_text()).get("reason") or "")[:300]
    except (OSError, ValueError):
        return ""


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


SOURCE_EXT = (".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".scss", ".html", ".vue", ".svelte",
              ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".swift", ".cs")


def is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (path.startswith(("tests/", "test/")) or "/tests/" in path or "/test/" in path
            or name.startswith("test_") or ".test." in name or ".spec." in name
            or name.rsplit(".", 1)[0].endswith("_test"))


def is_source(path: str) -> bool:
    prefixes = [x.strip() for x in os.environ.get("REVIEW_PATHS", "src/").split(",") if x.strip()]
    return (path.endswith(SOURCE_EXT) and not is_test(path)
            and (not prefixes or path.startswith(tuple(prefixes))))


# The monitor's message when the queue changed; POOL_MONITOR_FEED names a file to use instead.
FEED_HEADER = (
    "Pool update: the queue changed. Use the issuelooper-monitor skill and run its pass over "
    "what changed: triage the untriaged issues, re-rank the queue, hold or close low-value work, "
    "fix dependencies, act on stuck or failed work, and raise the owner's decisions at "
    "stage:owner. If nothing needs you, say so in one line. End with the owner's decisions, "
    "if any, as one question."
)

SESSION = (
    'set -o pipefail; "$@" 2>>"$POOL_ERR" | tee -a "$POOL_RAW" | $POOL_RENDER_CMD >> "$POOL_LOG"; '
    'exit "${PIPESTATUS[0]}"'
)


# Session outcomes that put the claim back without counting a failed attempt.
NOT_A_FAILURE = ("startup crash", "quota error", "stopped with the supervisor")


class Session(threading.Thread):
    def __init__(self, pool: "Pool", unit: Unit, seq: int):
        super().__init__(daemon=True, name=f"{unit.role}-{seq}")
        self.pool, self.unit, self.seq = pool, unit, seq
        self.proc: subprocess.Popen | None = None
        self.started = time.time()
        self.port = 0
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
        # The CLI's own limit leaves room for the clocks to stand still (below); the
        # supervisor enforces the rest.
        cmd = base + cli.first_turn(prompt, hard - start + cfg.retry_pause_max_s)
        with open(self.dir / "wrapper.err", "w") as err:
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
                    if conv and stall_k < 2 and hard - time.time() > 120:
                        stall_k += 1
                        resume(conv, f"no step for {stall_s:.0f} s after the last one finished", (
                            f"Your session stopped responding for {stall_s / 60:.0f} minutes after its last "
                            "step and was restarted. Continue the task from where you stopped. If a server or "
                            "command you started is still running, stop it by its port or PID first."
                        ), max(120, hard - time.time()))
                        continue
                    wait = quota_backoff(quota_k + 1, cfg.quota_wait_s, cfg.quota_wait_max_s)
                    conv = self.quota_stalled(quiet=wait)
                    if conv and quota_k < cfg.quota_resumes and hard - time.time() > 120:
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
                    if time.time() + wait < hard - 120:
                        quota_k += 1
                        with open(self.dir / "resumes.log", "a") as rl:
                            rl.write(f"{time.time():.0f} quota wait {wait:.0f} s before resume {quota_k}\n")
                        time.sleep(wait)
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
                     or elapsed >= seconds(timeout) + 900 + paused)
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
                                   issue_text(thread) if thread else "", files)
        elif u.role == "merger":
            env.update(MERGE_PR=str(u.number))
            prompt = merger_prompt(u, cfg.timeouts["merger"])
        else:
            self.port = self.pool.take_port()
            env.update(TST_PORT=str(self.port))
            prompt = tester_prompt(u, self.port, cfg.timeouts["tester"], cfg.ux_journeys)

        rc, elapsed, timed_out = self.session(prompt, env)

        if self.pool.stopping:
            # The owner stopped the loop: the claim goes back without a failed attempt,
            # so the next start picks the issue up at once instead of after CLAIM_STALE_MIN.
            outcome = "stopped with the supervisor"
        elif rc != 0 and self.ended_on_quota():
            # Not the agent's failure: the claim goes back without a failed attempt, and
            # the pool holds new starts for a while (Pool.finished).
            outcome = f"quota error (exit {rc})"
        elif timed_out:
            outcome = f"timed out after {cfg.timeouts[u.role]}"
        elif rc != 0 and elapsed < 120:
            outcome = f"startup crash (exit {rc})"
        elif rc != 0:
            outcome = f"failed (exit {rc})"
        else:
            outcome = "ok"

        if u.role == "worker":
            self.save_wip(f"/tmp/wrk-{self.seq}-*-wt")
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
            self.cleanup(f"/tmp/wrk-{self.seq}-*-wt")
        elif u.role == "merger":
            self.cleanup(f"/tmp/mrg-{u.number}-wt")
        return outcome

    # --- units without an agent ---
    def gh(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.pool.cfg.gh, *args], input=stdin, capture_output=True, text=True, timeout=120,
            cwd=repo_root(),
        )

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
        if r.returncode == 1:
            self.pool.log("handback", pr=pr, issue=n, by="review", reason=review_reason(head))
            return "rejected by the review gate (handed back)"
        if r.returncode != 0:
            return f"failed (review gate exit {r.returncode})"
        command = f"gh pr merge {pr} --squash --delete-branch --match-head-commit {head}"

        def gates() -> dict:
            g = subprocess.run(
                shlex.split(self.pool.cfg.guard_cmd),
                input=json.dumps({"tool_input": {"command": command}, "session_id": "ccpool"}),
                env=env, cwd=repo_root(), capture_output=True, text=True, timeout=300,
            )
            self.note(f"merge gates: {g.stdout.strip()[-800:]} {g.stderr.strip()[-300:]}")
            try:
                return json.loads(g.stdout.strip().splitlines()[-1]) if g.stdout.strip() else {}
            except ValueError:
                return {}

        with self.pool.merge_lock:
            # A head swept on an older main says nothing about the merged result: one PR
            # moved an import, another (swept before it) still used it, GitHub saw no
            # conflict, and main stopped starting. A rebase and full sweep per merge would
            # serialize the merges (most were behind main), so a quick check runs on the
            # head merged with the current main instead. It runs under the lock, right
            # before the merge: a check made before the review went stale when other
            # merges landed meanwhile, and main broke three times in one run that way.
            if self.pool.cfg.fast_check_cmd:
                held = self.merged_check(pr, n, head, u.info.get("branch") or "", env)
                if held:
                    return held
            verdict = gates()
            if verdict.get("decision") in ("block", "deny") and re.search(
                r"(?i)sweep", str(verdict.get("reason") or "")
            ) and self.resweep(pr, u.info.get("branch") or "", head, env):
                # A missing or stale sweep record is mechanical: sweep the head here and
                # ask the gates again before handing anything back (most merge-gate
                # hand-backs were a missing sweep line or record).
                verdict = gates()
            if verdict.get("decision") in ("block", "deny"):
                reason = str(verdict.get("reason") or "")
                if "shared checkout" in reason.lower():
                    self.pool.event(f"auto-merge of PR #{pr} refused by Gate 0 (shared checkout): {reason[:300]}")
                    return "failed (gate 0: shared checkout)"
                self.hand_back(pr, n, "**Merge gates: refused.** Not merged.\n\n" + reason[:2500])
                gate = re.search(r"(?i)\bgate\s*(\d)", reason)
                self.pool.log("handback", pr=pr, issue=n, by=f"gate {gate.group(1)}" if gate else "gates",
                              reason=reason.strip().splitlines()[0][:200] if reason.strip() else "")
                return "refused by the merge gates (handed back)"
            m = self.gh("pr", "merge", str(pr), "--squash", "--delete-branch", "--match-head-commit", head)
            self.note(f"gh pr merge exit {m.returncode}: {(m.stdout + m.stderr).strip()[-400:]}")
            if m.returncode != 0:
                return f"merge refused by GitHub (exit {m.returncode})"
        args = ["issue", "edit", str(n), "--remove-label", "stage:pr"]
        if u.info.get("needs_live"):
            args += ["--add-label", "stage:live"]
        self.gh(*args)
        self.gh(
            "issue", "comment", str(n), "--body",
            (f"<!-- merger -->\nMerged PR #{pr} (head {head[:8]}): the review gate and the merge "
             "gates passed, so the supervisor landed it. "
             + ("It stays open at stage:live for the tester's live check." if u.info.get("needs_live")
                else "The supervisor deploys main next." if self.pool.cfg.deploy_cmd else "")).strip(),
        )
        self.note_large_files(pr)
        return "ok"

    def fast_check(self, wt: str, env: dict) -> tuple[bool, str]:
        """POOL_FAST_CHECK_CMD in a worktree: (passed, the tail of its output)."""
        cfg = self.pool.cfg
        try:
            r = subprocess.run(["bash", "-c", cfg.fast_check_cmd], cwd=wt, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=cfg.fast_check_s)
        except subprocess.TimeoutExpired:
            return False, f"the fast check timed out after {cfg.fast_check_s:.0f} s"
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
        wt = f"/tmp/fresh-{pr}"
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)
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
            if ok:
                return ""
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
            return "breaks main once merged (handed back)"
        finally:
            subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)

    def main_ok(self, main: str, env: dict) -> bool:
        """fast_check on main itself, once per sha."""
        cache = self.pool.main_checks
        if main not in cache:
            root, wt = str(repo_root()), f"/tmp/fresh-main-{main[:12]}"
            subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)
            try:
                if self.git(root, "worktree", "add", "-q", "--detach", wt, main).returncode != 0:
                    return True
                cache[main] = self.fast_check(wt, env)[0]
            finally:
                subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)
        return cache[main]

    def behind_main(self, head: str, branch: str) -> bool:
        """Whether origin/main has commits the PR head lacks. Unknown counts as not behind."""
        root = str(repo_root())
        for attempt in range(3):  # other units fetch into the same .git (ref lock)
            f = self.git(root, "fetch", "-q", "origin", "main", *([branch] if branch else []))
            if f.returncode == 0:
                break
            time.sleep(2 + 2 * attempt)
        return self.git(root, "merge-base", "--is-ancestor", "origin/main", head).returncode == 1

    def resweep(self, pr: int, branch: str, head: str, env: dict) -> bool:
        """Run the sweep at the PR's head in a worktree and update the PR body
        (sweep_locked.py --update-pr). True when it ended green at that head."""
        if not branch:
            return False
        root, wt = str(repo_root()), f"/tmp/mrg-{pr}-sweep"
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)
        try:
            self.git(root, "fetch", "-q", "origin", branch)
            if self.git(root, "worktree", "add", "-q", "--detach", wt, head).returncode != 0:
                return False
            r = subprocess.run(
                [sys.executable, str(HERE / "sweep_locked.py"), "--update-pr", str(pr)],
                cwd=wt, env=env, capture_output=True, text=True, timeout=2400,
            )
            self.note(f"resweep exit {r.returncode}: {(r.stdout + r.stderr).strip()[-400:]}")
            return r.returncode == 0 and "green" in r.stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)

    def note_large_files(self, pr: int) -> None:
        """Measure the source files this PR touched on main; report the long ones. Source
        files are those under REVIEW_PATHS (the production paths, default src/)."""
        files = self.gh("pr", "view", str(pr), "--json", "files", "-q", ".files[].path").stdout.split()
        root = str(repo_root())
        self.git(root, "fetch", "-q", "origin", "main")
        large = {}
        for f in files:
            if not is_source(f):
                continue
            text = self.git(root, "show", f"origin/main:{f}").stdout
            if text.count("\n") > self.pool.cfg.split_lines:
                large[f] = text.count("\n")
        if large:
            self.pool.record_large(large)

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
        wt = f"/tmp/mrg-{pr}-auto"
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)
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
                    self.pool.record_conflicts(pr, files_seen)
                    self.note(f"left to a merger: conflicts in {', '.join(bad)}")
                    return "left to a merger (a hunk changes existing lines)"
                for f in conflicted:
                    self.git(wt, "add", f, check=True)
                r = self.git(wt, *diff3, "rebase", "--continue", env=gitenv)
            self.pool.record_conflicts(pr, files_seen)
            sweep = subprocess.run(
                [sys.executable, str(HERE / "sweep_locked.py"), "--update-pr", str(pr)],
                cwd=wt, env=env, capture_output=True, text=True, timeout=2400,
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
            subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt], capture_output=True)
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
        self.gh("pr", "comment", str(pr), "--body", f"<!-- merger -->\n{text}\n\n"
                "Fix this on this branch, re-sweep, push and update the PR body; the supervisor "
                "tries the merge again on the new head.")
        self.gh("issue", "edit", str(n), "--remove-label", "stage:pr", "--add-label", "stage:triage")

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
        """Remove the session's worktrees, rescuing unpushed work to /tmp/<wt>-rescued.diff."""
        root = str(repo_root())
        for wt in glob.glob(pattern):
            git = ["git", "-C", wt]
            subprocess.run(git + ["add", "-A", "-N", "."], capture_output=True)
            dirty = subprocess.run(
                git + ["status", "--porcelain"], capture_output=True, text=True
            ).stdout
            ahead = subprocess.run(
                git + ["log", "--oneline", "@{u}..HEAD"], capture_output=True, text=True
            )
            if ahead.returncode != 0:
                ahead = subprocess.run(
                    git + ["log", "--oneline", "origin/main..HEAD"], capture_output=True, text=True
                )
            if dirty.strip() or ahead.stdout.strip():
                diff = subprocess.run(
                    git + ["diff", "origin/main"], capture_output=True, text=True
                ).stdout
                pathlib.Path(f"/tmp/{pathlib.Path(wt).name}-rescued.diff").write_text(diff)
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
        self.main_check_last: dict = {}  # {sha, ok, t, failing}: the last full check that finished
        self.main_green = ""  # the last main whose full check was green
        self.red_issue: int | None = None  # the open `main is red` issue
        self.red_looked = False  # whether GitHub was asked for one since this supervisor started
        self.red_failing: tuple = ()  # the failing tests last reported on it
        self.own_deployed = ""
        self.ux_scores: list[dict] = []  # {t, sha, score} per UX run, score None when its report had none
        self.main_seen: dict[str, float] = {}
        self.stopping = False
        self.clock = time.time
        cfg.logdir.mkdir(parents=True, exist_ok=True)
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

    def record_conflicts(self, pr: int, files) -> None:
        now = self.clock()
        with self.lock:
            self.book.conflicts += [(now, f, pr) for f in files]
        self.split_hot_files(now)

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
                "<!-- merger -->\n"
                f"`{f}` {why}.\n\n"
                f"Split `{f}` into smaller files along its existing seams (one module per "
                "component or concern; long functions into named helpers), with no behavior "
                "change. Keep every public name and every hook the tests use working, and keep "
                "the sweep green. Add unit tests for the extracted pieces in a new test file and "
                "record them as Fail-before (a new file counts from zero). Do nothing else in "
                "this PR.\n\n"
                "This is checked from the repository, not from the app: in the PR body list "
                "every new file with its line count, the old file's line count before and "
                "after, and the output of `git diff --stat origin/main...HEAD`."
            )
            r = subprocess.run(
                [self.cfg.gh, "issue", "create", "--title", f"Split {f}: it is where parallel work collides",
                 "--label", "P1", "--label", "stage:triage", "--body", body],
                capture_output=True, text=True, timeout=120, cwd=repo_root(),
            )
            self.log("split_filed", file=f, prs=len(prs.get(f, ())), lines=b.large.get(f), ok=r.returncode == 0)
            self.event(f"filed a split issue for {f}: {r.stdout.strip()}")
        return due

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
            if u.role == "tester" and u.kind == "live" and outcome == "ok":
                # A finished live check needs the monitor's close or hand-back.
                self.events.append(
                    f"live check of #{u.number} finished: read the tester's comment on #{u.number} and close the issue "
                    "if it passed, or hand it back if it failed"
                )
            if outcome not in ("ok", "unclaimable") and not outcome.startswith("quota error"):
                self.events.append(
                    f"{u.role} session {w.seq} on {u.kind} {self.label(u)} {outcome} (log {w.dir})"
                )
        if outcome.startswith("quota error"):
            self.quota_seen(u.role)
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
        first = self.main_seen.setdefault(main, now)
        mergers = self.book.role_counts()["merger"]
        if mergers and now - first < self.cfg.deploy_grace_s:
            return
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
        return subprocess.run([self.cfg.gh, *args], capture_output=True, text=True, timeout=120,
                              cwd=repo_root())

    def main_check_due(self, st: dict):
        """Start the full check of the newest origin/main, unless one is running. The
        merged-tree check runs only the quick command, so a semantic conflict in the
        slower tests reached main twice in one run and nobody noticed for half an hour."""
        main = st.get("main", "")
        with self.lock:
            if not self.cfg.main_check or not main or self.main_check_running or main == self.main_check_sha:
                return
            self.main_check_running, self.main_check_sha = True, main
        threading.Thread(target=self.check_main, args=(main,), daemon=True).start()

    def check_main(self, sha: str):
        try:
            ok, out = self.sweep_main(sha)
            self.main_checked(sha, ok, out)
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
        wt = f"/tmp/issuelooper-main-{sha[:12]}"

        def git(*a):
            return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=300)
        git("worktree", "remove", "--force", wt)
        try:
            for attempt in range(3):  # other units fetch into the same .git (ref lock)
                if git("fetch", "-q", "origin", "main").returncode == 0:
                    break
                time.sleep(2 + 2 * attempt)
            if git("worktree", "add", "-q", "--detach", wt, sha).returncode != 0:
                return None, f"could not add a worktree at {sha[:12]}"
            r = subprocess.run([sys.executable, str(HERE / "sweep_locked.py")], cwd=wt, env=dict(os.environ),
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3600)
            out = r.stdout + r.stderr
            if r.returncode == 0 and "green" in r.stdout:
                return True, out
            # 1 is a red sweep; 2 and 3 mean it did not run (no runner, no slot).
            return (False if r.returncode == 1 else None), out
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, str(exc)
        finally:
            git("worktree", "remove", "--force", wt)

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
            self.main_check_last = {"sha": sha, "ok": ok, "t": round(time.time(), 1), "failing": failing[:5]}
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
        self.report_red(sha, failing, out)

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
            "Merges go on; fix this first.\n\nFailing:\n"
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
                   "Merges go on; the fix is P0")

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
                    continue  # a proposal waiting for the owner; not for the monitor to triage
                if old is not None and IGNORED & set(old["labels"]):
                    self.event(f"#{n} is no longer ux:proposed (the owner accepted it): triage it: {i['title']}")
                    continue
                if old is None and n in was_closed:
                    self.event(f"#{n} was reopened: its newest comments say why: {i['title']}")
                elif old is None:
                    self.event(f"new issue #{n}: {i['title']}")
                elif "stage:owner" in labels and "stage:owner" not in old["labels"]:
                    self.event(f"#{n} moved to stage:owner (needs a decision): {i['title']}")
                elif "stage:owner" in labels and i.get("comments", 0) > old.get("comments", 0):
                    # The owner answers stage:owner on the issue; say so, or the monitor keeps asking.
                    self.event(f"#{n} (stage:owner) has a new comment: read it with `gh issue view {n} --comments`; "
                               "it may be the owner's answer")
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
                self.event(
                    f"stuck: {u.role} unit {u.kind} {self.label(u)} is still due after "
                    f"{self.book.tries[u.key]} session(s) ({self.book.why_not(u, self.cfg)})"
                )
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
        return (
            f"Untriaged (yours): {', '.join(untriaged) or 'none'}. "
            f"Queue: {len(st['open'])} open ({stages}); {len(st['prs'])} open PRs; recently closed "
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
            if "stage:live" in i["labels"]:
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
        with self.lock:
            self.track_live(st)
            due, stuck = plan(st, self.book, self.cfg, now, self.ux_target(st))
            started = admit(due, self.book, self.cfg, now)
        for u in started:
            self.start(u)
        self.observe(st, now, started, stuck)
        self.prev = st
        if self.hurry_pr is not None and not any(
            p["number"] == self.hurry_pr and p["mergeable"] == "UNKNOWN" for p in st["prs"]
        ):
            self.hurry_pr = None
        self.deploy_check(st, now)
        self.main_check_due(st)
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

    def stop(self, *_):
        self.stopping = True
        self.wake.set()
        for w in list(self.sessions.values()):
            w.kill()


def cmd_run(_a) -> int:
    os.environ.setdefault("SHARED_REPO_PATH", str(repo_root()))
    cfg = Config.from_env()
    # Sessions and the review gate find the pool's logs here.
    os.environ["POOL_LOGDIR"] = str(cfg.logdir)
    pool = Pool(cfg)
    signal.signal(signal.SIGTERM, pool.stop)
    signal.signal(signal.SIGINT, pool.stop)
    pool.run()
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
    p = sub.add_parser("probe")
    p.add_argument("role", choices=ROLES)
    p.add_argument("--prompt", default="Report your CC_ROLE and the skills you can see, then stop.")
    a = ap.parse_args(argv)
    return {"run": cmd_run, None: cmd_run, "plan": cmd_plan, "probe": cmd_probe}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
