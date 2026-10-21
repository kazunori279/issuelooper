#!/usr/bin/env python3
"""File a sample's seed backlog as GitHub issues, with labels and dependencies.

    python3 examples/seed_issues.py BACKLOG.md --repo OWNER/NAME [--dry-run]

The backlog is markdown: each `## #N title` heading starts an issue, the `meta:` line
under it holds the labels and the issues it is blocked by
(`meta: type:bug P0 needs:live | blocked_by: 2 3`), and everything up to the next
heading is the body. Lines starting with `>` are notes and are not filed.

Every issue is filed at `stage:triage`, so workers can claim it at once. A blocker
becomes a native GitHub dependency and a "Blocked by: #N" line in the body.

Running it again files only the issues whose title is not in the repository yet, so
an interrupted run can be resumed. In a fresh repository the GitHub numbers equal the
backlog's; otherwise the dependencies follow the new numbers and a warning says so.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time

WRITE_GAP = 1.0  # seconds after each write: GitHub's secondary rate limit


def parse(path: pathlib.Path) -> list[dict]:
    issues, cur = [], None
    for line in path.read_text().splitlines():
        m = re.match(r"^## #(\d+) (.+)$", line)
        if m:
            cur = {
                "number": int(m.group(1)),
                "title": m.group(2).strip(),
                "body": [],
                "labels": [],
                "blocked_by": [],
            }
            issues.append(cur)
        elif cur is None or line.startswith(">") or line.strip() == "---":
            continue
        elif line.startswith("meta:"):
            labels, _, blocked = line[5:].partition("|")
            cur["labels"] = labels.split()
            cur["blocked_by"] = [int(x) for x in re.findall(r"\d+", blocked)]
        else:
            cur["body"].append(line)
    numbers = [i["number"] for i in issues]
    if numbers != list(range(1, len(issues) + 1)):
        sys.exit(f"{path}: issues must be numbered #1..#{len(issues)} in order")
    for i in issues:
        unknown = [b for b in i["blocked_by"] if b not in numbers or b == i["number"]]
        if unknown:
            sys.exit(f"{path}: #{i['number']} is blocked by unknown issue(s) {unknown}")
        i["body"] = "\n".join(i["body"]).strip()
    return issues


def gh(*args: str, input: str | None = None, write: bool = False) -> str:
    """One gh call; a write is throttled and retried through the rate limit."""
    for attempt in range(20):
        r = subprocess.run(("gh", *args), input=input, capture_output=True, text=True)
        if r.returncode == 0:
            if write:
                time.sleep(WRITE_GAP)
            return r.stdout.strip()
        if "rate limit" not in (r.stderr + r.stdout).lower() or attempt == 19:
            sys.exit(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()[-500:]}")
        print(f"rate limited; waiting a minute ({attempt + 1})", flush=True)
        time.sleep(60)
    raise AssertionError


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("backlog", type=pathlib.Path)
    ap.add_argument("--repo", required=True, help="OWNER/NAME")
    ap.add_argument("--dry-run", action="store_true", help="print what would be filed")
    a = ap.parse_args()
    issues = parse(a.backlog)
    if a.dry_run:
        for i in issues:
            blocked = (
                f"  (blocked by {', '.join(f'#{b}' for b in i['blocked_by'])})"
                if i["blocked_by"]
                else ""
            )
            print(f"#{i['number']} [{' '.join(i['labels'])}] {i['title']}{blocked}")
        return

    have = gh(
        "label",
        "list",
        "--repo",
        a.repo,
        "--limit",
        "200",
        "--json",
        "name",
        "--jq",
        ".[].name",
    ).split("\n")
    missing = sorted(
        {x for i in issues for x in i["labels"] + ["stage:triage"]} - set(have)
    )
    if missing:
        sys.exit(
            f"{a.repo} lacks the labels {missing}: run install.sh on the repository first"
        )

    # One JSON object per line: --paginate applies --jq to each page.
    found = gh(
        "api",
        f"repos/{a.repo}/issues?state=all&per_page=100",
        "--paginate",
        "--jq",
        ".[] | select(.pull_request == null) | {number, id, title}",
    )
    existing = [json.loads(x) for x in found.splitlines() if x.strip()]
    by_title = {e["title"]: e for e in existing}
    num, ids = {}, {}  # backlog number -> GitHub number, GitHub number -> issue id
    for e in existing:
        ids[e["number"]] = e["id"]

    for i in issues:
        if i["title"] in by_title:
            num[i["number"]] = by_title[i["title"]]["number"]
            continue
        body = i["body"]
        if i["blocked_by"]:
            body += "\n\nBlocked by: " + ", ".join(
                f"#{num.get(b, b)}" for b in i["blocked_by"]
            )
        payload = {
            "title": i["title"],
            "body": body,
            "labels": ["stage:triage", *i["labels"]],
        }
        out = json.loads(
            gh(
                "api",
                f"repos/{a.repo}/issues",
                "--input",
                "-",
                input=json.dumps(payload),
                write=True,
            )
        )
        num[i["number"]] = out["number"]
        ids[out["number"]] = out["id"]
        print(f"#{out['number']} {i['title']}", flush=True)

    for i in issues:
        if not i["blocked_by"]:
            continue
        n = num[i["number"]]
        url = f"repos/{a.repo}/issues/{n}/dependencies/blocked_by"
        linked = set(json.loads(gh("api", url, "--jq", "[.[].number]") or "[]"))
        for b in i["blocked_by"]:
            if num[b] not in linked:
                gh("api", url, "-F", f"issue_id={ids[num[b]]}", write=True)

    moved = [(b, n) for b, n in num.items() if b != n]
    if moved:
        b, n = moved[0]
        print(
            f"warning: backlog #{b} is #{n} on GitHub; the sample's kickoff and README "
            "name the backlog's numbers",
            file=sys.stderr,
        )
    print(f"{len(issues)} issues in {a.repo}")


if __name__ == "__main__":
    main()
