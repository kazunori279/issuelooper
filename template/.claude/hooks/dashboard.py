#!/usr/bin/env python3
"""The loop's dashboard: one page, served on 127.0.0.1, refreshed every 15 s.

    dashboard.py [--port 8780]

    /                     health, what needs you, agents at work, the queue, progress,
                          two charts, recent events
    /session/<seq>        one session: its prompt, the end of its log, its resumes
    /api/state.json       the same model as JSON (dash_model.load)

Everything comes from dash_model.py, which reads only the run directory and the
monitor's state; the page never calls GitHub. Plain HTML and SVG, no JavaScript.
`issuelooper start` starts it in the background on the first free port from 8780.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import dash_model as dm  # noqa: E402

ROLE_COLORS = {
    "worker": "#4e9af1",
    "merger": "#d36bd8",
    "tester": "#e8b04e",
    "auto": "#8a93a6",
}
CSS = """
body{background:#14161c;color:#d8dbe2;font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:24px;max-width:1200px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:15px;margin:26px 0 8px;color:#aab3c5}
a{color:#7fb2f5;text-decoration:none} a:hover{text-decoration:underline}
.dim{color:#7d8496} .bar{display:flex;gap:18px;flex-wrap:wrap;padding:10px 14px;background:#1d2029;border-radius:8px}
.ok{color:#5cc98a} .warn{color:#e8b04e} .bad{color:#f17a7a}
table{border-collapse:collapse;width:100%} td,th{padding:5px 10px;border-bottom:1px solid #262a35;text-align:left;vertical-align:top}
th{color:#8a93a6;font-weight:normal}
.att li{margin:4px 0} .att .red{color:#f17a7a} .att .yellow{color:#e8b04e}
.pipe{display:flex;gap:6px;flex-wrap:wrap} .stage{background:#1d2029;border-radius:6px;padding:8px 12px;min-width:84px}
.stage b{font-size:20px;display:block} .cols{display:flex;gap:28px;flex-wrap:wrap}
pre{background:#0f1116;padding:12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;font-size:12px}
.chip{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 10px}
"""


def esc(x) -> str:
    return html.escape(str(x))


def cls(level: str) -> str:
    return {"red": "bad", "yellow": "warn"}.get(level, "ok")


def issue_link(m: dict, target: str) -> str:
    n = re.search(r"\d+", target or "")
    if not (m["repo"] and n):
        return esc(target)
    kind = "pull" if target.startswith("PR") else "issues"
    return f'<a href="https://github.com/{esc(m["repo"])}/{kind}/{n.group()}" target="_blank">{esc(target)}</a>'


def label_link(m: dict, label: str, text: str) -> str:
    if not m["repo"]:
        return text
    q = "is%3Aissue+is%3Aopen+label%3A%22" + label.replace(" ", "+") + "%22"
    return f'<a href="https://github.com/{esc(m["repo"])}/issues?q={q}" target="_blank">{text}</a>'


def header(m: dict) -> str:
    h = m["health"]
    poll = h["last_poll_s"]
    items = [
        f'supervisor <b class="{"ok" if h["supervisor"] else "bad"}">{"up" if h["supervisor"] else "down"}</b>',
        f'GitHub read <b class="{"ok" if poll is not None and poll < 60 else "warn" if poll is not None and poll < 120 else "bad"}">'
        f"{dm.ago(poll)} ago</b>",
        f'monitor <b class="{ {"idle": "ok", "done": "ok", "working": "ok", "blocked": "bad", "gone": "bad"}.get(h["monitor"], "warn") }">'
        f'{esc(h["monitor"])}</b> <span class="dim">{esc(m["monitor"].get("host", ""))}</span>',
        f"main <code>{esc(h['main'][:8])}</code>",
    ]
    mc = h.get("main_check") or {}
    if mc:
        ok = mc.get("ok")
        items.append(
            f'main check <b class="{ {True: "ok", False: "bad"}.get(ok, "warn") }">'
            f'{ {True: "green", False: "red"}.get(ok, "not run") }</b> <code>{esc(str(mc.get("sha", ""))[:8])}</code>'
        )
    if h["deployed"]:
        items.append(
            f'deployed <code>{esc(h["deployed"][:8])}</code> <span class="dim">{dm.ago(h["deployed_ago_s"])} ago</span>'
        )
    if h["quota_pause_s"]:
        items.append(f'<b class="warn">quota pause {dm.ago(h["quota_pause_s"])}</b>')
    return '<div class="bar">' + "".join(f"<span>{x}</span>" for x in items) + "</div>"


def attention(m: dict) -> str:
    if not m["attention"]:
        return '<h2>Needs you</h2><div class="ok">Nothing. The loop is running on its own.</div>'
    items = "".join(
        f'<li><span class="{a["level"]}">{"●" if a["level"] == "red" else "○"} {esc(a["what"])}</span> '
        f'<span class="dim">→ {esc(a["next"])}</span></li>'
        for a in m["attention"]
    )
    return f'<h2>Needs you</h2><ul class="att">{items}</ul>'


def agents(m: dict) -> str:
    rows = []
    for s in m["sessions"]:
        frac = s["elapsed_s"] / s["timeout_s"] if s["timeout_s"] else 0
        el_cls = "bad" if frac > 0.9 else "warn" if frac > 0.75 else ""
        quiet_cls = "warn" if (s["quiet_s"] or 0) > 300 else ""
        rows.append(
            f'<tr><td><span class="chip" style="background:{ROLE_COLORS.get(s["role"], "#888")}"></span>{esc(s["role"])}'
            f' <span class="dim">{esc(s["kind"])}</span></td>'
            f"<td>{issue_link(m, s['target']) if s['target'] else esc(s['unit'])}</td>"
            f'<td class="{el_cls}">{dm.ago(s["elapsed_s"])}'
            + (
                f' <span class="dim">/ {dm.ago(s["timeout_s"])}</span>'
                if s["timeout_s"]
                else ""
            )
            + f'</td><td class="{quiet_cls}">{dm.ago(s["quiet_s"])}</td>'
            f"<td><code>{esc(s['step'][:110])}</code></td><td>{s['resumes'] or ''}</td>"
            f'<td><a href="/session/{s["seq"]}">log</a></td></tr>'
        )
    mon = m["monitor"]
    if mon.get("host"):
        rows.append(
            f'<tr><td class="dim">monitor</td><td>{esc(mon.get("name", ""))}</td><td colspan="5">'
            f'{esc(mon.get("state", ""))} <span class="dim">{esc(mon.get("host"))}'
            f"{' pane ' + esc(mon.get('pane')) if mon.get('pane') else ''}</span></td></tr>"
        )
    for a in mon.get("aides") or []:
        rows.append(
            f'<tr><td class="dim">aide</td><td>pane {esc(a["pane"])}</td><td colspan="5">{esc(a["state"])}</td></tr>'
        )
    if not rows:
        return '<h2>Agents</h2><div class="dim">No session running.</div>'
    return (
        "<h2>Agents</h2><table><tr><th>role</th><th>on</th><th>elapsed / timeout</th><th>last output</th>"
        "<th>doing now</th><th>resumes</th><th></th></tr>" + "".join(rows) + "</table>"
    )


def queue(m: dict) -> str:
    q = m["queue"]
    if not q:
        return '<h2>Queue</h2><div class="dim">No queue snapshot yet (the supervisor writes one each poll).</div>'
    st = q.get("stages") or {}
    stages = [
        ("untriaged", "untriaged", ""),
        ("triage", "stage:triage", "stage:triage"),
        ("ready", "stage:ready", "stage:ready"),
        ("doing", "stage:doing", "stage:doing"),
        ("PR open", "stage:pr", "stage:pr"),
        ("live check", "stage:live", "stage:live"),
        ("acceptance", "stage:accept", "stage:accept"),
    ]
    pipe = "".join(
        f'<div class="stage">{label_link(m, lab, f"<b>{st.get(key, 0)}</b>") if lab else f"<b>{st.get(key, 0)}</b>"}'
        f'<span class="dim">{esc(name)}</span></div>'
        for name, key, lab in stages
    )
    side = " · ".join(
        f"{label_link(m, k, esc(k))} {st.get(k, 0)}"
        for k in ("stage:owner", "on hold", "hold:monitor", "ux:proposed")
    )
    prs = q.get("prs") or {}
    pr_text = ", ".join(f"{v} {k.lower()}" for k, v in sorted(prs.items())) or "none"
    return (
        f'<h2>Queue <span class="dim">{q.get("open", 0)} open issues</span></h2><div class="pipe">{pipe}</div>'
        f'<div class="dim" style="margin-top:8px">{side} · open PRs: {esc(pr_text)}</div>'
    )


def progress(m: dict) -> str:
    p1, p24 = m["progress"]["1h"], m["progress"]["24h"]

    def row(label, f):
        return f"<tr><td>{label}</td><td>{f(p1)}</td><td>{f(p24)}</td></tr>"

    def cyc(p):
        c = p["cycle_min"]
        return (
            f'{c["median"]} / {c["p90"]} min <span class="dim">(n={c["n"]})</span>'
            if c["n"]
            else "—"
        )

    def outs(p):
        o = p["outcomes"]
        order = [
            "ok",
            "handed back",
            "timed out",
            "failed",
            "crashed",
            "quota",
            "unclaimable",
            "other",
        ]
        return (
            ", ".join(
                f'<span class="{"ok" if k == "ok" else "warn" if k in ("handed back", "quota", "unclaimable") else "bad"}">'
                f"{o[k]} {k}</span>"
                for k in order
                if o.get(k)
            )
            or "—"
        )

    def toks(p):
        t = p["tokens"]
        return (
            ", ".join(
                f"{r} {(v['input'] + v['output']) / 1e6:.2f}M"
                for r, v in sorted(t.items())
            )
            or "—"
        )

    def cost(p):
        if p["cost_usd"] is None:
            return (
                '<span class="dim">not reported by the agent CLI</span>'
                if p["tokens"]
                else "—"
            )
        note = (
            f' <span class="dim">+ {p["cost_sessions_without_price"]} sessions on a CLI that reports no price</span>'
            if p["cost_sessions_without_price"]
            else ""
        )
        return f"${p['cost_usd']:.2f}{note}"

    reasons = "".join(
        f"<li>{esc(r)} <span class='dim'>×{n}</span></li>"
        for r, n in p24["handback_reasons"]
    )
    rv = p24.get("reviewer") or {}
    table = (
        "<table><tr><th></th><th>last hour</th><th>last 24 h</th></tr>"
        + row("Merged / issues closed", lambda p: f"{p['merges']} / {p['closed']}")
        + row("Handed back from a PR", lambda p: p["handed_back"])
        + row("Issue cycle, claim to close: median / p90", cyc)
        + row("Agent sessions ended", outs)
        + row("Tokens by role (in + out)", toks)
        + row("Cost", cost)
        + "</table>"
    )
    review = (
        f'<div class="dim" style="margin-top:6px">Reviewer, 24 h: {rv.get("calls", 0)} calls, '
        f"{(rv.get('input', 0) + rv.get('output', 0)) / 1e6:.2f}M tokens, verdicts "
        f"{esc(', '.join(f'{k} {v}' for k, v in sorted((rv.get('verdicts') or {}).items())) or 'none')}</div>"
    )
    ux = m["progress"].get("ux_scores")
    return (
        "<h2>Progress</h2>"
        + (f"<div style='margin-bottom:6px'>{esc(dm.ux_scores_text(ux))}</div>" if ux else "")
        + table
        + review
        + (
            f"<div style='margin-top:8px'>Why PRs came back (24 h):<ul>{reasons}</ul></div>"
            if reasons
            else ""
        )
    )


def agents_chart(m: dict) -> str:
    s = m["series"]
    w, h, pad = 560, 170, 30
    pts = s["agents"]
    if not pts:
        return ""
    peak = max([sum(c.values()) for _, c in pts] + [1])
    span = max(1.0, s["now"] - s["t0"])
    bars = []
    bw = (w - 2 * pad) / max(1, len(pts))
    for t, counts in pts:
        x = pad + (w - 2 * pad) * (t - s["t0"]) / span
        y = h - pad
        for role in ("worker", "merger", "tester"):
            n = counts.get(role, 0)
            if not n:
                continue
            bh = (h - 2 * pad) * n / peak
            y -= bh
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(1, bw - 1):.1f}" height="{bh:.1f}" '
                f'fill="{ROLE_COLORS[role]}"/>'
            )
    legend = "".join(
        f'<span class="chip" style="background:{ROLE_COLORS[r]}"></span>{r}'
        for r in ("worker", "merger", "tester")
    )
    return (
        f'<div><div class="dim">Agents at work, last 6 h (peak {peak}){legend}</div>'
        f'<svg width="{w}" height="{h}"><line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#333"/>'
        f'<text x="2" y="{pad + 4}" fill="#666" font-size="10">{peak}</text>{"".join(bars)}</svg></div>'
    )


def queue_chart(m: dict) -> str:
    s = m["series"]
    w, h, pad = 560, 170, 30
    span = max(1.0, s["now"] - s["t0"])
    q = [(t, v) for t, v in s["queue"]]
    merges = s["merges_per_hour"]
    top = max([v for _, v in q] + merges + [1])
    out = []
    bw = (w - 2 * pad) / max(1, len(merges))
    for k, n in enumerate(merges):
        if n:
            bh = (h - 2 * pad) * n / top
            out.append(
                f'<rect x="{pad + k * bw + 2:.1f}" y="{h - pad - bh:.1f}" width="{bw - 4:.1f}" height="{bh:.1f}" '
                f'fill="#5cc98a" opacity="0.55"/>'
            )
    if q:
        # A step line: the open count holds until the next change; the last value runs to now.
        coords, last = [], None
        for t, v in q:
            x = pad + (w - 2 * pad) * max(0.0, (t - s["t0"]) / span)
            y = h - pad - (h - 2 * pad) * v / top
            if last is not None:
                coords.append(f"{x:.1f},{last:.1f}")
            coords.append(f"{x:.1f},{y:.1f}")
            last = y
        coords.append(f"{w - pad:.1f},{last:.1f}")
        out.append(
            f'<polyline fill="none" stroke="#4e9af1" stroke-width="2" points="{" ".join(coords)}"/>'
        )
    return (
        f'<div><div class="dim">Open issues (line) and merges per hour (bars), last 6 h</div>'
        f'<svg width="{w}" height="{h}"><line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#333"/>'
        f'<text x="2" y="{pad + 4}" fill="#666" font-size="10">{top}</text>{"".join(out)}</svg></div>'
    )


def recent(m: dict) -> str:
    rows = "".join(
        f'<tr><td class="dim">{time.strftime("%H:%M", time.localtime(r["t"]))}</td><td>{esc(r["text"])}</td></tr>'
        for r in m["recent"]
    )
    return f"<h2>Recent</h2><table>{rows}</table>" if rows else ""


def page(m: dict) -> str:
    title = m["repo"] or pathlib.Path(m["root"]).name
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>issuelooper · {esc(title)}</title>
<meta http-equiv="refresh" content="15"><style>{CSS}</style></head><body>
<h1>issuelooper · {esc(title)}</h1>
<div class="dim">{time.strftime("%H:%M:%S", time.localtime(m["now"]))} · refreshes every 15 s ·
<a href="/api/state.json">JSON</a></div>
{header(m)}{attention(m)}{agents(m)}{queue(m)}{progress(m)}
<h2>Over time</h2><div class="cols">{agents_chart(m)}{queue_chart(m)}</div>
{recent(m)}
</body></html>"""


def session_page(run: pathlib.Path, seq: int) -> str:
    logdir = pathlib.Path(os.environ.get("POOL_LOGDIR", "").strip() or run / "pool")
    dirs = sorted(logdir.glob(f"{seq:04d}-*"))
    if not dirs:
        return f"<p>No session {seq}.</p>"
    d = dirs[0]

    def part(name: str, tail: int = 0) -> str:
        try:
            lines = (d / name).read_text(errors="replace").splitlines()
        except OSError:
            return ""
        if tail:
            lines = lines[-tail:]
        return f"<h2>{esc(name)}{' (last %d lines)' % tail if tail else ''}</h2><pre>{esc(chr(10).join(lines))}</pre>"

    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>session {seq}</title>"
        f"<meta http-equiv='refresh' content='15'><style>{CSS}</style></head><body>"
        f"<a href='/'>← dashboard</a><h1>{esc(d.name)}</h1><div class='dim'>{esc(d)}</div>"
        + part("session.log", 200)
        + part("auto.log", 100)
        + part("resumes.log")
        + part("prompt.txt")
        + "</body></html>"
    )


def make_handler(run: pathlib.Path):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path in ("/", "/index.html"):
                    body, ctype = page(dm.load(run)), "text/html; charset=utf-8"
                elif self.path == "/api/state.json":
                    body, ctype = (
                        json.dumps(dm.load(run), default=str),
                        "application/json",
                    )
                elif re.fullmatch(r"/session/\d+", self.path):
                    body, ctype = (
                        session_page(run, int(self.path.rsplit("/", 1)[1])),
                        "text/html; charset=utf-8",
                    )
                else:
                    self.send_error(404)
                    return
            except Exception as exc:  # noqa: BLE001 - a bad file must not take the page down
                body, ctype = (
                    f"<pre>dashboard error: {esc(exc)}</pre>",
                    "text/html; charset=utf-8",
                )
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return H


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="issuelooper dashboard")
    ap.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ISSUELOOPER_DASHBOARD_PORT") or 8780),
    )
    a = ap.parse_args(argv[1:])
    run = dm.run_dir()
    server = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(run))
    print(f"issuelooper dashboard: http://127.0.0.1:{a.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
