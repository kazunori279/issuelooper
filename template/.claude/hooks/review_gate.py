#!/usr/bin/env python3
"""Review gate: a second model reads a pull request before it is merged.

    python3 "$SHARED_REPO_PATH/.claude/hooks/review_gate.py" --pr N

The judge (judge.py) reads the PR's diff once, with no tools, and answers pass or
reject. It looks for what tests and sweeps do not catch: data that is made up and
shown to users as real (invented values when a source fails, returns nothing or
omits a field), code that exists to pass tests or evaluators (special-casing
fixture or sample inputs, test-mode branches, injection seams), and whatever the
project's rules file adds. The judge is the reviewer role's CLI and model (see
judge.py; JUDGE=agy:<model> or claude:<model> picks another).

Settings (environment, or .issuelooper/config.toml through config.py):

    REVIEW_GATE          on|off (1|0), default on. Off writes a `skipped` record.
    REVIEW_PATHS         comma-separated path prefixes that hold production code,
                         default src/. Empty means every file in the repository.
                         Test files, fixtures, docs and the framework's own files
                         never count. A diff that touches none of them is `skipped`.
    REVIEW_RULES         the project's rules file, relative to the repository root,
                         default .issuelooper/review_rules.md. Its text (without
                         <!-- comments -->) goes into the prompt; missing = no rules.
                         A fenced block marked `prescan` holds extra pre-scan
                         regexes, one per line, and is not sent as rule text.
    REVIEW_MAX_REJECTS   rejects of one PR before it merges unreviewed, default 2.
    REVIEW_DIR           where records go, default /tmp/issuelooper-review.
    REVIEW_PROMPT        another prompt file with the same <<...>> fields, default
                         review_prompt.md next to this script.
    REVIEW_FIXTURES      comma-separated globs of JSON test fixtures, relative to
                         the repository root (default tests/fixtures/**/*.json,
                         spec/fixtures/**/*.json); ID-like values and input strings
                         from them that show up in production lines are pre-scan hits.
    REVIEW_CHARS         the most diff characters sent, default 30000.
    REVIEW_LONG_FN       function length logged as long, default 150.
    REVIEW_REFACTOR_LINES  a diff that grows a function past this many lines writes
                         a review_long_function event, default 300.
    REVIEW_EVENTS_FILE   the event log, default $POOL_LOGDIR/../review_events.jsonl,
                         else $REVIEW_DIR/events.jsonl.

A keyword pre-scan of the added lines goes into the prompt as hints; the judge
decides. Structural findings (long functions, copy-pasted blocks) are only logged:
used as reject reasons they rejected many clean diffs.

Every outcome writes a record for the PR's head sha, $REVIEW_DIR/<sha>.json, and
one event line. role_guard.py (Gate 5) refuses `gh pr merge` unless the record
for the current head allows it:

    pass      the judge passed it, or a pass for the same diff at an earlier head (a rebase)
    skipped   no production file changed, or the gate is off
    error     the judge failed twice (one retry): logged, and the merge goes ahead
    cap       REVIEW_MAX_REJECTS rejects of this PR already: logged, merge goes ahead
    reject    nobody may merge this head

On a reject this script hands the work back: a signed <!-- merger --> comment on
the PR with the reasons, and stage:pr swapped for stage:triage on the issue, so the
pool starts a worker on the same PR. ccpool.py runs it before every merge of a
mergeable PR; a merger session runs it before landing a conflicting one.

Exit status: 0 the merge may go ahead, 1 rejected (already handed back), 2 the PR
could not be read (retry).
"""

from __future__ import annotations

import argparse
import ast
import glob
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402

config.apply()


def _paths(raw: str) -> tuple[str, ...]:
    """REVIEW_PATHS -> prefixes; () means the whole repository."""
    out = []
    for p in raw.split(","):
        p = p.strip()
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if p in ("", "."):
            return ()
        out.append(p)
    return tuple(out)


GH = os.environ.get("CLAIM_GH", "gh")
GATE_ON = os.environ.get("REVIEW_GATE", "on").strip().lower() not in ("off", "0", "false", "no")
RECORDS = pathlib.Path(os.environ.get("REVIEW_DIR", "/tmp/issuelooper-review"))
MAX_CHARS = int(os.environ.get("REVIEW_CHARS", "30000"))
MAX_REJECTS = int(os.environ.get("REVIEW_MAX_REJECTS", "2"))
LONG_FN = int(os.environ.get("REVIEW_LONG_FN", "150"))
REFACTOR_LINES = int(os.environ.get("REVIEW_REFACTOR_LINES", "300"))
PROMPT = pathlib.Path(os.environ.get("REVIEW_PROMPT") or HERE / "review_prompt.md")
PATHS = _paths(os.environ.get("REVIEW_PATHS", "src/"))
RULES = os.environ.get("REVIEW_RULES", ".issuelooper/review_rules.md")
FIXTURES = os.environ.get("REVIEW_FIXTURES", "tests/fixtures/**/*.json,spec/fixtures/**/*.json")
ALLOWS = {"pass", "skipped", "error", "cap"}
LINK = re.compile(r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?|refs?)\s+#(\d+)\b")
BRANCH = re.compile(r"^issue-(\d+)\b")
# Never production code: the framework's files, and (when REVIEW_PATHS is the
# whole repository) the usual test, doc and spec directories.
FRAMEWORK = (".claude/", ".agents/", ".githooks/", ".github/", ".issuelooper/")
NOT_PROD = ("tests/", "test/", "docs/", "spec/")
TEST_FILE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|__mocks__|fixtures|testdata)/"
    r"|(?:^|/)(?:test_[^/]*|conftest\.py)$"
    r"|_test\.[^/]+$|\.(?:test|spec)\.[^/]+$"
)
CODE_EXT = (".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".html")
NO_RULES = "(This project has no rules file; only the two kinds above apply.)"

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "reject"]},
        "reason": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "file": {"type": "string"},
                    "what": {"type": "string"},
                },
                "required": ["what"],
            },
        },
    },
    "required": ["verdict", "reason"],
}

# Added production lines the judge is told to look at: placeholder media, mock and
# test-mode checks, injection seams, synthetic input, and names of canned data.
SUSPECT = [
    r"picsum\.photos|via\.placeholder|placehold\.(?:it|co)|placeholder\.com|dummyimage\.com|loremflickr|source\.unsplash\.com",
    r"\bexample\.(?:com|org|net)\b",
    r"(?i)lorem ipsum",
    r"\bFake\w*",
    r"\bMock\w*",
    r"(?i)test[_ -]?mode|\bis_?testing\b",
    r"PYTEST_CURRENT_TEST|NODE_ENV\W{1,8}test\b|\bunittest\.mock\b|\bjest\.fn\b|\bsinon\b",
    r"__class__\.__name__",
    r"assert_called|_mock_",
    r"window\.__\w+",
    r"(?i)(?:sessionStorage|localStorage)\.getItem\(\s*[\"'`][^\"'`]*(?:mock|fake|test|override)",
    r"(?i)[?&](?:mock|fake|test)\w*=",
    r"(?i)createOscillator|\bfake_?(?:frame|audio|video|stream|reading)s?\b",
    r"(?i)\b(?:fallback|default|dummy|sample|canned|demo|placeholder|hard_?coded)_?(?:data|result|response|item|user|record|answer|image|photo|video|url|list)s?\b",
]


# --- GitHub (tests replace these three) ----------------------------------------


def repo_name() -> str:
    repo = os.environ.get("GH_REPO", "").strip()
    if repo:
        return repo
    out = subprocess.run(
        [GH, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return out.stdout.strip()


def gh_get(path: str, accept: str = "") -> str:
    args = [GH, "api", path]
    if accept:
        args[2:2] = ["-H", f"Accept: {accept}"]
    out = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path}: {out.stderr.strip()[-300:]}")
    return out.stdout


def gh_write(method: str, path: str, body: dict | None) -> bool:
    out = subprocess.run(
        [GH, "api", "-X", method, path]
        + (["--input", "-"] if body is not None else []),
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        log_event("review_write_error", path=path, error=out.stderr.strip()[-200:])
    return out.returncode == 0


# --- records and events ---------------------------------------------------------


def events_file() -> pathlib.Path:
    if os.environ.get("REVIEW_EVENTS_FILE"):
        return pathlib.Path(os.environ["REVIEW_EVENTS_FILE"])
    pool = os.environ.get("POOL_LOGDIR", "")
    if pool:
        return pathlib.Path(pool).parent / "review_events.jsonl"
    return RECORDS / "events.jsonl"


def log_event(kind: str, **kv) -> None:
    rec = {"t": round(time.time(), 1), "kind": kind, **kv}
    try:
        events_file().parent.mkdir(parents=True, exist_ok=True)
        with open(events_file(), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def load_record(sha: str) -> dict | None:
    try:
        rec = json.loads((RECORDS / f"{sha.lower()}.json").read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def write_record(sha: str, rec: dict) -> None:
    RECORDS.mkdir(parents=True, exist_ok=True)
    tmp = RECORDS / f".{sha}.tmp"
    tmp.write_text(json.dumps(rec, indent=1))
    tmp.replace(RECORDS / f"{sha.lower()}.json")


def records_for(pr: int) -> list[dict]:
    out = []
    for p in sorted(RECORDS.glob("*.json")) if RECORDS.is_dir() else []:
        try:
            rec = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("pr") == pr:
            out.append(rec)
    return out


# --- the diff -------------------------------------------------------------------


def split_files(diff: str) -> list[tuple[str, str]]:
    """(path at head, that file's part of the diff)."""
    out = []
    for part in re.split(r"(?m)^(?=diff --git )", diff):
        if not part.strip():
            continue
        m = re.search(r"(?m)^\+\+\+ b/(.+)$", part) or re.match(
            r"diff --git a/\S+ b/(\S+)", part
        )
        out.append((m.group(1).strip() if m else "", part))
    return out


def is_prod(path: str) -> bool:
    """A file under REVIEW_PATHS that is not a test, fixture, doc or framework file."""
    if (
        not path
        or path.startswith(FRAMEWORK)
        or path.endswith((".md", ".lock"))
        or TEST_FILE.search(path)
    ):
        return False
    if not PATHS:
        return not path.startswith(NOT_PROD)
    return path.startswith(PATHS)


def compact(part: str, context: int = 3) -> str:
    """A file's diff with context lines kept only within `context` lines of a change."""
    lines = part.splitlines(keepends=True)
    changed = [i for i, ln in enumerate(lines)
               if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    keep = set()
    for i in changed:
        keep.update(range(i - context, i + context + 1))
    out, gap = [], False
    for i, ln in enumerate(lines):
        if not ln.startswith(" ") or i in keep:
            out.append(ln)
            gap = False
        elif not gap:
            out.append(" ...\n")
            gap = True
    return "".join(out)


def cap_diff(diff: str, limit: int) -> tuple[str, bool]:
    """Production files get the whole budget before any test, fixture or doc file, with
    context far from a change dropped. Equal shares once cut a long production file
    in half while a test went whole, and a branch in the part cut merged unseen."""
    if len(diff) <= limit:
        return diff, False
    files = split_files(diff)
    prod = [compact(p) for path, p in files if is_prod(path)]
    rest = [compact(p) for path, p in files if not is_prod(path)]
    cut = False
    if sum(map(len, prod)) > limit:
        share = max(1500, limit // max(1, len(prod)))
        prod = [p if len(p) <= share else p[:share] + "\n... (cut)\n" for p in prod]
        rest, cut = [], True
    room = limit - sum(map(len, prod))
    if rest and sum(map(len, rest)) > room:
        share = max(0, room // len(rest))
        rest = [p if len(p) <= share else p[:share] + "\n... (cut)\n" for p in rest if share > 200]
        cut = True
    out = "".join(prod + rest)
    return out[:limit], cut or len(out) > limit


def diff_key(diff: str) -> str:
    """The diff without line numbers and blob ids: equal across a clean rebase."""
    keep = [ln for ln in diff.splitlines() if not ln.startswith(("index ", "@@"))]
    return hashlib.sha256("\n".join(keep).encode()).hexdigest()[:20]


def added_lines(part: str) -> list[tuple[int, str]]:
    """(line number at head, text) of the lines a file's diff adds."""
    out, n = [], 0
    for ln in part.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
        if m:
            n = int(m.group(1))
            continue
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+"):
            out.append((n, ln[1:]))
            n += 1
        elif ln.startswith(" "):
            n += 1
    return out


FIXTURE_INPUT_KEYS = {"match", "query", "prompt", "input", "q", "question", "utterance"}


def fixture_tokens(root: pathlib.Path, globs: str = "") -> set[str]:
    """Values from the test fixtures that production code has no reason to contain:
    the inputs a fixture feeds in (under keys like "query" or "prompt") and ID-like
    strings (8-16 letters and digits, with both)."""
    toks: set[str] = set()

    def walk(x, key=None):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str):
            if key in FIXTURE_INPUT_KEYS and len(x.strip()) >= 4:
                toks.add(x.strip())
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{8,16}", x)
                and re.search(r"\d", x)
                and re.search(r"[A-Za-z]", x)
            ):
                toks.add(x)

    paths: set[pathlib.Path] = set()
    for g in (globs or FIXTURES).split(","):
        if g.strip():
            paths.update(pathlib.Path(p) for p in glob.glob(str(root / g.strip()), recursive=True))
    for p in sorted(paths):
        try:
            walk(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return toks


def prescan(
    files: list[tuple[str, str]],
    tokens: set[str],
    extra: list[str] | tuple[str, ...] = (),
    cap: int = 25,
) -> list[str]:
    """Added production lines that match SUSPECT, a project's own pre-scan
    regexes (`extra`), or contain a fixture token."""
    patterns = SUSPECT + list(extra)
    flags = []
    for path, part in files:
        if not is_prod(path):
            continue
        for _, line in added_lines(part):
            low = line.lower()
            hits = {t for t in tokens if t.lower() in low}
            hits |= {m.group(0) for rx in patterns for m in [re.search(rx, line)] if m}
            if hits:
                flags.append(
                    f"{path}: {', '.join(sorted(hits)[:3])}: {line.strip()[:160]}"
                )
                if len(flags) >= cap:
                    return flags
    return flags


JS_START = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([\w$]+)\s*\(|"
    r"^\s*(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s+)?(?:function\b[^(]*\([^)]*\)|\([^)]*\)\s*=>|[\w$]+\s*=>)\s*\{|"
    r"^\s*(?:async\s+)?([\w$]+)\s*\([^)]*\)\s*\{\s*$"
)
JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "with", "return", "function"}


def function_spans(path: str, text: str) -> list[tuple[str, int, int]]:
    """(name, first line, last line) of every function in a file at the head."""
    spans = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spans.append((node.name, node.lineno, node.end_lineno or node.lineno))
        return spans
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = JS_START.match(line)
        if not m:
            continue
        name = next(g for g in m.groups() if g)
        if name in JS_KEYWORDS:
            continue
        depth, seen = 0, False
        for j in range(i, len(lines)):
            code = re.sub(
                r"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`]*`|//.*$)", "", lines[j]
            )
            depth += code.count("{") - code.count("}")
            seen = seen or "{" in code
            if seen and depth <= 0:
                spans.append((name, i + 1, j + 1))
                break
    return spans


def grown_functions(files: list[tuple[str, str]], content_at, limit: int) -> list[dict]:
    """Functions longer than `limit` lines at the head that this diff adds lines to."""
    out = []
    for path, part in files:
        if not is_prod(path) or not path.endswith(CODE_EXT):
            continue
        changed = {n for n, _ in added_lines(part)}
        if not changed:
            continue
        text = content_at(path)
        if not text:
            continue
        for name, a, b in function_spans(path, text):
            size = b - a + 1
            mine = len([n for n in changed if a <= n <= b])
            if size > limit and mine:
                out.append(
                    {
                        "file": path,
                        "func": name,
                        "lines": size,
                        "start": a,
                        "end": b,
                        "added": mine,
                    }
                )
    return out


def long_functions(files: list[tuple[str, str]], content_at) -> list[str]:
    """Functions longer than LONG_FN lines at the head that this diff adds lines to."""
    return [
        f"{g['file']}: {g['func']}() is {g['lines']} lines (lines {g['start']}-{g['end']}); "
        f"this diff adds {g['added']} of them"
        for g in grown_functions(files, content_at, LONG_FN)
    ][:10]


def duplicate_blocks(files: list[tuple[str, str]], window: int = 6) -> list[str]:
    """Runs of `window` added non-trivial lines that the diff adds more than once."""
    seen: dict[str, tuple[str, int]] = {}
    out = []
    for path, part in files:
        if not is_prod(path):
            continue
        lines = [(n, t.strip()) for n, t in added_lines(part) if len(t.strip()) >= 12]
        for k in range(len(lines) - window + 1):
            chunk = lines[k : k + window]
            if chunk[-1][0] - chunk[0][0] > window * 2:
                continue  # not contiguous enough to be one block
            key = "\n".join(t for _, t in chunk)
            if key in seen and (seen[key][0], seen[key][1]) != (path, chunk[0][0]):
                p0, n0 = seen[key]
                if p0 == path and abs(n0 - chunk[0][0]) < window:
                    continue
                out.append(
                    f"{path}:{chunk[0][0]} repeats {p0}:{n0} ({window}+ lines): {chunk[0][1][:80]}"
                )
                if len(out) >= 5:
                    return out
            else:
                seen.setdefault(key, (path, chunk[0][0]))
    return out


# --- the project's rules --------------------------------------------------------


def rules_file() -> pathlib.Path:
    p = pathlib.Path(RULES)
    return p if p.is_absolute() else config.repo_root() / p


def load_rules(path: pathlib.Path | None = None) -> tuple[str, list[str]]:
    """The rules file -> (rule text for the prompt, extra pre-scan regexes).

    HTML comments are dropped. A fenced block opened with ```prescan holds one
    regex per line; it is taken out of the text, and a regex that does not compile
    is logged and skipped. A file with nothing left but headings counts as no rules."""
    try:
        text = (path or rules_file()).read_text()
    except (OSError, UnicodeDecodeError):
        return "", []
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    patterns: list[str] = []

    def take(m: re.Match) -> str:
        for ln in m.group(1).splitlines():
            if not ln.strip():
                continue
            try:
                re.compile(ln.strip())
            except re.error as exc:
                log_event("review_rules_bad_regex", regex=ln.strip()[:200], error=str(exc))
                continue
            patterns.append(ln.strip())
        return ""

    text = re.sub(r"(?ms)^```prescan[ \t]*\n(.*?)^```[ \t]*$\n?", take, text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not re.search(r"(?m)^(?!\s*#)\S", text):
        text = ""
    return text, patterns


# --- the verdict ----------------------------------------------------------------


def parse_verdict(reply) -> dict:
    """The judge's reply -> {"verdict", "reason", "findings"}; ValueError when unusable."""
    import judge as judge_mod

    obj = judge_mod.extract_json(reply)
    errs = judge_mod.schema_errors(obj, SCHEMA)
    if errs:
        raise ValueError("; ".join(errs[:3]))
    if obj["verdict"] == "reject" and not str(obj.get("reason", "")).strip():
        raise ValueError("a reject without a reason")
    return {
        "verdict": obj["verdict"],
        "reason": str(obj.get("reason", ""))[:1500],
        "findings": [
            {k: str(f.get(k, ""))[:300] for k in ("kind", "file", "what")}
            for f in obj.get("findings", [])[:10]
            if isinstance(f, dict)
        ],
    }


def build_prompt(n, title, flags, longfn, dupes, text, cut, rules: str = "") -> str:
    """The prompt file with its <<FIELD>>s filled in one pass, so text from the
    diff or the rules file is never taken for a field."""
    values = {
        "N": str(n or "?"),
        "TITLE": title,
        "PATHS": ", ".join(f"`{p}`" for p in PATHS) or "the repository",
        "RULES": rules.strip() or NO_RULES,
        "LONG": str(LONG_FN),
        "FLAGS": "\n".join(f"- {f}" for f in flags) or "(none)",
        "LONGFN": "\n".join(f"- {f}" for f in longfn) or "(none)",
        "DUPES": "\n".join(f"- {f}" for f in dupes) or "(none)",
        "TRUNC": f" (cut to {MAX_CHARS} characters)" if cut else "",
        "DIFF": text,
    }
    return re.sub(
        r"<<([A-Z]+)>>", lambda m: values.get(m.group(1), m.group(0)), PROMPT.read_text()
    )


def judge():
    import judge as judge_mod

    return judge_mod.Judge(log_dir=RECORDS / "judge-logs")


JUDGE_USAGE: dict = {}  # the last call's tokens, for the record


def ask_judge(prompt: str) -> str:
    """The judge's reply text; judge.JudgeError after one retry."""
    j = judge()
    try:
        return j.ask(prompt, max_tokens=2000, timeout=240, tries=2, schema=SCHEMA)
    finally:
        JUDGE_USAGE.clear()
        JUDGE_USAGE.update(j.usage)


def judge_name() -> str:
    try:
        return judge().name
    except (ImportError, ValueError):
        return "judge"


def hand_back(repo: str, pr: int, issue: int | None, v: dict) -> None:
    lines = [
        "<!-- merger -->",
        f"**Review gate: rejected.** Not merged. {v['reason']}",
    ]
    if v["findings"]:
        lines.append("")
        lines += [
            f"- [{f.get('kind') or 'finding'}] `{f.get('file') or '?'}`: {f.get('what')}"
            for f in v["findings"]
        ]
    lines += [
        "",
        "Fix this on this branch, re-sweep, push and update the PR body; the review runs "
        "again on the new head. Show an honest empty state or error instead of invented "
        "data, and remove code that treats test or sample inputs specially.",
    ]
    gh_write("POST", f"repos/{repo}/issues/{pr}/comments", {"body": "\n".join(lines)})
    if issue:
        gh_write("DELETE", f"repos/{repo}/issues/{issue}/labels/stage:pr", None)
        gh_write(
            "POST", f"repos/{repo}/issues/{issue}/labels", {"labels": ["stage:triage"]}
        )


def pr_issue(info: dict) -> int | None:
    m = BRANCH.match(((info.get("head") or {}).get("ref")) or "")
    if m:
        return int(m.group(1))
    m = LINK.search(info.get("body") or "")
    return int(m.group(1)) if m else None


def review(pr: int, hand: bool = True) -> int:
    repo = repo_name()
    try:
        info = json.loads(gh_get(f"repos/{repo}/pulls/{pr}"))
        head = info["head"]["sha"]
        diff = gh_get(
            f"repos/{repo}/pulls/{pr}", accept="application/vnd.github.v3.diff"
        )
    except (
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"review_gate: could not read PR #{pr}: {exc}", file=sys.stderr)
        log_event("review_unreadable", pr=pr, error=str(exc)[:300])
        return 2
    issue = pr_issue(info)
    base = {"pr": pr, "issue": issue, "sha": head, "t": round(time.time(), 1)}

    def done(verdict: str, **kv) -> int:
        rec = dict(base, verdict=verdict, **kv)
        write_record(head, rec)
        log_event("review", **{k: v for k, v in rec.items() if k != "t"})
        ok = verdict in ALLOWS
        print(
            f"review_gate: PR #{pr} head {head[:12]}: {verdict}"
            + (f" ({kv.get('reason', '')[:300]})" if kv.get("reason") else "")
        )
        return 0 if ok else 1

    if not GATE_ON:
        return done("skipped", reason="the review gate is off (REVIEW_GATE)")
    old = load_record(head)
    if old and old.get("verdict") in ALLOWS | {"reject"}:
        print(f"review_gate: PR #{pr} head {head[:12]} already reviewed: {old['verdict']}")
        return 0 if old["verdict"] in ALLOWS else 1

    files = split_files(diff)
    prod = [p for p, _ in files if is_prod(p)]
    if not prod:
        return done(
            "skipped",
            reason="no production file changed",
            files=[p for p, _ in files][:20],
        )
    key = diff_key(diff)
    earlier = records_for(pr)
    same = next(
        (r for r in earlier if r.get("key") == key and r.get("verdict") == "pass"), None
    )
    if same:
        return done(
            "pass",
            key=key,
            by="same-diff",
            reason=f"same diff as the pass at {same.get('sha', '')[:12]}",
        )
    rejects = sum(1 for r in earlier if r.get("verdict") == "reject")
    if rejects >= MAX_REJECTS:
        return done(
            "cap",
            key=key,
            reason=f"{rejects} rejects of this PR already; merging without another review",
        )

    contents: dict[str, str] = {}

    def content_at(path: str) -> str:
        if path not in contents:
            try:
                contents[path] = gh_get(
                    f"repos/{repo}/contents/{path}?ref={head}",
                    accept="application/vnd.github.raw",
                )
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                contents[path] = ""
        return contents[path]

    rules, extra = load_rules()
    flags = prescan(files, fixture_tokens(config.repo_root()), extra)
    longfn = long_functions(files, content_at)
    # Only logged, never a reason to reject.
    grown = grown_functions(files, content_at, REFACTOR_LINES)
    if grown:
        log_event(
            "review_long_function",
            pr=pr,
            issue=issue,
            sha=head,
            limit=REFACTOR_LINES,
            functions=grown[:10],
        )
    dupes = duplicate_blocks(files)
    text, cut = cap_diff(diff, MAX_CHARS)
    prompt = build_prompt(
        issue, info.get("title", ""), flags, longfn, dupes, text, cut, rules
    )
    hints = {
        "key": key,
        "flags": len(flags),
        "long_functions": longfn,
        "dupes": len(dupes),
        "chars": len(text),
        "cut": cut,
    }
    by = judge_name()
    t0 = time.time()
    try:
        v = parse_verdict(ask_judge(prompt))
    except Exception as exc:  # noqa: BLE001 - a broken judge never blocks the queue
        kind = getattr(exc, "kind", type(exc).__name__)
        return done(
            "error",
            by=by,
            **({"usage": dict(JUDGE_USAGE)} if JUDGE_USAGE else {}),
            reason=f"{kind}: {str(exc)[:300]}",
            s=round(time.time() - t0, 1),
            **hints,
        )
    s = round(time.time() - t0, 1)
    if JUDGE_USAGE:
        hints["usage"] = dict(JUDGE_USAGE)
    if v["verdict"] == "pass":
        return done(
            "pass", by=by, reason=v["reason"], findings=v["findings"], s=s, **hints
        )
    rc = done(
        "reject",
        by=by,
        reason=v["reason"],
        findings=v["findings"],
        s=s,
        round=rejects + 1,
        **hints,
    )
    if hand:
        hand_back(repo, pr, issue, v)
        print(
            f"review_gate: handed back: comment on PR #{pr}, #{issue} moved from stage:pr to stage:triage. Stop here."
        )
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument(
        "--no-hand-back", action="store_true", help="record the verdict only"
    )
    a = ap.parse_args(argv)
    return review(a.pr, hand=not a.no_hand_back)


if __name__ == "__main__":
    sys.exit(main())
