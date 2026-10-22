#!/usr/bin/env python3
"""Race-safe issue claims for the worker pool.

The claim lives where everything else in this loop lives: on GitHub. One claim is
one label edit and one signed comment:

    gh issue edit N --add-label stage:doing --remove-label stage:triage,stage:ready
    <!-- worker -->
    <!-- claim loop=K token=T from=stage:ready -->

and then a re-read of the thread. The label is the queue state everyone reads;
the comments decide a race. Among the claim comments that no later release
comment cancels, the one with the lowest comment id holds the issue. A loop that
finds an earlier active claim than its own deletes its comment and tries the next
candidate. Comment ids are assigned in commit order, so two loops cannot both see
themselves as first once both writes are readable; `check` repeats the test later
(the worker runs it before opening the PR) for the replica-lag window.

    claim.py candidates                       the pick order, nothing written
    claim.py claim --loop K [--claim-file F]  claim the best candidate; JSON on stdout
                                              exit 0 claimed, 3 nothing claimable
    claim.py claim --loop K --issue N         claim issue N only (the supervisor picks;
                                              exit 3 if N is not claimable)
    claim.py check N --token T                exit 0 if T still holds N, 4 if not
    claim.py release N --loop K --token T --reason TEXT [--to stage:owner]
    claim.py finish --claim-file F --loop K   after a session: keep (PR open) or release
    claim.py stale [--release]                stage:doing claims older than STALE_MIN
                                              with no open PR (ccpool.py's reclaim)

Pick order: P0 > P1 > P2 > no priority, then the lowest number. Claimable means
open, `stage:triage` or `stage:ready` (or a stale `stage:doing`), not `on hold` or
`hold:monitor`, not `ux:proposed`, no open blocker (GitHub dependencies or a `Blocked by:` line), and
fewer than MAX_ATTEMPTS failed attempts since the owner last commented on it (or
the monitor posted `<!-- reset-attempts -->`). An issue whose PR was handed back
(open PR, issue back at `stage:triage`) is claimable, and the claim names the PR
so the worker updates it instead of opening a second one.

The writes here go through subprocess, so role_guard never sees them; this file
enforces its own rule instead: only CC_ROLE worker may run the commands that write
(ccpool.py runs them with that role).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import config  # noqa: E402

config.apply()

GH = os.environ.get("CLAIM_GH", "gh")
STALE_MIN = int(os.environ.get("CLAIM_STALE_MIN", "60"))
MAX_ATTEMPTS = int(os.environ.get("CLAIM_MAX_ATTEMPTS", "3"))
SETTLE_S = float(os.environ.get("CLAIM_SETTLE_S", "2"))
JITTER_S = float(os.environ.get("CLAIM_JITTER_S", "3"))
# Claims this much older than yours do not compete with it: they are left over
# from an earlier cycle (a PR the merger later handed back). A live rival claimed
# within seconds of you, because both of you read the issue as claimable.
WINDOW_S = float(os.environ.get("CLAIM_WINDOW_S", "300"))

CLAIMABLE = ("stage:triage", "stage:ready")
DOING = "stage:doing"
HOLDS = {"on hold", "hold:monitor"}
# A UX candidate the tester filed waits for the owner, who swaps the label for
# ux:accepted; until then nobody claims, triages or tests it.
IGNORED = {"ux:proposed"}
STAGES = (
    "stage:triage",
    "stage:ready",
    DOING,
    "stage:pr",
    "stage:owner",
    "stage:live",
)
PRIO = {"P0": 0, "P1": 1, "P2": 2}

EVENT = re.compile(r"<!--\s*(claim|release)\b([^>]*)-->")
PAIR = re.compile(r"(\w+)=([^\s>]+)")
BLOCKED_LINE = re.compile(r"(?im)^\s*\**blocked by:?\**:?\s*(.+)$")
NUM = re.compile(r"#(\d+)")


class GhError(RuntimeError):
    pass


def gh(*args: str, stdin: str | None = None) -> str:
    try:
        done = subprocess.run([GH, *args], input=stdin, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh {' '.join(args[:3])}: {exc}") from exc
    if done.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])}: {done.stderr.strip()[-300:]}")
    return done.stdout


def gh_json(*args: str, stdin: str | None = None):
    out = gh(*args, stdin=stdin)
    return json.loads(out) if out.strip() else None


def paged(path: str) -> list[dict]:
    rows: list[dict] = []
    sep = "&" if "?" in path else "?"
    for page in range(1, 20):
        batch = gh_json("api", f"{path}{sep}per_page=100&page={page}") or []
        rows += batch
        if len(batch) < 100:
            break
    return rows


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def labels_of(issue: dict) -> set[str]:
    return {lab["name"] if isinstance(lab, dict) else str(lab) for lab in issue.get("labels") or []}


def priority(issue: dict) -> int:
    return min((PRIO[n] for n in labels_of(issue) if n in PRIO), default=9)


def stage_of(issue: dict) -> str:
    names = labels_of(issue)
    return next((s for s in STAGES if s in names), "")


def events(comments: list[dict]) -> list[dict]:
    """Claim and release markers in comment-id order (a reclaim carries both)."""
    out = []
    for c in sorted(comments, key=lambda c: c["id"]):
        for m in EVENT.finditer(c.get("body") or ""):
            ev = dict(PAIR.findall(m.group(2)))
            ev.update(kind=m.group(1), id=c["id"], created_at=c.get("created_at", ""))
            out.append(ev)
    return out


def active_claims(comments: list[dict]) -> list[dict]:
    """Claims no later release cancels, earliest first. `token=*` releases all."""
    active: dict[str, dict] = {}
    for ev in events(comments):
        tok = ev.get("token", "")
        if ev["kind"] == "claim" and tok:
            active[tok] = ev
        elif ev["kind"] == "release":
            if tok == "*":
                active.clear()
            else:
                active.pop(tok, None)
    return sorted(active.values(), key=lambda e: e["id"])


def holds(comments: list[dict], token: str) -> tuple[bool, str]:
    """Whether `token` holds the issue, and if not, which token does.

    It holds when it is active and no other active claim has a lower comment id
    and was made within WINDOW_S before it."""
    act = active_claims(comments)
    mine = next((e for e in act if e.get("token") == token), None)
    if mine is None:
        return False, act[-1].get("token", "none") if act else "none"
    t = parse_time(mine["created_at"]) if mine["created_at"] else now()
    for e in act:
        if e["id"] < mine["id"] and e["created_at"] and \
                (t - parse_time(e["created_at"])).total_seconds() <= WINDOW_S:
            return False, e.get("token", "?")
    return True, token


RESET = "<!-- reset-attempts -->"
ROLE_MARK = re.compile(r"<!--\s*(worker|merger|tester|monitor|aide)\s*-->")


def is_reset(c: dict) -> bool:
    """The owner spoke on the thread (a person's comment with no role marker), or the
    monitor reset the count explicitly. Either starts the attempt count again."""
    body = c.get("body") or ""
    if RESET in body:
        return True
    user = c.get("user") or {}
    return user.get("type") == "User" and not ROLE_MARK.search(body)


def failed_attempts(comments: list[dict]) -> int:
    """Failed releases since the latest reset (see is_reset)."""
    n = 0
    for c in sorted(comments, key=lambda c: c["id"]):
        if is_reset(c):
            n = 0
        n += sum(
            1
            for m in EVENT.finditer(c.get("body") or "")
            if m.group(1) == "release" and dict(PAIR.findall(m.group(2))).get("fail") == "1"
        )
    return n


def comments_of(n: int) -> list[dict]:
    return paged(f"repos/{{owner}}/{{repo}}/issues/{n}/comments")


def open_prs() -> list[dict]:
    return paged("repos/{owner}/{repo}/pulls?state=open")


def pr_for(n: int, prs: list[dict]) -> dict | None:
    ref = re.compile(rf"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?|refs?)\s+#{n}\b")
    for pr in prs:
        head = (pr.get("head") or {}).get("ref", "")
        if head == f"issue-{n}" or ref.search(pr.get("body") or ""):
            return pr
    return None


def blockers(issue: dict, open_numbers: set[int]) -> list[int]:
    found: list[int] = []
    summary = issue.get("issue_dependencies_summary") or {}
    if summary.get("blocked_by"):
        found.append(-1)  # GitHub says an open blocker exists; which one does not matter
    for m in BLOCKED_LINE.finditer(issue.get("body") or ""):
        found += [int(x) for x in NUM.findall(m.group(1)) if int(x) in open_numbers]
    return found


def candidates(stale_min: int = STALE_MIN) -> list[dict]:
    """Claimable issues in pick order, each with the PR it already has, if any."""
    issues = [i for i in paged("repos/{owner}/{repo}/issues?state=open") if "pull_request" not in i]
    open_numbers = {i["number"] for i in issues}
    prs = open_prs()
    out = []
    for issue in issues:
        names = labels_of(issue)
        if names & (HOLDS | IGNORED) or blockers(issue, open_numbers):
            continue
        n = issue["number"]
        pr = pr_for(n, prs)
        if names & set(CLAIMABLE):
            pass
        elif DOING in names and pr is None:
            if not is_stale(issue, stale_min):
                continue
        else:
            continue
        out.append({"issue": issue, "pr": pr})
    out.sort(key=lambda c: (priority(c["issue"]), c["issue"]["number"]))
    return out


def is_stale(issue: dict, stale_min: int, comments: list[dict] | None = None) -> bool:
    """A stage:doing claim older than stale_min minutes. The caller has checked for a PR."""
    limit = stale_min * 60
    updated = issue.get("updated_at")
    if updated and (now() - parse_time(updated)).total_seconds() <= limit:
        return False  # the claim comment itself bumps updated_at; skip the read
    comments = comments_of(issue["number"]) if comments is None else comments
    act = active_claims(comments)
    if act:
        born = parse_time(act[-1]["created_at"]) if act[-1]["created_at"] else now()
    else:
        born = parse_time(issue.get("updated_at") or now().isoformat())
    return (now() - born).total_seconds() > limit


def need_role(*roles: str) -> None:
    role = os.environ.get("CC_ROLE", "").strip().lower()
    if role not in roles:
        sys.exit(
            f"claim.py: CC_ROLE is {role or '<unset>'!r}; this command is for {', '.join(roles)}"
        )


def post_comment(n: int, body: str) -> dict:
    return (
        gh_json(
            "api",
            "-X",
            "POST",
            f"repos/{{owner}}/{{repo}}/issues/{n}/comments",
            "--input",
            "-",
            stdin=json.dumps({"body": body}),
        )
        or {}
    )


def edit_labels(n: int, add: list[str], remove: list[str]) -> None:
    args = ["issue", "edit", str(n)]
    for a in add:
        args += ["--add-label", a]
    if remove:
        args += ["--remove-label", ",".join(remove)]
    gh(*args)


def try_claim(cand: dict, loop: str) -> dict | None:
    """One claim attempt on one candidate. Returns the claim, or None if lost; then
    cand["lost"] says what failed."""
    issue = cand["issue"]
    n = issue["number"]
    frm = stage_of(issue)
    token = uuid.uuid4().hex[:10]
    comments = comments_of(n)
    attempts = failed_attempts(comments)
    if attempts >= MAX_ATTEMPTS:
        cand["lost"] = f"{attempts} failed attempts since the last reset (limit {MAX_ATTEMPTS})"
        return None
    stale = active_claims(comments) if frm == DOING else []
    if frm == DOING:
        frm = stale[-1].get("from", "stage:triage") if stale else "stage:triage"
        if frm not in CLAIMABLE:
            frm = "stage:triage"
    remove = [s for s in (*CLAIMABLE, "stage:owner") if s in labels_of(issue)]
    edit_labels(n, [DOING], remove)
    pre = "".join(f"<!-- release token={e['token']} to={DOING} -->\n" for e in stale)
    body = (
        "<!-- worker -->\n"
        f"{pre}<!-- claim loop={loop} token={token} from={frm} -->\n"
        f"Claimed by worker loop {loop}"
        + (f" (the previous claim was stale: no PR after {STALE_MIN} min)" if stale else "")
        + ". It is released automatically if the loop ends without a pull request."
    )
    mine = post_comment(n, body)
    time.sleep(SETTLE_S)
    after = comments_of(n)
    ok, other = holds(after, token)
    if not ok:
        # Two different failures: a rival's earlier claim, or GitHub's read not showing
        # this claim's own comment yet. Both were reported as "another worker claimed it
        # first", which sent the owner looking for a worker that did not exist.
        if any(e.get("token") == token for e in active_claims(after)):
            cand["lost"] = f"another worker claimed it first (claim {other})"
        else:
            cand["lost"] = ("its own claim comment was not in GitHub's re-read of the thread "
                            f"{SETTLE_S:.0f} s later (a read that lagged behind the write)")
        if mine.get("id"):
            try:
                gh("api", "-X", "DELETE", f"repos/{{owner}}/{{repo}}/issues/comments/{mine['id']}")
            except GhError:
                pass
        return None
    pr = cand.get("pr")
    return {
        "issue": n,
        "title": issue.get("title", ""),
        "token": token,
        "loop": loop,
        "from": frm,
        "needs_live": "needs:live" in labels_of(issue),
        "priority": next((p for p in PRIO if p in labels_of(issue)), ""),
        "pr": pr and {"number": pr["number"], "branch": (pr.get("head") or {}).get("ref", "")},
        "claimed_at": now().isoformat(),
    }


def cmd_candidates(_args) -> int:
    for c in candidates():
        i = c["issue"]
        pr = f"\tPR #{c['pr']['number']}" if c["pr"] else ""
        print(f"P{priority(i)}\t#{i['number']}\t{stage_of(i)}\t{i['title']}{pr}")
    return 0


def why_not(n: int, unseen: bool = False) -> str:
    """Why issue n is not claimable now, for the supervisor's log. Without it a refused
    claim leaves no reason anywhere, and repeated refusals cannot be explained. unseen:
    the list of open issues claim.py picked from did not have n."""
    try:
        issue = gh_json("api", f"repos/{{owner}}/{{repo}}/issues/{n}") or {}
    except GhError as exc:
        return f"the issue could not be read ({exc})"
    if issue.get("state") != "open":
        return "it is closed"
    names = labels_of(issue)
    if names & (HOLDS | IGNORED):
        return f"it is held ({', '.join(sorted(names & (HOLDS | IGNORED)))})"
    open_numbers = {i["number"] for i in paged("repos/{owner}/{repo}/issues?state=open")}
    found = blockers(issue, open_numbers)
    if found:
        named = [f"#{b}" for b in found if b > 0]
        return "it is blocked by " + (", ".join(named) or "an open GitHub dependency")
    stage = stage_of(issue)
    if stage == DOING:
        if pr_for(n, open_prs()):
            return "it is at stage:doing with an open pull request"
        return f"it is at stage:doing and the claim is not stale yet ({STALE_MIN} min)"
    if stage not in CLAIMABLE:
        return f"it is at {stage or 'no stage'}, not {' or '.join(CLAIMABLE)}"
    attempts = failed_attempts(comments_of(n))
    if attempts >= MAX_ATTEMPTS:
        return f"{attempts} failed attempts since the last reset (limit {MAX_ATTEMPTS})"
    if unseen:
        # A worker started 1-3 s after an issue was filed: GitHub's list of open issues
        # did not have it yet, though the issue itself reads as claimable.
        return ("it was not in GitHub's list of open issues when claim.py read it, though it "
                "reads as claimable now (a new issue can take a few seconds to appear there)")
    return "no reason found: it reads as claimable now (a read that lagged behind GitHub?)"


def cmd_claim(args) -> int:
    need_role("worker")
    if JITTER_S > 0:
        time.sleep(random.uniform(0, JITTER_S))
    tried: set[int] = set()
    lost = ""  # what failed in the last claim attempt
    for _ in range(args.tries):
        pool = [c for c in candidates() if c["issue"]["number"] not in tried]
        if args.issue is not None:
            # ccpool.py already chose the issue from its own snapshot; this
            # re-checks it against GitHub (holds, blockers, attempts) and claims only it.
            pool = [c for c in pool if c["issue"]["number"] == args.issue][:1]
        if not pool:
            break
        cand = pool[0]
        tried.add(cand["issue"]["number"])
        claim = try_claim(cand, args.loop)
        if claim:
            text = json.dumps(claim)
            if args.claim_file:
                pathlib.Path(args.claim_file).write_text(text + "\n")
            print(text)
            return 0
        lost = cand.get("lost", "")
    if args.issue is not None:
        # A lost attempt says itself what failed: a read made after it would find the
        # label it set (stage:doing) and blame that.
        why = lost or why_not(args.issue, unseen=args.issue not in tried)
        print(f"claim.py: #{args.issue} is not claimable: {why}", file=sys.stderr)
    return 3


def cmd_check(args) -> int:
    ok, other = holds(comments_of(args.issue), args.token)
    print("held" if ok else f"lost: the issue is held by claim {other}")
    return 0 if ok else 4


def release(
    n: int, loop: str, token: str, reason: str, to: str, fail: bool, role: str = "worker"
) -> None:
    issue = gh_json("api", f"repos/{{owner}}/{{repo}}/issues/{n}") or {}
    names = labels_of(issue)
    if DOING in names:
        edit_labels(n, [to], [DOING])
    who = f" loop={loop}" if loop else ""
    post_comment(
        n,
        (
            f"<!-- {role} -->\n"
            f"<!-- release{who} token={token} to={to}{' fail=1' if fail else ''} -->\n"
            f"Claim released ({to}): {reason}"
        ),
    )


def cmd_release(args) -> int:
    need_role("worker")
    to, reason = args.to, args.reason
    if to != "stage:owner" and failed_attempts(comments_of(args.issue)) + 1 >= MAX_ATTEMPTS:
        to = "stage:owner"
        reason += f"; {MAX_ATTEMPTS} attempts without a PR, so it leaves the pool until the owner or monitor looks"
    release(args.issue, args.loop, args.token, reason, to, fail=True)
    claim_file = os.environ.get("WRK_CLAIM_FILE")
    if claim_file:
        pathlib.Path(claim_file).unlink(missing_ok=True)
    print(f"released #{args.issue} to {to}")
    return 0


def cmd_finish(args) -> int:
    """After a session: the issue has a PR (make sure it says stage:pr), or release it."""
    need_role("worker")
    path = pathlib.Path(args.claim_file)
    try:
        claim = json.loads(path.read_text())
    except (OSError, ValueError):
        print("no claim")
        return 0
    n = claim["issue"]
    issue = gh_json("api", f"repos/{{owner}}/{{repo}}/issues/{n}") or {}
    names = labels_of(issue)
    held = holds(comments_of(n), claim["token"])[0]
    pr = pr_for(n, open_prs())
    if issue.get("state") == "closed":
        outcome = "closed"
    elif pr is not None:
        if held and DOING in names:
            edit_labels(
                n, ["stage:pr"], [s for s in (DOING, *CLAIMABLE, "stage:owner") if s in names]
            )
        outcome = f"pr #{pr['number']}"
    elif held and DOING in names:
        fails = failed_attempts(comments_of(n)) + (0 if args.no_fail else 1)
        to = "stage:owner" if fails >= MAX_ATTEMPTS else claim.get("from") or "stage:triage"
        why = args.reason or "the session ended without a pull request"
        if to == "stage:owner":
            why += f"; {fails} attempts without a PR, so it leaves the pool until the owner or monitor looks"
        release(n, claim["loop"], claim["token"], why, to, fail=not args.no_fail)
        outcome = f"released to {to}"
    else:
        outcome = "released"
    path.unlink(missing_ok=True)
    print(f"#{n}: {outcome}")
    return 0


def cmd_stale(args) -> int:
    if args.release:
        need_role("worker")
    role = os.environ.get("CC_ROLE", "worker").strip().lower() or "worker"
    issues = [
        i
        for i in paged(f"repos/{{owner}}/{{repo}}/issues?state=open&labels={DOING}")
        if "pull_request" not in i
    ]
    prs = open_prs()
    for issue in issues:
        n = issue["number"]
        if pr_for(n, prs) is not None:
            continue
        comments = comments_of(n)
        if not is_stale(issue, args.minutes, comments):
            continue
        act = active_claims(comments)
        frm = act[-1].get("from", "stage:triage") if act else "stage:triage"
        if frm not in CLAIMABLE:
            frm = "stage:triage"
        print(f"stale #{n} (claim {act[-1]['token'] if act else 'none'})")
        if args.release:
            release(
                n,
                "",
                "*",
                f"stale claim: stage:doing for over {args.minutes} min with no pull request",
                frm,
                fail=True,
                role=role,
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("candidates")
    p = sub.add_parser("claim")
    p.add_argument("--loop", required=True)
    p.add_argument("--claim-file")
    p.add_argument("--tries", type=int, default=3)
    p.add_argument("--issue", type=int, default=None, help="claim this issue only")
    p = sub.add_parser("check")
    p.add_argument("issue", type=int)
    p.add_argument("--token", required=True)
    p = sub.add_parser("release")
    p.add_argument("issue", type=int)
    p.add_argument("--loop", default=os.environ.get("WRK_K", ""))
    p.add_argument("--token", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument(
        "--to", default="stage:triage", choices=["stage:triage", "stage:ready", "stage:owner"]
    )
    p = sub.add_parser("finish")
    p.add_argument("--claim-file", required=True)
    p.add_argument("--loop", default=os.environ.get("WRK_K", ""))
    p.add_argument("--reason", default="")
    p.add_argument("--no-fail", action="store_true", help="release without counting a failed attempt")
    p = sub.add_parser("stale")
    p.add_argument("--release", action="store_true")
    p.add_argument("--minutes", type=int, default=STALE_MIN)
    args = ap.parse_args(argv)
    try:
        return {
            "candidates": cmd_candidates,
            "claim": cmd_claim,
            "check": cmd_check,
            "release": cmd_release,
            "finish": cmd_finish,
            "stale": cmd_stale,
        }[args.cmd](args)
    except GhError as exc:
        print(f"claim.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
