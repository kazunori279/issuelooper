#!/usr/bin/env python3
"""What the dashboard and `issuelooper watch` show, built from the run directory.

    dash_model.py text      the terminal view (what `issuelooper watch` prints)
    dash_model.py json      the whole model as JSON

It reads only local files: the supervisor's pool.jsonl and state.json (state.json
carries the queue counts the supervisor read from GitHub, so this never calls GitHub),
each session's directory, the review gate's records, and the monitor's state (herdr
or the broker's socket). Every view is a function of one dict, `load()`.
"""

from __future__ import annotations

import collections
import json
import os
import pathlib
import re
import socket
import statistics
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import agent_cli  # noqa: E402
import config  # noqa: E402
import herdr_link  # noqa: E402

HOUR = 3600.0
SERIES_SPAN = 6 * HOUR
SERIES_STEP = 300.0


def repo_root() -> pathlib.Path:
    shared = os.environ.get("SHARED_REPO_PATH", "").strip()
    return pathlib.Path(shared) if shared else HERE.parents[1]


def run_dir() -> pathlib.Path:
    v = os.environ.get("ISSUELOOPER_RUN", "").strip()
    return pathlib.Path(v) if v else repo_root() / ".issuelooper" / "run"


def read_jsonl(path: pathlib.Path) -> list[dict]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for x in lines:
        try:
            out.append(json.loads(x))
        except ValueError:
            continue
    return out


def read_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def alive(pidfile: pathlib.Path) -> bool:
    try:
        os.kill(int(pidfile.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def github_repo(root: pathlib.Path) -> str:
    """owner/name from the origin remote, for links ('' when it is not GitHub)."""
    try:
        url = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else ""


def outcome_class(outcome: str) -> str:
    """One word per session result, for counting."""
    o = outcome or ""
    if o == "ok":
        return "ok"
    if "handed back" in o:
        return "handed back"
    if o.startswith("timed out"):
        return "timed out"
    if o.startswith("quota error"):
        return "quota"
    if o.startswith("startup crash"):
        return "crashed"
    if o == "unclaimable":
        return "unclaimable"
    if o.startswith(
        ("failed", "claim failed", "error", "merge refused", "push refused")
    ):
        return "failed"
    return "other"


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(max(0, seconds))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    return f"{s / 3600:.1f} h"


UNIT_KINDS = {"wrk": "#", "mrg": "PR #", "merge": "PR #", "rebase": "PR #", "handback": "PR #", "reclaim": "#",
              "tst": "#"}


def target_of(r: dict) -> str:
    """"#7" for an issue, "PR #8" for a pull request, from the record or its unit key."""
    unit = str(r.get("unit", ""))
    n = r.get("number")
    m = re.match(r"([a-z]+):(?:live)?(\d+)", unit)
    if n is None and m:
        n = int(m.group(2))
    if n is None:
        return ""
    prefix = UNIT_KINDS.get(m.group(1), "#") if m else "#"
    if r.get("role") == "merger":
        prefix = "PR #"
    return f"{prefix}{n}"


def unit_label(r: dict) -> str:
    """A unit in words: "worker #7", "auto-merge of PR #8", "tester UX run"."""
    unit, role, tgt = str(r.get("unit", "")), r.get("role", ""), target_of(r)
    if unit.startswith("merge:"):
        return f"auto-merge of {tgt}"
    if unit.startswith("rebase:"):
        return f"auto-rebase of {tgt}"
    if unit.startswith("reclaim:"):
        return f"stale claim on {tgt}"
    if unit.startswith("handback:"):
        return f"hand-back of {tgt}"
    if unit.startswith("ux:"):
        return "tester UX run"
    if unit.startswith("spec:"):
        return "tester spec check"
    return f"{role} {tgt}".strip() if tgt else f"{role} {unit}"


# --- the monitor ---------------------------------------------------------------


def broker_status(sock: pathlib.Path) -> dict | None:
    if not sock.exists():
        return None
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    try:
        s.connect(str(sock))
        s.sendall((json.dumps({"cmd": "status"}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data)
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def monitor_state(run: pathlib.Path) -> dict:
    rec = read_json(run / "herdr.json")
    if rec.get("monitor"):
        if rec.get("socket") and not os.environ.get("HERDR_SOCKET_PATH"):
            os.environ["HERDR_SOCKET_PATH"] = rec["socket"]
        aides = [
            {"pane": p, "state": herdr_link.status(p)} for p in rec.get("aides") or []
        ]
        return {
            "host": "herdr",
            "state": herdr_link.status(rec["monitor"]),
            "name": rec.get("monitor_name", ""),
            "pane": rec["monitor"],
            "session": rec.get("session", ""),
            "aides": aides,
        }
    b = broker_status(run / "broker.sock")
    if b is not None:
        return {
            "host": "broker",
            "state": "working" if b.get("busy") else "idle",
            "name": "monitor",
            "restarts": b.get("restarts", 0),
            "aides": [],
        }
    return {"host": "", "state": "gone", "name": "", "aides": []}


# --- the model -------------------------------------------------------------------


def load(
    run: pathlib.Path | None = None, now: float | None = None, monitor=None
) -> dict:
    run = run or run_dir()
    now = time.time() if now is None else now
    logdir = pathlib.Path(os.environ.get("POOL_LOGDIR", "").strip() or run / "pool")
    log = read_jsonl(logdir / "pool.jsonl")
    state = read_json(logdir / "state.json")
    started_at = max(
        (r["t"] for r in log if r.get("kind") == "supervisor_start"), default=0.0
    )
    mon = monitor if monitor is not None else monitor_state(run)
    root = repo_root()
    model = {
        "now": now,
        "repo": github_repo(root),
        "root": str(root),
        "health": health(run, state, log, mon, now),
        "monitor": mon,
        "sessions": sessions(state, now),
        "queue": state.get("queue") or {},
        "progress": {
            "1h": progress(log, now - HOUR, now),
            "24h": progress(log, now - 24 * HOUR, now),
        },
        "series": series(log, now),
        "recent": recent(log),
    }
    model["progress"]["24h"]["reviewer"] = reviewer_usage(
        max(started_at, now - 24 * HOUR)
    )
    # The UX runs' scores, oldest first (None: the report had no `Score: N/100`).
    model["progress"]["ux_scores"] = [
        r.get("score") for r in state.get("ux_scores") or []
    ]
    model["attention"] = attention(model, state, log, now)
    return model


def health(
    run: pathlib.Path, state: dict, log: list[dict], mon: dict, now: float
) -> dict:
    paused = {
        r: until for r, until in (state.get("paused") or {}).items() if until > now
    }
    quota = [r for r in log if r.get("kind") == "quota_pause"]
    q_until = (quota[-1]["t"] + float(quota[-1].get("seconds") or 0)) if quota else 0
    deploys = [r for r in log if r.get("kind") in ("deployed", "deploy_failed")]
    return {
        "supervisor": alive(run / "pool.pid"),
        "last_poll_s": (now - state["t"]) if state.get("t") else None,
        "main": state.get("main", ""),
        "deployed": state.get("deployed", ""),
        "deployed_ago_s": (now - deploys[-1]["t"])
        if deploys and deploys[-1]["kind"] == "deployed"
        else None,
        "quota_pause_s": max(0.0, q_until - now),
        "paused_roles": paused,
        "monitor": mon.get("state", "gone"),
        # the supervisor's last full check of origin/main: {sha, ok, t, failing}
        "main_check": state.get("main_check") or {},
    }


def sessions(state: dict, now: float) -> list[dict]:
    out = []
    for s in state.get("sessions") or []:
        d = pathlib.Path(s.get("dir", ""))
        raw = d / "session.jsonl"
        quiet = step = None
        if raw.exists():
            quiet = now - raw.stat().st_mtime
            try:
                lines = raw.read_text(errors="replace").splitlines()[-400:]
                step = agent_cli.get(s.get("cli") or agent_cli.DEFAULT).last_tool(
                    agent_cli.parse(lines)
                )
            except (OSError, ValueError):
                step = None
        try:
            resumes = len((d / "resumes.log").read_text().splitlines())
        except OSError:
            resumes = 0
        elapsed = now - float(s.get("started") or now)
        out.append(
            {
                "seq": s.get("seq"),
                "role": s.get("role"),
                "kind": s.get("kind"),
                "number": s.get("number"),
                "unit": s.get("unit"),
                "target": target_of(s),
                "elapsed_s": elapsed,
                "timeout_s": s.get("timeout_s") or 0,
                "quiet_s": quiet,
                "step": step or "",
                "resumes": resumes,
                "dir": str(d),
            }
        )
    return sorted(out, key=lambda x: x["seq"] or 0)


def progress(log: list[dict], since: float, now: float) -> dict:
    win = [r for r in log if since <= r.get("t", 0) <= now]
    texts = [str(r.get("text", "")) for r in win if r.get("kind") == "event"]
    ends = [r for r in win if r.get("kind") == "end"]
    outcomes = collections.Counter(
        outcome_class(r.get("outcome", "")) for r in ends if r.get("role") != "auto"
    )
    handbacks = [r for r in win if r.get("kind") == "handback"]
    reasons = collections.Counter(
        f"{r.get('by')}: {r.get('reason')}"[:120] for r in handbacks
    )
    tokens: dict[str, dict] = {}
    cost, cost_known, cost_missing = 0.0, 0, 0
    for r in ends:
        u = r.get("usage")
        if not u:
            continue
        t = tokens.setdefault(
            r.get("role", "?"), {"input": 0, "output": 0, "sessions": 0}
        )
        t["input"] += int(u.get("input") or 0)
        t["output"] += int(u.get("output") or 0) + int(u.get("thinking") or 0)
        t["sessions"] += 1
        if u.get("cost_usd") is None:
            cost_missing += 1
        else:
            cost += float(u["cost_usd"])
            cost_known += 1
    return {
        "merges": sum(1 for x in texts if x.endswith("(a merge)")),
        "closed": sum(1 for x in texts if re.match(r"#\d+ closed:", x)),
        "handed_back": sum(1 for x in texts if "was handed back" in x),
        "handback_reasons": reasons.most_common(3),
        "outcomes": dict(outcomes),
        "cycle_min": cycle_times(log, since, now),
        "tokens": tokens,
        "cost_usd": round(cost, 2) if cost_known else None,
        "cost_sessions_without_price": cost_missing,
    }


def cycle_times(log: list[dict], since: float, now: float) -> dict:
    """Minutes from an issue's first worker start to its close, for closes in the window."""
    first: dict[int, float] = {}
    for r in log:
        if r.get("kind") == "start" and r.get("role") == "worker":
            m = re.match(r"wrk:(\d+)@", str(r.get("unit", "")))
            if m:
                first.setdefault(int(m.group(1)), r["t"])
    mins = []
    for r in log:
        m = (
            re.match(r"#(\d+) closed:", str(r.get("text", "")))
            if r.get("kind") == "event"
            else None
        )
        if m and since <= r["t"] <= now and int(m.group(1)) in first:
            mins.append((r["t"] - first[int(m.group(1))]) / 60)
    if not mins:
        return {"n": 0, "median": None, "p90": None}
    mins.sort()
    return {
        "n": len(mins),
        "median": round(statistics.median(mins)),
        "p90": round(mins[min(len(mins) - 1, int(0.9 * len(mins)))]),
    }


def reviewer_usage(since: float) -> dict:
    d = config.record_dirs("review")[0]
    out = {"input": 0, "output": 0, "calls": 0, "verdicts": collections.Counter()}
    for f in d.glob("*.json"):
        r = read_json(f)
        if not r or float(r.get("t") or 0) < since:
            continue
        u = r.get("usage") or {}
        out["input"] += int(u.get("input_tokens") or 0)
        out["output"] += int(u.get("output_tokens") or 0) + int(
            u.get("thinking_tokens") or 0
        )
        out["calls"] += int(u.get("calls") or 0)
        out["verdicts"][str(r.get("verdict") or "?")] += 1
    out["verdicts"] = dict(out["verdicts"])
    return out


def series(log: list[dict], now: float) -> dict:
    """Agents at work by role, open issues, and merges per hour, over the last six hours."""
    t0 = now - SERIES_SPAN
    starts = {
        r["session"]: r for r in log if r.get("kind") == "start" and "session" in r
    }
    ends = {
        r["session"]: r["t"] for r in log if r.get("kind") == "end" and "session" in r
    }
    supervisor_starts = sorted(
        r["t"] for r in log if r.get("kind") == "supervisor_start"
    )
    agents = []
    t = t0
    while t <= now:
        counts = collections.Counter()
        for seq, r in starts.items():
            if r.get("role") == "auto" or r["t"] > t:
                continue
            end = ends.get(seq)
            if end is None:  # a supervisor restart ends what it did not finish
                end = next((s for s in supervisor_starts if s > r["t"]), None)
            if end is None or end > t:
                counts[r["role"]] += 1
        agents.append([round(t), dict(counts)])
        t += SERIES_STEP
    queue = [
        [r["t"], r.get("open", 0)]
        for r in log
        if r.get("kind") == "queue" and r["t"] >= t0 - SERIES_SPAN
    ]
    merges = collections.Counter()
    for r in log:
        if (
            r.get("kind") == "event"
            and str(r.get("text", "")).endswith("(a merge)")
            and r["t"] >= t0
        ):
            merges[int((r["t"] - t0) // HOUR)] += 1
    return {
        "t0": t0,
        "now": now,
        "agents": agents,
        "queue": queue,
        "merges_per_hour": [merges.get(k, 0) for k in range(int(SERIES_SPAN // HOUR))],
    }


def describe(r: dict) -> str | None:
    k = r.get("kind")
    if k == "event":
        return str(r.get("text", ""))
    if k == "start":
        return f"{unit_label(r)} started (session {r.get('session')})"
    if k == "end":
        tok = r.get("usage") or {}
        extra = f", {(tok.get('input', 0) + tok.get('output', 0)) / 1000:.0f}k tokens" if tok else ""
        return f"{unit_label(r)} ended: {r.get('outcome')} after {ago(r.get('seconds'))}{extra}"
    if k == "handback":
        return f"PR #{r.get('pr')} (#{r.get('issue')}) handed back by {r.get('by')}: {r.get('reason')}"
    if k == "quota_pause":
        return f"quota error from a {r.get('role')}: new sessions paused {ago(r.get('seconds'))}"
    if k in ("deployed", "deploy_failed", "deploy"):
        return f"{k.replace('_', ' ')} {r.get('sha', '')}"
    if k == "feed":
        return f"sent the monitor {r.get('events')} update(s)"
    if k in ("monitor_blocked", "monitor_gone"):
        return f"the monitor is {k.split('_')[1]}"
    if k in ("poll_error", "feed_error"):
        return f"{k.replace('_', ' ')}: {str(r.get('error', ''))[:120]}"
    if k == "supervisor_start":
        return "supervisor started"
    if k == "split_filed":
        return f"split issue filed for {r.get('file', '')}"
    return None


def recent(log: list[dict], n: int = 30) -> list[dict]:
    out = []
    for r in reversed(log):
        text = describe(r)
        if text:
            out.append({"t": r.get("t", 0), "kind": r.get("kind"), "text": text})
            if len(out) >= n:
                break
    return out


def attention(m: dict, state: dict, log: list[dict], now: float) -> list[dict]:
    """What needs the owner, worst first: [{level: red|yellow, what, next}]."""
    out = []
    h = m["health"]

    def add(level, what, nxt):
        out.append({"level": level, "what": what, "next": nxt})

    if not h["supervisor"]:
        add(
            "red",
            "The supervisor is not running.",
            "`issuelooper start` (or read .issuelooper/run/pool.out)",
        )
    elif h["last_poll_s"] is not None and h["last_poll_s"] > 120:
        add(
            "red",
            f"The supervisor has not read GitHub for {ago(h['last_poll_s'])}.",
            "Read .issuelooper/run/pool.out",
        )
    if m["monitor"].get("state") == "blocked":
        add(
            "red",
            "The monitor is waiting at a question or approval prompt.",
            "Answer it in the monitor's pane",
        )
    elif m["monitor"].get("state") == "gone" and h["supervisor"]:
        add(
            "red",
            "The monitor is not running; queue updates pile up.",
            "`issuelooper stop` then `issuelooper start`",
        )
    for key in state.get("stuck") or []:
        add(
            "red",
            f"Stuck: {key} keeps failing and will not be retried.",
            "Read its session.log; re-scope the issue",
        )
    mc = h["main_check"]
    if mc.get("ok") is False:
        add(
            "red",
            f"main is red at {mc.get('sha', '')[:8]}: "
            + (", ".join(mc.get("failing") or [])[:200] or "the check suite fails"),
            "The supervisor filed a `main is red` issue (P0); merges go on",
        )
    deploys = [r for r in log if r.get("kind") in ("deployed", "deploy_failed")]
    if deploys and deploys[-1]["kind"] == "deploy_failed":
        add(
            "red",
            f"The last deploy failed ({deploys[-1].get('sha', '')}).",
            "Read .issuelooper/run/pool/deploy.log",
        )
    for o in m["queue"].get("owner") or []:
        add(
            "yellow",
            f"#{o['number']} waits for your decision: {o['title']}",
            "Comment on the issue",
        )
    by_family = collections.defaultdict(list)
    for r in log:
        if (
            r.get("kind") == "end"
            and r.get("role") in ("worker", "merger", "tester")
            and r.get("number") is not None
        ):
            by_family[(r["role"], r["number"])].append(
                outcome_class(r.get("outcome", ""))
            )
    for (role, n), results in by_family.items():
        bad = [x for x in results[-2:] if x in ("failed", "timed out", "crashed")]
        if len(results) >= 2 and len(bad) == 2:
            add(
                "yellow",
                f"{role} sessions on #{n} failed twice in a row ({', '.join(bad)}).",
                "Read the last session.log under .issuelooper/run/pool/",
            )
    if any(r.get("kind") == "poll_error" and r["t"] > now - 600 for r in log):
        add(
            "yellow",
            "The supervisor could not read GitHub in the last 10 min.",
            "`gh auth status`; `gh api rate_limit`",
        )
    since = state.get("feed_pending_since") or 0
    if since and now - since > 900:
        add(
            "yellow",
            f"{state.get('feed_pending', 0)} update(s) have waited {ago(now - since)} for the monitor.",
            "Check that the monitor is not stuck on a long request",
        )
    if h["quota_pause_s"] > 0:
        add(
            "yellow",
            f"Model quota errors: new sessions paused for {ago(h['quota_pause_s'])}.",
            "Wait, or check quota",
        )
    for role, until in h["paused_roles"].items():
        add(
            "yellow",
            f"{role} sessions kept crashing at startup; paused {ago(until - now)}.",
            "Read their session.err",
        )
    return out


# --- the terminal view --------------------------------------------------------------


def ux_scores_text(scores: list) -> str:
    """"UX score 38 → 58 → 78" for the last six runs that reported one."""
    known = [str(s) for s in scores if s is not None][-6:]
    missing = sum(1 for s in scores if s is None)
    return (
        "UX score "
        + (" → ".join(known) or "none reported")
        + (f" ({missing} report(s) without a score)" if missing else "")
    )


def main_check_text(mc: dict) -> str:
    """" · main check green abcd1234", or "" before the first check."""
    if not mc:
        return ""
    word = {True: "green", False: "RED"}.get(mc.get("ok"), "not run")
    return f" · main check {word} {str(mc.get('sha', ''))[:8]}"


def text(m: dict, width: int = 100) -> str:
    h, q, p = m["health"], m["queue"], m["progress"]
    lines = [
        f"{time.strftime('%H:%M:%S', time.localtime(m['now']))}  {m['repo'] or m['root']}",
        f"supervisor {'up' if h['supervisor'] else 'DOWN'}, polled {ago(h['last_poll_s'])} ago · "
        f"monitor {h['monitor']} · main {h['main'][:8]}"
        + main_check_text(h["main_check"])
        + (f" · deployed {h['deployed'][:8]}" if h["deployed"] else "")
        + (f" · QUOTA PAUSE {ago(h['quota_pause_s'])}" if h["quota_pause_s"] else ""),
        "",
    ]
    if m["attention"]:
        lines.append("Needs attention:")
        for a in m["attention"][:8]:
            lines.append(
                f"  {'!!' if a['level'] == 'red' else ' !'} {a['what']}  -> {a['next']}"
            )
    else:
        lines.append("Needs attention: nothing")
    lines.append("")
    lines.append(f"Agents ({len(m['sessions'])} running):")
    for s in m["sessions"]:
        frac = f"/{ago(s['timeout_s'])}" if s["timeout_s"] else ""
        lines.append(
            f"  {s['role']:<7} {s['target'] or s['unit']:<10} {ago(s['elapsed_s'])}{frac:<8} "
            f"quiet {ago(s['quiet_s']):<7} {s['step'][: width - 50]}"
        )
    for a in m["monitor"].get("aides") or []:
        lines.append(f"  aide    pane {a['pane']:<6} {a['state']}")
    st = q.get("stages") or {}
    lines += [
        "",
        "Queue: "
        + " → ".join(
            f"{k} {st.get(v, 0)}"
            for k, v in (
                ("untriaged", "untriaged"),
                ("triage", "stage:triage"),
                ("ready", "stage:ready"),
                ("doing", "stage:doing"),
                ("pr", "stage:pr"),
                ("live", "stage:live"),
            )
        )
        + f" · owner {st.get('stage:owner', 0)} · on hold {st.get('on hold', 0)} · open PRs {sum((q.get('prs') or {}).values())}",
    ]
    if p.get("ux_scores"):
        lines.append(ux_scores_text(p["ux_scores"]))
    for key in ("1h", "24h"):
        x = p[key]
        cyc = x["cycle_min"]
        lines.append(
            f"Last {key}: {x['merges']} merged, {x['closed']} closed, {x['handed_back']} handed back · "
            f"cycle median {cyc['median'] if cyc['median'] is not None else '—'} min · "
            + ", ".join(f"{k} {v}" for k, v in sorted(x["outcomes"].items()))
        )
    lines.append("")
    lines.append("Recent:")
    for r in m["recent"][:8]:
        lines.append(
            f"  {time.strftime('%H:%M', time.localtime(r['t']))} {r['text'][: width - 8]}"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "text"
    m = load()
    if cmd == "json":
        print(json.dumps(m, indent=1, default=str))
    else:
        import shutil

        width = shutil.get_terminal_size((100, 40)).columns
        print("\n".join(x[:width] for x in text(m, width).splitlines()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
