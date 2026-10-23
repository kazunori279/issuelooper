#!/usr/bin/env python3
"""Cases for claim.py. Run it: `python3 claim_test.py`.

`gh` is faked by a script on CLAIM_GH that keeps a whole repository's issues,
labels, pull requests and comments in one JSON file under an flock, so several
claim.py processes can race against it the way several workers race
against GitHub. Comment ids are assigned in commit order, as GitHub's are.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
CLAIM = HERE / "claim.py"

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


FAKE_GH = r"""#!/usr/bin/env python3
import datetime as dt, fcntl, json, os, re, sys, time
state_path = os.environ["FAKE_GH_STATE"]
args = sys.argv[1:]
def iso(t=None):
    return (t or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
def issue_view(s, i):
    return {"number": i["number"], "title": i["title"], "body": i.get("body", ""),
            "state": i.get("state", "open"), "updated_at": i["updated_at"],
            "labels": [{"name": n} for n in i["labels"]],
            "issue_dependencies_summary": {"blocked_by": i.get("blocked_by", 0)}}
with open(state_path + ".lock", "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    s = json.load(open(state_path))
    out, write = None, False
    if args[:2] == ["api", "user"]:
        print(os.environ.get("FAKE_GH_LOGIN", "loop-bot"))
        sys.exit(0)
    if args[0] == "api":
        method = "GET"
        if "-X" in args:
            method = args[args.index("-X") + 1]
        path = next(a for a in args[1:] if a.startswith("repos/"))
        path, _, query = path.partition("?")
        q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if q.get("page", "1") != "1":
            out = []
        elif method == "POST" and re.search(r"/issues/(\d+)/comments$", path):
            n = re.search(r"/issues/(\d+)/comments$", path).group(1)
            body = json.loads(sys.stdin.read())["body"]
            s["next_id"] += 1
            c = {"id": s["next_id"], "body": body, "created_at": iso(),
                 "user": {"login": os.environ.get("FAKE_GH_LOGIN", "loop-bot"), "type": "User"},
                 "author_association": "OWNER"}
            s["comments"].setdefault(n, []).append(c)
            s["issues"][n]["updated_at"] = iso()
            s["writes"].append(["comment", int(n)])
            out, write = c, True
        elif method == "DELETE" and re.search(r"/issues/comments/(\d+)$", path):
            cid = int(re.search(r"/issues/comments/(\d+)$", path).group(1))
            for n, cs in s["comments"].items():
                s["comments"][n] = [c for c in cs if c["id"] != cid]
            s["writes"].append(["delete", cid])
            out, write = "", True
        elif re.search(r"/issues/(\d+)/comments$", path):
            out = s["comments"].get(re.search(r"/issues/(\d+)/comments$", path).group(1), [])
            # a read that lags behind the writes: new comments not visible yet
            out = [c for c in out if c["id"] <= int(os.environ.get("FAKE_GH_HIDE_AFTER", "999999999"))]
        elif re.search(r"/issues/(\d+)$", path):
            out = issue_view(s, s["issues"][re.search(r"/issues/(\d+)$", path).group(1)])
        elif path.endswith("/issues"):
            want = q.get("labels")
            out = [issue_view(s, i) for i in s["issues"].values() if i.get("state", "open") == "open"
                   and (not want or want in i["labels"])
                   and str(i["number"]) not in os.environ.get("FAKE_GH_UNLISTED", "").split(",")]
            out += [{"number": p["number"], "pull_request": {}, "labels": []} for p in s["prs"]]
        elif path.endswith("/pulls"):
            out = s["prs"]
        else:
            sys.exit(f"fake gh: unknown path {path}")
    elif args[:2] == ["issue", "edit"]:
        i = s["issues"][args[2]]
        for flag, val in zip(args, args[1:]):
            if flag == "--add-label":
                i["labels"] += [v for v in val.split(",") if v not in i["labels"]]
            if flag == "--remove-label":
                i["labels"] = [x for x in i["labels"] if x not in val.split(",")]
        i["updated_at"] = iso()
        s["writes"].append(["label", int(args[2])])
        out, write = "", True
    else:
        sys.exit(f"fake gh: unknown command {args}")
    if write:
        json.dump(s, open(state_path, "w"))
delay = float(os.environ.get("FAKE_GH_WRITE_DELAY", "0"))
if write and delay:
    time.sleep(delay)
if out not in (None, ""):
    print(json.dumps(out))
"""


def ago(minutes: float) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(n, labels, title=None, body="", blocked_by=0, updated=120, state="open"):
    return {
        "number": n,
        "title": title or f"issue {n}",
        "labels": labels,
        "body": body,
        "blocked_by": blocked_by,
        "updated_at": ago(updated),
        "state": state,
    }


class Repo:
    def __init__(self, issues: list[dict], prs=(), comments=None):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="claim-test-"))
        self.state = self.dir / "state.json"
        self.gh = self.dir / "gh"
        self.gh.write_text(FAKE_GH)
        self.gh.chmod(0o755)
        self.state.write_text(
            json.dumps(
                {
                    "issues": {str(i["number"]): i for i in issues},
                    "prs": list(prs),
                    "comments": {str(k): v for k, v in (comments or {}).items()},
                    "next_id": 1000,
                    "writes": [],
                }
            )
        )

    def env(self, role="worker", **extra) -> dict:
        env = dict(os.environ)
        env.update(
            CLAIM_GH=str(self.gh),
            FAKE_GH_STATE=str(self.state),
            CLAIM_SETTLE_S="0",
            CLAIM_JITTER_S="0",
            CC_ROLE=role,
        )
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run(self, *args, role="worker", **extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLAIM), *args],
            capture_output=True,
            text=True,
            env=self.env(role, **extra),
            timeout=60,
        )

    def spawn(self, *args, **extra) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(CLAIM), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env(**extra),
        )

    def load(self) -> dict:
        return json.loads(self.state.read_text())

    def save(self, s: dict) -> None:
        self.state.write_text(json.dumps(s))

    def labels(self, n) -> list[str]:
        return sorted(self.load()["issues"][str(n)]["labels"])

    def comments(self, n) -> list[str]:
        return [c["body"] for c in self.load()["comments"].get(str(n), [])]


LOOP_USER = {"login": "loop-bot", "type": "User"}


def old_claim(
    cid: int, token: str, minutes: float, loop: str = "9", frm: str = "stage:ready"
) -> dict:
    return {
        "id": cid,
        "created_at": ago(minutes),
        "body": f"<!-- worker -->\n<!-- claim loop={loop} token={token} from={frm} -->\nClaimed",
        "user": LOOP_USER,
        "author_association": "OWNER",
    }


def test_pick_order() -> None:
    repo = Repo(
        [
            issue(1, ["stage:triage", "P2"]),
            issue(2, ["stage:ready", "P1"]),
            issue(3, ["stage:triage", "P0"], blocked_by=1),  # GitHub dependency open
            issue(4, ["stage:triage", "P0"], body="x\n\nBlocked by: #2"),  # body line, #2 open
            issue(5, ["stage:triage", "P1", "on hold"]),
            issue(6, ["stage:pr", "P0"]),
            issue(7, ["stage:doing", "P0"], updated=5),  # fresh claim
            issue(8, ["stage:doing", "P1"], updated=90),  # stale claim
            issue(9, ["stage:triage", "P1"], body="Blocked by: #99"),  # blocker closed / absent
            issue(10, ["stage:triage", "P1", "hold:monitor"]),
            issue(11, ["stage:triage"]),  # no priority: last
        ],
        comments={8: [old_claim(1, "dead", 90)]},
    )
    res = repo.run("candidates")
    got = [line.split("\t")[1] for line in res.stdout.splitlines()]
    check(
        "pick order: P1 by number, stale doing included, then P2, then unprioritized",
        got,
        ["#2", "#8", "#9", "#1", "#11"],
    )
    check("candidates writes nothing", repo.load()["writes"], [])


def test_single_claim() -> None:
    repo = Repo([issue(1, ["stage:triage", "P2"]), issue(2, ["stage:ready", "P1", "needs:live"])])
    cf = repo.dir / "claim"
    res = repo.run("claim", "--loop", "3", "--claim-file", str(cf))
    check("claim exits 0", res.returncode, 0)
    claim = json.loads(res.stdout)
    check("claims the P1", claim["issue"], 2)
    check("remembers where it came from", claim["from"], "stage:ready")
    check("reports needs:live", claim["needs_live"], True)
    check("claim file written", json.loads(cf.read_text())["token"], claim["token"])
    check(
        "label is stage:doing, ready removed", repo.labels(2), ["P1", "needs:live", "stage:doing"]
    )
    body = repo.comments(2)[0]
    check("claim comment is signed", body.startswith("<!-- worker -->"), True)
    check("claim comment names loop and token", f"loop=3 token={claim['token']}" in body, True)
    check(
        "a claim is one label edit and one comment",
        sorted(w[0] for w in repo.load()["writes"]),
        ["comment", "label"],
    )
    res = repo.run("check", "2", "--token", claim["token"])
    check("check says held", (res.returncode, res.stdout.strip()), (0, "held"))
    res = repo.run("check", "2", "--token", "someoneelse")
    check("check refuses another token", res.returncode, 4)


def test_role_is_enforced() -> None:
    repo = Repo([issue(1, ["stage:triage", "P2"])])
    for role in ("merger", "tester", ""):
        res = repo.run("claim", "--loop", "1", role=role)
        check(
            f"claim refused for role {role or '<unset>'}",
            res.returncode != 0 and res.returncode != 3,
            True,
        )
    check("nothing written by refused roles", repo.load()["writes"], [])
    res = repo.run("stale", "--release", role="tester")
    check("stale --release refused for the tester", res.returncode != 0, True)


def test_nothing_claimable() -> None:
    repo = Repo([issue(1, ["stage:pr", "P0"]), issue(2, ["stage:doing", "P0"], updated=1)])
    res = repo.run("claim", "--loop", "1")
    check("nothing claimable exits 3", res.returncode, 3)
    check("and writes nothing", repo.load()["writes"], [])


def test_claim_named_issue() -> None:
    """ccpool.py names the issue; claim.py claims that one or none."""
    repo = Repo(
        [
            issue(1, ["stage:ready", "P0"]),
            issue(2, ["stage:triage", "P2"]),
            issue(3, ["stage:triage", "P1"], body="Blocked by: #1"),
            issue(4, ["stage:pr", "P1"]),
        ]
    )
    res = repo.run("claim", "--loop", "7", "--issue", "2")
    check("--issue claims the named issue, not the top pick", json.loads(res.stdout)["issue"], 2)
    check("the top pick is untouched", repo.labels(1), ["P0", "stage:ready"])
    for n, why, said in ((3, "blocked", "blocked by #1"), (4, "not claimable", "at stage:pr"),
                         (99, "absent", "could not be read")):
        before = len(repo.load()["writes"])
        res = repo.run("claim", "--loop", "7", "--issue", str(n))
        check(f"--issue {n} ({why}) exits 3", res.returncode, 3)
        check(f"--issue {n} ({why}) writes nothing", len(repo.load()["writes"]), before)
        check(f"--issue {n} ({why}) says why", said in res.stderr, True)


def test_strangers_change_nothing() -> None:
    """On a public repository anyone can comment: a stranger's release marker emptied
    the claims, and a stranger's "hello" reset the attempt count."""
    sys.path.insert(0, str(HERE))
    import claim as cl
    cl._LOGIN[:] = ["loop-bot"]
    stranger = {"login": "drive-by", "type": "User"}

    def c(cid, body, user=LOOP_USER, assoc="OWNER"):
        return {"id": cid, "created_at": ago(1), "body": body, "user": user, "author_association": assoc}
    claim_c = old_claim(1, "tok1", 1)
    check("a stranger's release token=* does not cancel the claim",
          [e["token"] for e in cl.active_claims([claim_c, c(2, "<!-- release token=* -->", stranger, "NONE")])],
          ["tok1"])
    check("nor does a stranger's own claim count",
          cl.holds([c(0, "<!-- worker -->\n<!-- claim loop=1 token=evil from=stage:ready -->", stranger, "NONE"),
                    claim_c], "tok1"), (True, "tok1"))
    fail = "<!-- worker -->\n<!-- release token=x to=stage:triage fail=1 -->"
    check("a stranger's failed releases are not attempts",
          cl.failed_attempts([c(1, fail), c(2, fail, stranger, "NONE"), c(3, fail, stranger, "CONTRIBUTOR")]), 1)
    check("a stranger's plain comment or reset marker does not reset the count",
          (cl.is_reset(c(5, "hello", stranger, "NONE")), cl.is_reset(c(5, cl.RESET, stranger, "NONE"))),
          (False, False))
    check("the owner's plain comment (write access, no role marker) does",
          (cl.is_reset(c(5, "Try the simpler layout.", {"login": "kaz", "type": "User"}, "OWNER")),
           cl.is_reset(c(5, "Go ahead.", {"login": "teammate", "type": "User"}, "COLLABORATOR"))), (True, True))
    check("the monitor's reset marker from the loop's account does; an agent's signed comment does not",
          (cl.is_reset(c(5, "<!-- monitor -->\n" + cl.RESET)), cl.is_reset(c(5, "<!-- worker -->\nreleased"))),
          (True, False))
    check("a bot with write access is not the owner",
          cl.is_reset(c(5, "ok", {"login": "ci[bot]", "type": "Bot"}, "MEMBER")), False)
    cl._LOGIN[:] = []
    repo = Repo([issue(4, ["stage:triage", "P1"])])
    res = repo.run("claim", "--loop", "3", "--issue", "4")
    token = json.loads(res.stdout)["token"]
    s_ = repo.load()
    s_["next_id"] += 1
    s_["comments"]["4"].append({"id": s_["next_id"], "body": "<!-- release token=* -->", "created_at": ago(0),
                                "user": stranger, "author_association": "NONE"})
    repo.save(s_)
    check("end to end: the loop's account comes from `gh api user`, and the claim still holds",
          repo.run("check", "4", "--token", token).returncode, 0)


def test_refusal_says_what_failed() -> None:
    """A claim right after an issue was filed failed on GitHub's lag, and claim.py said
    "another worker claimed it first" though nobody had."""
    repo = Repo([issue(5, ["stage:triage", "P1"])])
    res = repo.run("claim", "--loop", "7", "--issue", "5", FAKE_GH_UNLISTED="5")
    check("missing from GitHub's list of open issues: exit 3, said so",
          (res.returncode, "not in GitHub's list of open issues" in res.stderr, "claimed it first" in res.stderr),
          (3, True, False))
    check("and nothing written", repo.load()["writes"], [])
    res = repo.run("claim", "--loop", "7", "--issue", "5", FAKE_GH_HIDE_AFTER="1000")
    check("its own claim comment not in the re-read: said so",
          (res.returncode, "own claim comment was not in GitHub's re-read" in res.stderr,
           "claimed it first" in res.stderr, "stage:doing" in res.stderr),
          (3, True, False, False))
    labels = set(repo.load()["issues"]["5"]["labels"])
    check("nobody holds it, so its stage is put back (not left at stage:doing)",
          ("stage:triage" in labels, "stage:doing" in labels), (True, False))
    repo = Repo([issue(6, ["stage:triage", "P1"])], comments={6: [old_claim(900, "rival", 0.1)]})
    res = repo.run("claim", "--loop", "7", "--issue", "6")
    check("a rival's earlier claim: another worker claimed it first",
          (res.returncode, "another worker claimed it first (claim rival)" in res.stderr), (3, True))


def test_two_loops_race_for_one_issue() -> None:
    """Both read #1 as top, both label it, both comment before either re-reads."""
    repo = Repo([issue(1, ["stage:triage", "P0"]), issue(2, ["stage:triage", "P2"])])
    procs = [
        repo.spawn("claim", "--loop", str(k), CLAIM_SETTLE_S=1.5, FAKE_GH_WRITE_DELAY=0.3)
        for k in (1, 2)
    ]
    outs = [p.communicate(timeout=60) for p in procs]
    claims = [json.loads(o[0]) for o, p in zip(outs, procs) if p.returncode == 0]
    check("both loops end up with a claim", len(claims), 2)
    check("they hold different issues", sorted(c["issue"] for c in claims), [1, 2])
    comments_1 = [c for c in repo.comments(1) if "<!-- claim" in c]
    check("the loser deleted its comment on #1: one claim left", len(comments_1), 1)
    winner = next(c for c in claims if c["issue"] == 1)
    check("the surviving comment is the winner's", winner["token"] in comments_1[0], True)
    for c in claims:
        res = repo.run("check", str(c["issue"]), "--token", c["token"])
        check(f"#{c['issue']} is held by its claimant", res.returncode, 0)


def test_eight_loops_three_issues() -> None:
    repo = Repo([issue(n, ["stage:ready", "P1"]) for n in (11, 12, 13)])
    procs = [
        repo.spawn("claim", "--loop", str(k), CLAIM_SETTLE_S=1.0, FAKE_GH_WRITE_DELAY=0.1)
        for k in range(1, 9)
    ]
    results = [(p.communicate(timeout=90)[0], p.returncode) for p in procs]
    won = [json.loads(o) for o, rc in results if rc == 0]
    check("exactly three loops win", len(won), 3)
    check("one winner per issue", sorted(c["issue"] for c in won), [11, 12, 13])
    check("the other five find nothing", sorted(rc for _, rc in results if rc != 0), [3] * 5)
    for n in (11, 12, 13):
        claims = [c for c in repo.comments(n) if "<!-- claim" in c]
        check(f"#{n} carries exactly one claim comment", len(claims), 1)
        check(f"#{n} is stage:doing", "stage:doing" in repo.labels(n), True)


def test_handed_back_pr_is_claimable_and_named() -> None:
    pr = {"number": 40, "head": {"ref": "issue-5"}, "body": "<!-- worker -->\nFixes #5"}
    repo = Repo(
        [issue(5, ["stage:triage", "P1"])],
        prs=[pr],
        comments={5: [old_claim(1, "oldtok", 50, frm="stage:triage")]},
    )
    res = repo.run("claim", "--loop", "4")
    check("a handed-back issue is claimable despite its old claim", res.returncode, 0)
    claim = json.loads(res.stdout)
    check(
        "the claim names the open PR and its branch",
        claim["pr"],
        {"number": 40, "branch": "issue-5"},
    )
    check("the new claim holds", repo.run("check", "5", "--token", claim["token"]).returncode, 0)


def test_stale_claim_is_taken_over() -> None:
    repo = Repo(
        [issue(6, ["stage:doing", "P0"], updated=75)], comments={6: [old_claim(1, "deadtok", 75)]}
    )
    res = repo.run("claim", "--loop", "2")
    check("a stale claim is reclaimed", res.returncode, 0)
    claim = json.loads(res.stdout)
    check("a reclaim keeps the stale claim's origin", claim["from"], "stage:ready")
    check(
        "the old token no longer holds", repo.run("check", "6", "--token", "deadtok").returncode, 4
    )
    check("the new token holds", repo.run("check", "6", "--token", claim["token"]).returncode, 0)


def test_finish_with_pr_sets_stage_pr() -> None:
    repo = Repo([issue(7, ["stage:ready", "P1"])])
    cf = repo.dir / "claim"
    claim = json.loads(repo.run("claim", "--loop", "1", "--claim-file", str(cf)).stdout)
    state = repo.load()
    state["prs"].append({"number": 70, "head": {"ref": "issue-7"}, "body": "Fixes #7"})
    repo.state.write_text(json.dumps(state))
    res = repo.run("finish", "--claim-file", str(cf), "--loop", "1")
    check("finish reports the PR", res.stdout.strip(), "#7: pr #70")
    check("finish moved the issue to stage:pr", repo.labels(7), ["P1", "stage:pr"])
    check("claim file removed", cf.exists(), False)
    check("token unused after", claim["issue"], 7)


def test_finish_without_pr_releases_then_parks() -> None:
    repo = Repo([issue(8, ["stage:ready", "P1"])])
    cf = repo.dir / "claim"
    repo.run("claim", "--loop", "1", "--claim-file", str(cf))
    res = repo.run("finish", "--claim-file", str(cf), "--loop", "1")
    check(
        "first failure goes back where it came from",
        res.stdout.strip(),
        "#8: released to stage:ready",
    )
    check("labels after first failure", repo.labels(8), ["P1", "stage:ready"])
    check(
        "release comment is signed and marked",
        repo.comments(8)[-1].startswith("<!-- worker -->")
        and "fail=1" in repo.comments(8)[-1],
        True,
    )
    claim2 = json.loads(repo.run("claim", "--loop", "2", "--claim-file", str(cf)).stdout)
    check("it is claimable again", claim2["issue"], 8)
    res = repo.run("finish", "--claim-file", str(cf), "--loop", "2")
    check("second failure goes back too", res.stdout.strip(), "#8: released to stage:ready")
    repo.run("claim", "--loop", "3", "--claim-file", str(cf))
    res = repo.run("finish", "--claim-file", str(cf), "--loop", "3")
    check(
        "third failure parks it at stage:owner", res.stdout.strip(), "#8: released to stage:owner"
    )
    s = repo.load()
    s["issues"]["8"]["labels"] = ["P1", "stage:triage"]
    repo.save(s)
    check("not claimable after three failures", repo.run("claim", "--loop", "4").returncode, 3)
    s = repo.load()
    s["next_id"] += 1
    s["comments"]["8"].append(
        {"id": s["next_id"], "body": "Try the simpler layout.", "created_at": ago(0), "user": LOOP_USER,
         "author_association": "OWNER"}
    )
    repo.save(s)
    claim5 = repo.run("claim", "--loop", "5", "--claim-file", str(cf))
    check("the owner's comment resets the count", (claim5.returncode, json.loads(claim5.stdout or "{}").get("issue")), (0, 8))
    repo.run("finish", "--claim-file", str(cf), "--loop", "5")
    s = repo.load()
    s["next_id"] += 1
    s["comments"]["8"].append(
        {"id": s["next_id"], "body": "<!-- monitor -->\n<!-- reset-attempts -->\nRe-queued.", "created_at": ago(0),
         "user": LOOP_USER, "author_association": "OWNER"}
    )
    s["issues"]["8"]["labels"] = ["P1", "stage:triage"]
    repo.save(s)
    check("and so does the monitor's reset marker", repo.run("claim", "--loop", "6").returncode, 0)


def test_agent_release() -> None:
    repo = Repo([issue(9, ["stage:triage", "P2"])])
    cf = repo.dir / "claim"
    claim = json.loads(repo.run("claim", "--loop", "5", "--claim-file", str(cf)).stdout)
    res = repo.run(
        "release",
        "9",
        "--token",
        claim["token"],
        "--to",
        "stage:owner",
        "--reason",
        "acceptance contradicts spec",
        WRK_CLAIM_FILE=cf,
        WRK_K=5,
    )
    check("release exits 0", res.returncode, 0)
    check("released to stage:owner", repo.labels(9), ["P2", "stage:owner"])
    check("claim file removed by release", cf.exists(), False)
    check(
        "finish afterwards is a no-op",
        repo.run("finish", "--claim-file", str(cf)).stdout.strip(),
        "no claim",
    )


def test_stale_sweep() -> None:
    pr = {"number": 41, "head": {"ref": "x"}, "body": "Refs #21"}
    repo = Repo(
        [
            issue(20, ["stage:doing", "P1"], updated=80),
            issue(21, ["stage:doing", "P1"], updated=80),  # has a PR: not stale
            issue(22, ["stage:doing", "P1"], updated=10),  # fresh
        ],
        prs=[pr],
        comments={
            20: [old_claim(1, "t20", 80)],
            21: [old_claim(2, "t21", 80)],
            22: [old_claim(3, "t22", 10)],
        },
    )
    res = repo.run("stale")
    check("stale lists only #20", res.stdout.strip(), "stale #20 (claim t20)")
    check("listing writes nothing", repo.load()["writes"], [])
    repo.run("stale", "--release")
    check("stale --release puts #20 back at its origin", repo.labels(20), ["P1", "stage:ready"])
    check("signed by the worker role (ccpool runs it)", repo.comments(20)[-1].startswith("<!-- worker -->"), True)
    check("#21 untouched", repo.labels(21), ["P1", "stage:doing"])


# --------------------------------------------------------------------------- #
# sweep_locked.py: at most SWEEP_SLOTS sweeps at a time across the pool and the
# merger, and the PR-body rewrite a rebase needs (Gate 2 wants Sweep SHA == head).
# --------------------------------------------------------------------------- #

SWEEP = HERE / "sweep_locked.py"
SHA = "a" * 40

# Stands in for the worktree's .claude/hooks/sweep_run.py: one second of work,
# with its start and end written down so overlaps can be measured.
FAKE_RUNNER = """import time
spans = {spans!r}
open(spans, "a").write(f"start {{time.time()}}\\n")
time.sleep(1)
print("git:            {sha} (clean)")
print("sweep record: /tmp/x/{sha}.json")
print()
print("47 green: unit 23 · e2e 24")
open(spans, "a").write(f"end {{time.time()}}\\n")
"""


def make_worktree(tmp: pathlib.Path) -> pathlib.Path:
    wt = tmp / "wt"
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    hooks = wt / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "sweep_run.py").write_text(FAKE_RUNNER.format(spans=str(tmp / "spans"), sha=SHA))
    return wt


def sweep_env(tmp: pathlib.Path, lock: pathlib.Path, body: str = "", slots: int = 1) -> dict:
    (tmp / "gh").write_text(f"""#!/usr/bin/env bash
if [ "$1 $2" = "pr view" ]; then cat "{tmp}/body.md"; fi
if [ "$1 $2" = "pr edit" ]; then cp "$5" "{tmp}/edited.md"; fi
""")
    (tmp / "body.md").write_text(body)
    (tmp / "gh").chmod(0o755)
    env = dict(os.environ)
    env.update(
        SWEEP_LOCK=str(lock),
        SWEEP_SLOTS=str(slots),
        CLAIM_GH=str(tmp / "gh"),
        SHARED_REPO_PATH="/nonexistent",
    )
    return env


def spans_of(tmp: pathlib.Path) -> tuple[list[float], list[float]]:
    spans = [line.split() for line in (tmp / "spans").read_text().splitlines()]
    return (sorted(float(t) for k, t in spans if k == "start"),
            sorted(float(t) for k, t in spans if k == "end"))


def run_sweeps(tmp: pathlib.Path, wt: pathlib.Path, env: dict, n: int) -> list[tuple[int, str]]:
    procs = [
        subprocess.Popen(
            [sys.executable, str(SWEEP)], cwd=wt, env=env, stdout=subprocess.PIPE, text=True
        )
        for _ in range(n)
    ]
    return [(p.communicate(timeout=60)[0], p.returncode) for p in procs]


def test_sweep_lock_serializes() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sweep-test-"))
    wt = make_worktree(tmp)
    lock = tmp / "issuelooper-sweep.lock"
    results = run_sweeps(tmp, wt, sweep_env(tmp, lock), 3)
    check("three concurrent sweeps all succeed", [rc for _, rc in results], [0, 0, 0])
    starts, ends = spans_of(tmp)
    check(
        "no two sweeps overlap", all(starts[i + 1] >= ends[i] for i in range(len(starts) - 1)), True
    )
    check("each reports its lock wait", all("sweep lock taken after" in o for o, _ in results), True)
    check("each passes the runner's output through",
          all("47 green: unit 23 · e2e 24" in o for o, _ in results), True)
    check("the lock file is left in place (never unlinked)", lock.is_file(), True)


def test_sweep_slots_run_side_by_side() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sweep-test-"))
    wt = make_worktree(tmp)
    lock = tmp / "lock"
    results = run_sweeps(tmp, wt, sweep_env(tmp, lock, slots=3), 3)
    check("three sweeps in three slots all succeed", [rc for _, rc in results], [0, 0, 0])
    starts, ends = spans_of(tmp)
    check("with three slots they overlap", starts[-1] < ends[0], True)
    check("slot files are LOCK and LOCK.k", sorted(p.name for p in tmp.glob("lock*")),
          ["lock", "lock.1", "lock.2"])


def test_a_sweep_lane_is_not_a_slot() -> None:
    """The main check held one of the sessions' four slots for over half the run."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sweep-test-"))
    wt = make_worktree(tmp)
    lock = tmp / "lock"
    env = sweep_env(tmp, lock, slots=1)
    procs = [subprocess.Popen([sys.executable, str(SWEEP), *extra], cwd=wt, env=env, stdout=subprocess.PIPE,
                              text=True) for extra in ([], ["--lane", "main"])]
    rcs = [(p.communicate(timeout=60), p.returncode)[1] for p in procs]
    starts, ends = spans_of(tmp)
    check("a lane sweep runs beside the one slot in use", (rcs, starts[-1] < ends[0]), ([0, 0], True))
    check("in its own lock file", sorted(x.name for x in tmp.glob("lock*")), ["lock", "lock.lane-main"])
    (tmp / "spans").unlink()
    results = run_sweeps(tmp, wt, env, 0)
    procs = [subprocess.Popen([sys.executable, str(SWEEP), "--lane", "main"], cwd=wt, env=env,
                              stdout=subprocess.PIPE, text=True) for _ in range(2)]
    [p.communicate(timeout=60) for p in procs]
    starts, ends = spans_of(tmp)
    check("one at a time within the lane", (results, starts[-1] >= ends[0]), ([], True))


def test_sweep_refuses_the_shared_checkout() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sweep-test-"))
    wt = make_worktree(tmp)
    env = sweep_env(tmp, tmp / "lock")
    env["SHARED_REPO_PATH"] = str(wt)
    res = subprocess.run(
        [sys.executable, str(SWEEP)], cwd=wt, env=env, capture_output=True, text=True
    )
    check("sweeping the shared checkout is refused", res.returncode, 2)


def test_sweep_needs_the_runner() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sweep-test-"))
    wt = make_worktree(tmp)
    (wt / ".claude" / "hooks" / "sweep_run.py").unlink()
    res = subprocess.run(
        [sys.executable, str(SWEEP)], cwd=wt, env=sweep_env(tmp, tmp / "lock"),
        capture_output=True, text=True,
    )
    check("a worktree with no sweep_run.py is refused", res.returncode, 2)
    check("and the refusal names the runner", "sweep_run.py" in res.stderr, True)


def test_sweep_update_pr_body() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sweep-test-"))
    wt = make_worktree(tmp)
    body = (
        "<!-- worker -->\nFixes #5\n\n- **Base:** 1234567\n- **Sweep SHA:** bbbbbbbbbbbb\n"
        "- **Fail-before:** tests/test_app.py 3 -> 4\n\n45 green: unit 22 · e2e 23\n"
    )
    env = sweep_env(tmp, tmp / "lock", body)
    res = subprocess.run(
        [sys.executable, str(SWEEP), "--update-pr", "41"],
        cwd=wt,
        env=env,
        capture_output=True,
        text=True,
    )
    check("update-pr exits 0", res.returncode, 0)
    edited = (tmp / "edited.md").read_text() if (tmp / "edited.md").exists() else ""
    check("Sweep SHA now names the swept head", f"- **Sweep SHA:** {SHA}\n" in edited, True)
    check(
        "the green line is replaced, not duplicated",
        ("47 green: unit 23 · e2e 24" in edited, "45 green" in edited),
        (True, False),
    )
    check(
        "the rest of the body is kept",
        "Fail-before:** tests/test_app.py 3 -> 4" in edited,
        True,
    )


def test_ux_proposed_is_never_claimed() -> None:
    """A UX candidate waits for the owner; claim.py neither lists nor claims it."""
    before = len(FAILURES)
    repo = Repo(
        [
            issue(1, ["stage:triage", "P0", "ux:proposed"]),
            issue(2, ["stage:triage", "P2", "ux:accepted"]),
        ]
    )
    got = [line.split("\t")[1] for line in repo.run("candidates").stdout.splitlines()]
    check("ux:proposed is no candidate, ux:accepted is", got, ["#2"])
    res = repo.run("claim", "--loop", "1", "--issue", "1")
    check("claim --issue on a ux:proposed issue exits 3", res.returncode, 3)
    check("and writes nothing", repo.load()["writes"], [])
    assert len(FAILURES) == before, FAILURES[before:]


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all claim cases pass ({len(tests)} groups, {CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
