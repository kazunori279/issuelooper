#!/usr/bin/env python3
"""Tests for review_gate.py. Run: python3 review_gate_test.py

`gh` is faked by a script on CLAIM_GH that serves pull requests from FAKE_GH_DIR
(<n>.json and <n>.diff) and logs every write to FAKE_GH_DIR/writes.jsonl. The
judge is replaced by a function returning a canned reply. No network."""

import atexit
import json
import os
import pathlib
import shutil
import sys
import tempfile

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
com = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\d+)/comments(?:\?.*)?", path)
if com:
    try:
        print(open(os.path.join(d, f"comments-{com.group(1)}.json")).read(), end="")
    except OSError:
        print("[]")
    sys.exit(0)
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
    check("the tests a diff adds are judged too (lint as a test, source greps, presence only)",
          ("Tests this diff adds" in p, "ruff format --check" in p, "test_page_loads" in p), (True, True, True))
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

    # --- names the spec requires (run 11, PR #92: msg-user called test-specific) --------
    spec_root = pathlib.Path(tempfile.mkdtemp())
    (spec_root / "spec").mkdir()
    (spec_root / "spec" / "REQUIREMENTS.md").write_text(
        "## 12. Test hooks\n\n| testid | Element |\n| --- | --- |\n| `msg-user`, `msg-assistant` | bubbles |\n"
        "The settings key place_info is listed.\n")
    named = rg.split_files(diff_for([("src/static/app.js", [
        'el.dataset.testid = "x"; bubble.setAttribute("data-testid", "msg-user");',
        'const k = "place_info"; const z = "dash-board"; const status = "status";'])]))
    check("names the diff adds that the spec names: a test hook, a quoted identifier; not others",
          rg.spec_names(named, spec_root),
          ["`msg-user` (spec/REQUIREMENTS.md:5)", "`place_info` (spec/REQUIREMENTS.md:6)"])
    p = rg.build_prompt(7, "t", [], [], [], "d", False, spec=["`msg-user` (spec/REQUIREMENTS.md:5)"])
    check("the prompt lists them as the product's, never test_specific",
          ("- `msg-user` (spec/REQUIREMENTS.md:5)" in p, "never a `test_specific` finding" in p, "<<SPEC>>" in p),
          (True, True, False))
    check("none: said so", "(none found)" in rg.build_prompt(7, "t", [], [], [], "d", False), True)
    keep_spec = rg.SPEC_PATHS
    rg.SPEC_PATHS = ()
    check("REVIEW_SPEC_PATHS empty: no lookup", rg.spec_names(named, spec_root), [])
    rg.SPEC_PATHS = keep_spec

    # --- a re-review names new findings only on new lines (run 12, PR #111) ----------
    first = rg.split_files(diff_for([("src/concierge/weather.py", [
        "while len(daily) < 5 and daily:", "    daily.append(daily[-1])",
        "def wmo_to_condition(code):", "    return CODES.get(code) or \"cloudy\""])]))
    earlier = [{"t": 1, "sha": "e48dd89", "verdict": "reject", "seen": rg.seen_lines(first),
                "findings": [{"file": "src/concierge/weather.py", "what": "daily.append(daily[-1])", "kind": "fabricated"}]}]
    found = [{"file": "src/concierge/weather.py", "what": 'return CODES.get(code) or "cloudy"', "kind": "fabricated"},
             {"file": "src/concierge/weather.py", "what": "daily.append(daily[-1])", "kind": "fabricated"},
             {"file": "src/concierge/weather.py", "what": "temp = reading or 0.0", "kind": "fabricated"}]
    kept, dropped = rg.new_findings(found, earlier)
    check("a re-review keeps an earlier finding still there and one on a new line, and drops one on a line the "
          "first review read and did not name",
          ([f["what"] for f in kept], [f["what"] for f in dropped]),
          (["daily.append(daily[-1])", "temp = reading or 0.0"], ['return CODES.get(code) or "cloudy"']))
    check("the first review keeps everything", rg.new_findings(found, [])[1], [])
    p = rg.build_prompt(7, "t", [], [], [], "d", False, previous=rg.previous_text(earlier))
    check("the prompt shows the last findings to check first, and asks for every finding in one reply",
          ("At head e48dd89" in p, "daily.append(daily[-1])" in p, "Name every finding in this one reply" in p,
           "<<PREVIOUS>>" in p), (True, True, True, False))

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

    # Cohort 55: six tests that count a source file's lines merged in test-only PRs (acceptance
    # chores), which the review skipped. A static check rejects them without the model.
    count = [("tests/accept/test_accept_265.py", [
        "def test_split():", "    src_dir = REPO / 'src' / 'concierge' / 'static' / 'js'",
        "    lines = len((src_dir / 'app.js').read_text(encoding='utf-8').splitlines())", "    assert lines < 400"])]
    serve(5, "c5" * 20, count)
    check("a test-only PR whose test counts a source file's lines: rejected, judge not asked",
          (rg.review(5, hand=False), record("c5" * 20)["verdict"], record("c5" * 20)["by"], PROMPTS),
          (1, "reject", "static check", []))
    serve(6, "c6" * 20, [("tests/unit/test_cli.py", ["def test_one_line(capsys):", "    main()",
                                                     "    assert len(capsys.readouterr().err.splitlines()) == 1"])])
    check("counting lines of captured output is no structure test", (rg.review(6), record("c6" * 20)["verdict"]),
          (0, "skipped"))
    serve(7, "c7" * 20, [("tests/unit/test_lint.py", ["import subprocess", "def test_fmt():",
                                                      "    assert subprocess.run(['ruff', 'format', '--check', '.']).returncode == 0"])])
    check("a formatter run as a test: rejected", (rg.review(7, hand=False), record("c7" * 20)["verdict"]), (1, "reject"))

    # Run 10b #47: a rejection told the worker to undo a status label the issue and spec §11
    # ask for. The reviewer now sees the issue, and the author's answer gets the same head
    # reviewed again.
    (GHDIR / "issue-108.json").write_text(json.dumps({"body": "While reconnecting the pill reads Reconnecting "
                                                              "(R-CONN-3 in spec section 11)."}))
    label = [("src/i18n.js", ["error: 'Reconnecting',"])]
    serve(8, "c8" * 20, label)
    REPLY["value"] = '{"verdict": "reject", "reason": "keep Error", "findings": []}'
    check("a reject; the prompt carries the issue's text and the rule that the spec wins",
          (rg.review(8, hand=False), "pill reads Reconnecting" in PROMPTS[-1], "whatever you would prefer" in PROMPTS[-1]),
          (1, True, True))
    asked = len(PROMPTS)
    check("the same head without an answer: the reject stands, judge not asked", (rg.review(8), len(PROMPTS)), (1, asked))
    (GHDIR / "comments-8.json").write_text(json.dumps([{"body": "<!-- worker -->\nR-CONN-3 asks for Reconnecting.",
                                                         "created_at": "2099-01-01T00:00:00Z", "user": {"login": "bot"}}]))
    REPLY["value"] = '{"verdict": "pass", "reason": "the spec asks for it"}'
    check("the author's answer citing the spec: the same head is reviewed again, with the answer",
          (rg.review(8), len(PROMPTS), "R-CONN-3 asks for Reconnecting." in PROMPTS[-1]), (0, asked + 1, True))
    writes()
    PROMPTS.clear()

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
    check("after the cap a new head is still reviewed, not let through",
          (rg.review(3), record("c3" * 20)["verdict"], len(PROMPTS)), (1, "owner", n + 1))
    w = writes()
    check("and a third reject goes to the owner, not to another worker",
          ([(x["method"], x["path"]) for x in w][1:], w[-1]["body"],
           "review-override: c3c3c3c3c3c3" in w[0]["body"]["body"]),
          ([("DELETE", "repos/o/r/issues/43/labels/stage:pr"), ("POST", "repos/o/r/issues/43/labels")],
           {"labels": ["stage:owner"]}, True))
    check("the head waiting for the owner stays refused", (rg.review(3), writes()), (1, []))
    (GHDIR / "comments-3.json").write_text(json.dumps([
        {"id": 7, "body": "<!-- monitor -->\nreview-override: c3c3c3c3c3c3 the monitor may not",
         "user": {"login": "loop-bot", "type": "User"}, "author_association": "OWNER"},
        {"id": 8, "body": "review-override: c3c3c3c3c3c3 strangers may not",
         "user": {"login": "drive-by", "type": "User"}, "author_association": "NONE"},
        {"id": 9, "body": "review-override: c1c1c1c1c1c1 an older head", "user": {"login": "kaz", "type": "User"},
         "author_association": "OWNER"}]))
    check("an override by an agent, a stranger or for another head does nothing", rg.review(3), 1)
    (GHDIR / "comments-3.json").write_text(json.dumps([
        {"id": 10, "body": "Fine by me.\nreview-override: c3c3c3c3c3c3 the price is the spec's list price",
         "user": {"login": "kaz", "type": "User"}, "author_association": "OWNER"}]))
    r_ = rg.review(3)
    r = record("c3" * 20)
    check("the owner's override naming this head lets it through, recorded",
          (r_, r["verdict"], r["by"], r["reason"], r["comment"]),
          (0, "override", "kaz", "the price is the spec's list price", 10))

    import judge  # noqa: E402

    REPLY["value"] = judge.JudgeError("quota", "RESOURCE_EXHAUSTED")
    serve(4, "d1" * 20, prod, branch="issue-44")
    check("judge error: held, retried later (exit 2)", rg.review(4), 2)
    r = record("d1" * 20)
    check(
        "judge error: record",
        (r["verdict"], r["reason"].startswith("quota:"), r["errors"]),
        ("error", True, 1),
    )
    check("judge error: asked again on the next run", (rg.review(4), record("d1" * 20)["errors"]), (2, 2))
    writes()
    check("the third error on the head: the owner's", (rg.review(4), record("d1" * 20)["verdict"]), (1, "owner"))
    w = writes()
    check("with a comment and stage:owner", (w[0]["body"]["body"].count("failed 3 time(s)"), w[-1]["body"]),
          (1, {"labels": ["stage:owner"]}))
    REPLY["value"] = "not json at all"
    serve(5, "e1" * 20, prod)
    check("unparsable reply: held", rg.review(5), 2)
    check("unparsable reply: error", record("e1" * 20)["verdict"], "error")
    check("the allowed verdicts", rg.ALLOWS, {"pass", "skipped", "override"})

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
    gone = rg.left_out(tests + long_prod, text)
    check("what was left out: the test file, the production file whole",
          gone, [("tests/test_big.py", "cut", False)])
    check("and the prompt says so", rg.cut_note(gone),
          "; every production file is whole; left out of other files: `tests/test_big.py` (cut)")
    many = diff_for([(f"tests/test_{k}.py", ["x" * 300] * 10) for k in range(30)] + [("src/a.py", ["y"] * 5)])
    text, _ = rg.cap_diff(many, 3000)
    note = rg.cut_note(rg.left_out(many, text))
    check("many test files left out: named, then counted", ("every production file is whole" in note,
          "(omitted)" in note, "more" in note), (True, True, True))
    huge = diff_for([("src/one.py", ["z" * 100] * 80), ("src/two.py", ["w" * 100] * 80)])
    text, _ = rg.cap_diff(huge, 6000, 6000)
    check("production cut: the prompt names it", rg.cut_note(rg.left_out(huge, text)),
          "; production files not whole: `src/one.py` (cut), `src/two.py` (cut)")
    # PR #121 and #140: production over REVIEW_CHARS was cut and the PR rejected as unread.
    text, cut = rg.cap_diff(huge + tests, 6000, 20000)
    check("production over REVIEW_CHARS but within its own budget goes whole; tests get nothing left",
          (cut, rg.left_out(huge + tests, text)), (True, [("tests/test_big.py", "omitted", False)]))
    uneven = diff_for([("src/small.py", ["s" * 50] * 20), ("src/big.py", ["b" * 100] * 200)])
    text, cut = rg.cap_diff(uneven, 3000, 8000)
    check("over its budget: the short file whole, the long one gets the rest, the budget used",
          (cut, rg.left_out(uneven, text), 7000 < len(text) <= 8000), (True, [("src/big.py", "cut", True)], True))
    serve(7, "a7" * 20, [("src/a.py", ["price = item.get('price')"]), ("tests/test_a.py", ["x" * 100] * 60)])
    keep_chars = rg.MAX_CHARS
    rg.MAX_CHARS = 2500
    try:
        REPLY["value"] = '{"verdict": "pass", "reason": "fine"}'
        rg.review(7, hand=False)
    finally:
        rg.MAX_CHARS = keep_chars
    check("a review over the limit: the prompt and the record say what was left out",
          ("(cut to 2500 characters; every production file is whole; left out of other files: `tests/test_a.py` (cut))"
           in PROMPTS[-1], record("a7" * 20)["left_out"]), (True, ["tests/test_a.py (cut)"]))
    serve(8, "a8" * 20, [("src/a.py", ["価格 = item.get('price')" * 4] * 400)])
    keep = rg.MAX_CHARS, rg.PROMPT_BYTES
    rg.MAX_CHARS, rg.PROMPT_BYTES = 2500, 40000
    try:
        rg.review(8, hand=False)
    finally:
        rg.MAX_CHARS, rg.PROMPT_BYTES = keep
    check("a prompt too long for one argument: the production budget shrinks to fit",
          (len(PROMPTS[-1].encode()) <= 40000, record("a8" * 20)["left_out"]), (True, ["src/a.py (cut)"]))
    unchanged = ("diff --git a/src/a.py b/src/a.py\n@@ -1,40 +1,41 @@\n" + " ctx\n" * 20 + "+new\n"
                 + " ctx\n" * 20)
    check("context far from a change is dropped", rg.compact(unchanged).count(" ctx"), 6)

    # --- split PRs: only verbatim moves are summarized --------------------------------
    def two_files(removed: list[str], added: list[str]) -> str:
        return ("diff --git a/src/big.py b/src/big.py\nindex 1..2 100644\n--- a/src/big.py\n+++ b/src/big.py\n"
                f"@@ -1,{len(removed) + 1} +1,2 @@\n import os\n" + "".join(f"-{ln}\n" for ln in removed)
                + "+from helpers import *\n"
                + "diff --git a/src/helpers.py b/src/helpers.py\nnew file mode 100644\n--- /dev/null\n"
                f"+++ b/src/helpers.py\n@@ -0,0 +1,{len(added)} @@\n" + "".join(f"+{ln}\n" for ln in added))

    moved_fn = [f"def helper_{k}(value):" if k % 5 == 0 else f"    total_{k} = compute(value, {k})" for k in range(60)]
    split_diff = two_files(["from x import compute"] + moved_fn,
                           ["from x import compute"] + moved_fn
                           + ["def price_of(item):", "    return item.get('price') or 19.99"])
    text, n = rg.summarize_moves(split_diff)
    check("a verbatim move: one line on each side, new code whole",
          ("+[moved verbatim: 61 lines from src/big.py, not repeated]" in text,
           "-[moved verbatim: 61 lines to src/helpers.py, not repeated]" in text, "or 19.99" in text,
           "total_7" in text, n),
          (True, True, True, False, 122))
    guarded = ["def save(user, record):", "    require_admin(user)", "    db.write(record)",
               "    audit.log(user, record)", "    return record"]
    reordered = [guarded[0], guarded[2], guarded[1], guarded[3], guarded[4]]
    text, n = rg.summarize_moves(two_files(guarded, reordered))
    check("calls reordered on the way (the check after the write): nothing folded",
          (n, "+    db.write(record)\n+    require_admin(user)" in text), (0, True))
    reindented = ["    " + ln for ln in guarded]
    text, n = rg.summarize_moves(two_files(guarded, reindented))
    check("re-indented on the way (now inside another block): nothing folded",
          (n, "+        require_admin(user)" in text), (0, True))
    text, n = rg.summarize_moves(two_files(guarded, ["def save(user, record):", "    return record"]))
    check("a removal that is not a move (the check is gone): shown in full",
          (n, "-    require_admin(user)" in text, "removed]" in text), (0, True, False))
    text, n = rg.summarize_moves(two_files(guarded, guarded + guarded))
    check("a block added twice: only one copy is the move", (n, text.count("+    require_admin(user)")), (10, 1))
    (GHDIR / "issue-140.json").write_text(json.dumps({
        "title": "Split src/big.py: it is where parallel work collides",
        "body": "<!-- merger -->\n<!-- split -->\n`src/big.py` has 900 lines on main."}))
    (GHDIR / "issue-141.json").write_text(json.dumps({
        "title": "Split it", "body": "<!-- worker -->\n<!-- split -->\nnot the supervisor's"}))
    check("split issues are the supervisor's, by mark or by title",
          (rg.is_split_issue("o/r", 140), rg.is_split_issue("o/r", 141), rg.is_split_issue("o/r", 999)),
          (True, False, False))
    keep_chars, keep_prod = rg.MAX_CHARS, rg.PROD_CHARS
    rg.MAX_CHARS = rg.PROD_CHARS = 3000
    try:
        for n_pr, issue, sha in ((40, 140, "f" * 40), (41, 141, "e" * 40)):
            (GHDIR / f"{n_pr}.json").write_text(json.dumps(
                {"title": f"PR {n_pr}", "body": f"Fixes #{issue}", "head": {"sha": sha, "ref": f"issue-{issue}"}}))
            (GHDIR / f"{n_pr}.diff").write_text(split_diff)
            rg.review(n_pr, hand=False)
            last = PROMPTS[-1]
            if n_pr == 40:
                check("a split PR over the limit: summarized, not cut",
                      ("[moved verbatim: 61 lines" in last, "(a split: 122 lines it moves verbatim" in last,
                       "cut to" in last, record(sha).get("moved")), (True, True, False, 122))
            else:
                check("any other PR over the limit is cut as before",
                      ("[moved" in last, "cut to 3000 characters" in last), (False, True))
        (GHDIR / "42.json").write_text(json.dumps(
            {"title": "PR 42", "body": "Fixes #140", "head": {"sha": "d" * 40, "ref": "issue-140"}}))
        (GHDIR / "42.diff").write_text(two_files(moved_fn, ["    " + ln for ln in moved_fn]))
        rg.review(42, hand=False)
        check("a split that re-indents what it moves stays over the limit: cut, as before",
              ("[moved" in PROMPTS[-1], "cut to 3000 characters" in PROMPTS[-1]), (False, True))
    finally:
        rg.MAX_CHARS, rg.PROD_CHARS = keep_chars, keep_prod
finally:
    os.environ.clear()
    os.environ.update(saved)
    tmp.cleanup()

if failures:
    print("\n".join(failures))
    print(f"FAIL: {len(failures)} failure(s)")
    sys.exit(1)
print("OK: review_gate_test")
