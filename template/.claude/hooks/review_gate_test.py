#!/usr/bin/env python3
"""Tests for review_gate.py. Run: python3 review_gate_test.py

`gh` is faked by a script on CLAIM_GH that serves pull requests from FAKE_GH_DIR
(<n>.json and <n>.diff) and logs every write to FAKE_GH_DIR/writes.jsonl. The
judge is replaced by a function returning a canned reply. No network."""

import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


FAKE_GH = r"""#!/usr/bin/env python3
import json, os, re, sys
d = os.environ["FAKE_GH_DIR"]
args = sys.argv[1:]
if args[:1] != ["api"]:
    sys.exit(1)
if "-X" in args:
    i = args.index("-X")
    body = sys.stdin.read() if "--input" in args else ""
    with open(os.path.join(d, "writes.jsonl"), "a") as f:
        f.write(json.dumps({"method": args[i + 1], "path": args[i + 2],
                            "body": json.loads(body) if body else None}) + "\n")
    sys.exit(0)
accept = args[args.index("-H") + 1] if "-H" in args else ""
path = args[-1]
m = re.fullmatch(r"repos/[^/]+/[^/]+/pulls/(\d+)", path)
issue = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)", path)
if not m and not issue:
    print("not found", file=sys.stderr); sys.exit(1)
name = f"issue-{issue.group(1)}.json" if issue else m.group(1) + (".diff" if "diff" in accept else ".json")
try:
    print(open(os.path.join(d, name)).read(), end="")
except OSError:
    print("HTTP 404", file=sys.stderr); sys.exit(1)
"""

tmp = tempfile.TemporaryDirectory()
T = pathlib.Path(tmp.name)
ROOT = T / "repo"
GHDIR = T / "gh"
for p in (ROOT / ".issuelooper", ROOT / "tests/fixtures", GHDIR):
    p.mkdir(parents=True)
(T / "gh.py").write_text(FAKE_GH)
(T / "gh.py").chmod(0o755)
(ROOT / "tests/fixtures/search.json").write_text(
    json.dumps({"cases": [{"query": "red running shoes", "id": "sku9X41ab"}]})
)
saved = dict(os.environ)
for k in list(os.environ):
    if k.startswith(("REVIEW_", "CCROLE_", "JUDGE", "POOL_")):
        del os.environ[k]
os.environ.update(
    SHARED_REPO_PATH=str(ROOT),
    CLAIM_GH=str(T / "gh.py"),
    FAKE_GH_DIR=str(GHDIR),
    GH_REPO="o/r",
    REVIEW_DIR=str(T / "records"),
    REVIEW_PATHS="src/,web/",
    REVIEW_MAX_REJECTS="2",
)
import review_gate as rg  # noqa: E402

PROMPTS = []
REPLY = {"value": '{"verdict": "pass", "reason": "fine"}'}


def fake_ask(prompt):
    PROMPTS.append(prompt)
    r = REPLY["value"]
    if isinstance(r, Exception):
        raise r
    return r


rg.ask_judge = fake_ask
rg.judge_name = lambda: "fake"


def diff_for(files):
    out = []
    for path, lines in files:
        out.append(
            f"diff --git a/{path} b/{path}\nindex 111..222 100644\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,0 +1,{len(lines)} @@\n" + "".join(f"+{ln}\n" for ln in lines)
        )
    return "".join(out)


def serve(n, sha, files, branch=None):
    (GHDIR / f"{n}.json").write_text(
        json.dumps(
            {
                "title": f"PR {n}",
                "body": f"Fixes #{n + 100}",
                "head": {"sha": sha, "ref": branch or f"feature-{n}"},
            }
        )
    )
    (GHDIR / f"{n}.diff").write_text(diff_for(files))


def writes():
    p = GHDIR / "writes.jsonl"
    out = [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []
    p.unlink(missing_ok=True)
    return out


def record(sha):
    return json.loads((T / "records" / f"{sha.lower()}.json").read_text())


try:
    # --- production paths ---------------------------------------------------------
    check("paths", rg.PATHS, ("src/", "web/"))
    check("prod src", rg.is_prod("src/app/main.py"), True)
    check("prod web", rg.is_prod("web/index.html"), True)
    check("not under paths", rg.is_prod("scripts/x.py"), False)
    check("test file under src", rg.is_prod("src/app/main_test.py"), False)
    check("test dir under src", rg.is_prod("src/tests/test_x.py"), False)
    check("spec file under web", rg.is_prod("web/app.spec.ts"), False)
    check("markdown under src", rg.is_prod("src/README.md"), False)
    check("_paths empty = whole repo", rg._paths(""), ())
    check("_paths dot = whole repo", rg._paths("."), ())
    check("_paths ./src", rg._paths("./src/, lib/"), ("src/", "lib/"))
    keep = rg.PATHS
    rg.PATHS = ()
    check("whole repo: any file", rg.is_prod("lib/x.py"), True)
    check("whole repo: not docs", rg.is_prod("docs/x.py"), False)
    check("whole repo: not framework", rg.is_prod(".claude/hooks/x.py"), False)
    rg.PATHS = keep

    # --- rules ----------------------------------------------------------------------
    rules_path = T / "rules.md"
    rules_path.write_text(
        "# Rules\n<!-- guidance\nnot sent -->\n\n## Keys stay on the server\n\n"
        "Reject client code that reads SECRET_KEY.\n\n"
        "```prescan\nSECRET_KEY\n(unclosed\n```\n"
    )
    text, extra = rg.load_rules(rules_path)
    check("rules text kept", "Reject client code that reads SECRET_KEY." in text, True)
    check("comment dropped", "guidance" in text, False)
    check("prescan block dropped", "```" in text, False)
    check("prescan regexes (bad one skipped)", extra, ["SECRET_KEY"])
    rules_path.write_text("# Rules\n\n<!-- nothing yet -->\n")
    check("headings only = no rules", rg.load_rules(rules_path), ("", []))
    check("missing file = no rules", rg.load_rules(T / "nope.md"), ("", []))
    tpl = HERE.parents[1] / ".issuelooper/review_rules.md"
    if tpl.exists():
        check("template rules are all commented out", rg.load_rules(tpl), ("", []))
    (ROOT / ".issuelooper/review_rules.md").write_text(
        "Reject anything that reads SECRET_KEY.\n"
    )
    check(
        "REVIEW_RULES default path",
        rg.rules_file(),
        ROOT / ".issuelooper/review_rules.md",
    )

    # --- prompt -------------------------------------------------------------------
    p = rg.build_prompt(
        7, "Add search", ["src/a.py: Mock: x"], [], [], "+x <<RULES>>\n", False
    )
    check("no rules line", rg.NO_RULES in p, True)
    check("paths in prompt", "`src/`, `web/`" in p, True)
    check("diff text not substituted", "+x <<RULES>>" in p, True)
    check("title", '#7 ("Add search")' in p, True)
    check("flags", "- src/a.py: Mock: x" in p, True)
    check(
        "no fields left",
        [
            f
            for f in ("<<N>>", "<<PATHS>>", "<<FLAGS>>", "<<DIFF>>", "<<TRUNC>>")
            if f in p
        ],
        [],
    )
    p = rg.build_prompt(7, "t", [], [], [], "d", True, "Keys stay on the server.")
    check("rules in prompt", "Keys stay on the server." in p, True)
    check("rules replace the none line", rg.NO_RULES in p, False)
    check("cut note", f"(cut to {rg.MAX_CHARS} characters)" in p, True)
    check(
        "kinds in reply format", '"project_rule"' in p and '"test_specific"' in p, True
    )

    # --- pre-scan -------------------------------------------------------------------
    files = rg.split_files(
        diff_for(
            [
                ("src/img.py", ['url = "https://picsum.photos/200"', "x = 1"]),
                (
                    "src/search.py",
                    [
                        'if q == "red running shoes":',
                        "    return CANNED",
                        'k = "sku9X41ab"',
                    ],
                ),
                (
                    "web/app.js",
                    ["const k = SECRET_KEY;", "if (window.__mockResults) {}"],
                ),
                ("tests/test_img.py", ["MockClient()"]),
            ]
        )
    )
    toks = rg.fixture_tokens(ROOT)
    check("fixture tokens", toks >= {"red running shoes", "sku9X41ab"}, True)
    flags = rg.prescan(files, toks, ["SECRET_KEY"])
    joined = "\n".join(flags)
    check("placeholder image flagged", "picsum.photos" in joined, True)
    check("fixture input flagged", "red running shoes" in joined, True)
    check("fixture id flagged", "sku9X41ab" in joined, True)
    check("project regex flagged", "SECRET_KEY" in joined, True)
    check("injection seam flagged", "window.__mockResults" in joined, True)
    check("test files not scanned", "MockClient" in joined, False)
    check("plain line not flagged", any(f.endswith(": x = 1") for f in flags), False)
    for line in (
        "if os.environ.get('TEST_MODE'):",
        "return DEFAULT_RESULTS",
        "if hasattr(c, 'assert_called'):",
    ):
        hit = rg.prescan(rg.split_files(diff_for([("src/x.py", [line])])), set())
        check(f"generic pattern: {line}", len(hit), 1)

    # --- verdict parsing ------------------------------------------------------------
    v = rg.parse_verdict(
        'ok: {"verdict": "reject", "reason": "made up", "findings": [{"file": "src/a.py", "what": "x or 5", "kind": "fabricated"}]}'
    )
    check("verdict", (v["verdict"], v["findings"][0]["kind"]), ("reject", "fabricated"))
    for bad in (
        '{"verdict": "reject", "reason": " "}',
        '{"verdict": "maybe", "reason": "x"}',
        "no json",
        '{"reason": "x"}',
    ):
        try:
            rg.parse_verdict(bad)
            failures.append(f"parse_verdict({bad!r}) did not raise")
        except ValueError:
            pass

    # --- review(): skip, pass, reject, cap ------------------------------------------
    serve(1, "A" * 40, [("docs/x.md", ["hi"]), ("tests/test_a.py", ["assert 1"])])
    check("docs only: exit", rg.review(1), 0)
    r = record("A" * 40)
    check("docs only: skipped", r["verdict"], "skipped")
    check("record schema", {"pr", "issue", "sha", "t", "verdict"} <= set(r), True)
    check("record issue from body", r["issue"], 101)
    check("skipped: judge not asked", PROMPTS, [])

    prod = [("src/a.py", ["price = item.get('price') or 19.99"])]
    serve(2, "b1" * 20, prod, branch="issue-42")
    REPLY["value"] = '{"verdict": "pass", "reason": "fine"}'
    check("pass: exit", rg.review(2), 0)
    r = record("b1" * 20)
    check("pass: record", (r["verdict"], r["by"], r["issue"]), ("pass", "fake", 42))
    check(
        "pass: rules in prompt",
        "Reject anything that reads SECRET_KEY." in PROMPTS[-1],
        True,
    )
    check("pass: no writes", writes(), [])
    check("same head again: exit", rg.review(2), 0)
    check("same head again: judge not asked", len(PROMPTS), 1)
    serve(2, "b2" * 20, prod, branch="issue-42")
    check("rebase, same diff: exit", rg.review(2), 0)
    check("rebase, same diff: by", record("b2" * 20)["by"], "same-diff")
    check("rebase: judge not asked", len(PROMPTS), 1)

    REPLY["value"] = (
        '{"verdict": "reject", "reason": "invented price", "findings": [{"file": "src/a.py", "what": "or 19.99", "kind": "fabricated"}]}'
    )
    serve(3, "c1" * 20, prod, branch="issue-43")
    check("reject: exit", rg.review(3), 1)
    r = record("c1" * 20)
    check(
        "reject: record",
        (r["verdict"], r["round"], r["reason"]),
        ("reject", 1, "invented price"),
    )
    w = writes()
    check(
        "reject: comment, unlabel, label",
        [(x["method"], x["path"]) for x in w],
        [
            ("POST", "repos/o/r/issues/3/comments"),
            ("DELETE", "repos/o/r/issues/43/labels/stage:pr"),
            ("POST", "repos/o/r/issues/43/labels"),
        ],
    )
    check(
        "reject: comment signed",
        w[0]["body"]["body"].startswith("<!-- merger -->"),
        True,
    )
    check(
        "reject: comment has finding",
        "[fabricated] `src/a.py`: or 19.99" in w[0]["body"]["body"],
        True,
    )
    check("reject: back to triage", w[2]["body"], {"labels": ["stage:triage"]})
    check("rejected head again: exit 1", rg.review(3), 1)
    check("rejected head again: no writes", writes(), [])

    serve(3, "c2" * 20, prod + [("src/b.py", ["y = 2"])], branch="issue-43")
    check("second reject: exit", rg.review(3), 1)
    check("second reject: round", record("c2" * 20)["round"], 2)
    writes()
    n = len(PROMPTS)
    serve(3, "c3" * 20, prod + [("src/b.py", ["y = 3"])], branch="issue-43")
    check("cap: exit", rg.review(3), 0)
    check("cap: record", record("c3" * 20)["verdict"], "cap")
    check("cap: judge not asked", len(PROMPTS), n)

    import judge  # noqa: E402

    REPLY["value"] = judge.JudgeError("quota", "RESOURCE_EXHAUSTED")
    serve(4, "d1" * 20, prod)
    check("judge error: exit", rg.review(4), 0)
    r = record("d1" * 20)
    check(
        "judge error: record",
        (r["verdict"], r["reason"].startswith("quota:")),
        ("error", True),
    )
    REPLY["value"] = "not json at all"
    serve(5, "e1" * 20, prod)
    check("unparsable reply: exit", rg.review(5), 0)
    check("unparsable reply: error", record("e1" * 20)["verdict"], "error")

    check("unreadable PR: exit 2", rg.review(99), 2)
    check("main --no-hand-back", rg.main(["--pr", "1", "--no-hand-back"]), 0)

    rg.GATE_ON = False
    serve(6, "f1" * 20, prod)
    check("gate off: exit", rg.review(6), 0)
    check("gate off: skipped", record("f1" * 20)["verdict"], "skipped")
    rg.GATE_ON = True

    ev = (T / "records" / "events.jsonl").read_text().splitlines()
    check(
        "events written",
        all(json.loads(ln)["kind"].startswith("review") for ln in ev) and len(ev) > 5,
        True,
    )

    # --- diff cap -------------------------------------------------------------------
    big = diff_for(
        [("tests/test_big.py", ["x" * 100] * 50), ("src/a.py", ["y" * 100] * 50)]
    )
    text, cut = rg.cap_diff(big, 3000)
    check("cap cut", cut, True)
    check("cap puts production first", text.startswith("diff --git a/src/a.py"), True)
    check("cap within limit", len(text) <= 3000, True)
    long_prod = diff_for([("src/places.py", ["line"] * 700 + ["is_legacy_mock = True"])])
    tests = diff_for([("tests/test_big.py", ["x" * 100] * 50)])
    text, cut = rg.cap_diff(tests + long_prod, 6000)
    check(
        "the whole production file, then tests in the room left",
        (cut, "is_legacy_mock" in text, text.startswith("diff --git a/src/places.py"), "tests/test_big.py" in text),
        (True, True, True, True),
    )
    unchanged = ("diff --git a/src/a.py b/src/a.py\n@@ -1,40 +1,41 @@\n" + " ctx\n" * 20 + "+new\n"
                 + " ctx\n" * 20)
    check("context far from a change is dropped", rg.compact(unchanged).count(" ctx"), 6)

    # --- split PRs: moved code summarized ----------------------------------------------
    moved_fn = [f"def helper_{k}(value):" if k % 5 == 0 else f"    total_{k} = compute(value, {k})" for k in range(60)]
    split_diff = (
        "diff --git a/src/big.py b/src/big.py\nindex 1..2 100644\n--- a/src/big.py\n+++ b/src/big.py\n"
        "@@ -1,62 +1,3 @@\n import os\n-from x import compute\n" + "".join(f"-{ln}\n" for ln in moved_fn)
        + "+from helpers import *\n"
        + "diff --git a/src/helpers.py b/src/helpers.py\nnew file mode 100644\n--- /dev/null\n+++ b/src/helpers.py\n"
        "@@ -0,0 +1,63 @@\n+from x import compute\n" + "".join(f"+    {ln}\n" for ln in moved_fn)
        + "+def price_of(item):\n+    return item.get('price') or 19.99\n"
    )
    text, n = rg.summarize_moves(split_diff)
    check("a split: the moved block is one line, the removed block one line, new code whole",
          ("+[moved: 61 lines that this diff removes elsewhere, not repeated]" in text,
           "-[61 lines removed]" in text, "or 19.99" in text, "total_7" in text, n),
          (True, True, True, False, 122))
    (GHDIR / "issue-140.json").write_text(json.dumps({
        "title": "Split src/big.py: it is where parallel work collides",
        "body": "<!-- merger -->\n<!-- split -->\n`src/big.py` has 900 lines on main."}))
    (GHDIR / "issue-141.json").write_text(json.dumps({
        "title": "Split it", "body": "<!-- worker -->\n<!-- split -->\nnot the supervisor's"}))
    check("split issues are the supervisor's, by mark or by title",
          (rg.is_split_issue("o/r", 140), rg.is_split_issue("o/r", 141), rg.is_split_issue("o/r", 999)),
          (True, False, False))
    keep_chars = rg.MAX_CHARS
    rg.MAX_CHARS = 3000
    try:
        for n_pr, issue, sha in ((40, 140, "f" * 40), (41, 141, "e" * 40)):
            (GHDIR / f"{n_pr}.json").write_text(json.dumps(
                {"title": f"PR {n_pr}", "body": f"Fixes #{issue}", "head": {"sha": sha, "ref": f"issue-{issue}"}}))
            (GHDIR / f"{n_pr}.diff").write_text(split_diff)
            rg.review(n_pr, hand=False)
            last = PROMPTS[-1]
            if n_pr == 40:
                check("a split PR over the limit: summarized, not cut",
                      ("[moved: 61 lines" in last, "(a split: 122 moved or removed lines" in last, "cut to" in last,
                       record(sha).get("moved")), (True, True, False, 122))
            else:
                check("any other PR over the limit is cut as before",
                      ("[moved:" in last, "cut to 3000 characters" in last), (False, True))
    finally:
        rg.MAX_CHARS = keep_chars
finally:
    os.environ.clear()
    os.environ.update(saved)
    tmp.cleanup()

if failures:
    print("\n".join(failures))
    print(f"FAIL: {len(failures)} failure(s)")
    sys.exit(1)
print("OK: review_gate_test")
