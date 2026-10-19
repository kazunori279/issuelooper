#!/usr/bin/env python3
"""Tests for judge.py. Run: python3 judge_test.py

The agy and claude CLIs are faked by one script whose reply FAKE_MODE picks; it
logs its argv, working directory and that directory's files to FAKE_LOG."""

import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import judge  # noqa: E402

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


FAKE = r"""#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                        "files": sorted(os.listdir("."))}) + "\n")
mode = os.environ.get("FAKE_MODE", "")
usage = {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 5,
         "cache_read_tokens": 0, "total_tokens": 125}
if mode == "agy_ok":
    print(json.dumps({"event": "step_update", "step_update": {"usage": usage}}))
    print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "x",
          "structured_output": {"verdict": "pass", "reason": "ok"}, "usage": usage}}))
elif mode == "agy_quota":
    print(json.dumps({"event": "result", "result": {"status": "ERROR",
          "error": "RESOURCE_EXHAUSTED: quota"}}))
elif mode == "agy_auth":
    print("not signed in", file=sys.stderr); sys.exit(1)
elif mode == "agy_empty":
    print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": ""}}))
elif mode == "claude_ok":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
          "result": 'Here it is: {"verdict": "reject", "reason": "made up"} done',
          "usage": {"input_tokens": 50, "output_tokens": 10,
                    "cache_read_input_tokens": 7}}))
elif mode == "claude_text":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
          "result": "plain words"}))
elif mode == "claude_list":
    print(json.dumps([{"type": "system"}, {"type": "result", "subtype": "success",
          "is_error": False, "result": '{"verdict": "pass", "reason": "fine"}'}]))
elif mode == "claude_bad_schema":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
          "result": '{"verdict": "maybe", "reason": "?"}'}))
elif mode == "claude_auth":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
          "result": "Invalid API key. Please run /login"}))
elif mode == "claude_net":
    print("Error: connection refused", file=sys.stderr); sys.exit(1)
elif mode == "claude_max_turns":
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": False,
          "result": ""}))
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "reject"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
}

judge.time.sleep = lambda s: None  # no backoff waits in tests
saved = dict(os.environ)


def env(**kv):
    for k in (
        "JUDGE",
        "CCROLE_REVIEWER_CLI",
        "CCROLE_CLI",
        "CCROLE_MODEL_REVIEWER",
        "CCROLE_MODEL",
    ):
        os.environ.pop(k, None)
    os.environ.update(kv)


try:
    # --- which backend -----------------------------------------------------------
    env()
    check("default spec", judge.default_spec(), "agy")
    env(CCROLE_CLI="claude")
    check("CCROLE_CLI", judge.default_spec(), "claude")
    env(CCROLE_CLI="claude", CCROLE_REVIEWER_CLI="agy", CCROLE_MODEL="m1")
    check("reviewer CLI wins, shared model", judge.default_spec(), "agy:m1")
    env(CCROLE_MODEL="m1", CCROLE_MODEL_REVIEWER="m2")
    check("reviewer model wins", judge.default_spec(), "agy:m2")
    env(CCROLE_MODEL="", CCROLE_CLI="")
    check("empty settings = CLI default", judge.default_spec(), "agy")
    env(JUDGE="claude:big", CCROLE_CLI="agy", CCROLE_MODEL="m1")
    check("JUDGE wins", judge.default_spec(), "claude:big")
    env()
    check("caller model", judge.default_spec("m3"), "agy:m3")
    check("parse agy", judge.parse_spec("agy"), ("agy", ""))
    check("parse claude:m", judge.parse_spec("claude: m "), ("claude", "m"))
    for bad in ("other", "gpt:x", ""):
        try:
            judge.parse_spec(bad)
            failures.append(f"parse_spec({bad!r}) did not raise")
        except ValueError:
            pass
    check("name with model", judge.Judge("claude:m").name, "claude:m")
    check("name without model", judge.Judge("agy").name, "agy")

    # --- command lines -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        cmd = judge.Judge("agy:fast").agy_cmd("Q", 300, d, SCHEMA)
        check("agy cmd", cmd[:5], ["agy", "-p", "Q", "--model", "fast"])
        check("agy schema file", cmd[-2:], ["--json-schema", str(d / "schema.json")])
        check("agy schema written", json.loads((d / "schema.json").read_text()), SCHEMA)
        check("agy print timeout", cmd[cmd.index("--print-timeout") + 1], "5m")
        cmd = judge.Judge("agy").agy_cmd("Q", 300, d)
        check("agy without model", "--model" in cmd, False)
        check("agy without schema", "--json-schema" in cmd, False)
    c = judge.Judge("claude:big")
    cmd = c.claude_cmd("Q")
    check("claude prompt last", cmd[-2:], ["-p", "Q"])
    check("claude json", cmd[cmd.index("--output-format") + 1], "json")
    check("claude model", cmd[cmd.index("--model") + 1], "big")
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    check(
        "claude denies Bash/Edit/Write/Read",
        {"Bash", "Edit", "Write", "Read"} - set(denied),
        {"Read"},
    )
    check("claude no tools allowed", "--allowedTools" in cmd, False)
    check("claude one turn", cmd[cmd.index("--max-turns") + 1], "1")
    cmd = judge.Judge("claude").claude_cmd("Q", images=[("image/png", b"x")])
    check("claude images allow Read", cmd[cmd.index("--allowedTools") + 1], "Read")
    check("claude without model", "--model" in cmd, False)

    # --- calls through the fake CLIs -------------------------------------------------
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        fake = d / "fake_cli"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        log = d / "log.jsonl"
        os.environ.update(
            FAKE_LOG=str(log), JUDGE_AGY_BIN=str(fake), JUDGE_CLAUDE_BIN=str(fake)
        )

        def calls():
            out = (
                [json.loads(ln) for ln in log.read_text().splitlines()]
                if log.exists()
                else []
            )
            log.unlink(missing_ok=True)
            return out

        def run(spec, mode, **kw):
            os.environ["FAKE_MODE"] = mode
            j = judge.Judge(spec, log_dir=d / "logs")
            try:
                return j, j.ask("Question?", **kw), None
            except judge.JudgeError as e:
                return j, None, e

        j, out, err = run("agy:m", "agy_ok", schema=SCHEMA, tries=1)
        check(
            "agy structured output",
            json.loads(out),
            {"verdict": "pass", "reason": "ok"},
        )
        check("agy usage", j.last_usage["total_tokens"], 125)
        check("agy usage totals", j.usage["calls"], 1)
        (c1,) = calls()
        check("agy ran in an empty temp dir", c1["files"], ["schema.json"])
        check("agy temp dir removed", pathlib.Path(c1["cwd"]).exists(), False)
        check("agy log kept", len(list((d / "logs").glob("judge-*.jsonl"))) >= 1, True)

        j, out, err = run("agy:m", "agy_quota", tries=3)
        check("agy quota kind", err and err.kind, "quota")
        check("quota is retried", len(calls()), 3)
        j, out, err = run("agy", "agy_auth", tries=3)
        check("agy auth kind", err and err.kind, "auth")
        check("auth is not retried", len(calls()), 1)
        j, out, err = run("agy", "agy_empty", tries=1)
        check("agy empty reply", err and err.kind, "parse")
        calls()

        os.environ["FAKE_MODE"] = "agy_ok"
        j = judge.Judge("agy")
        j.ask(
            "Look",
            tries=1,
            images=[("image/jpeg", b"\xff\xd8", "shot.jpg"), ("image/png", b"p")],
        )
        (c1,) = calls()
        check("agy images written", c1["files"], ["image-2.png", "shot.jpg"])
        check("agy image note", "view_file" in c1["argv"][1], True)

        j, out, err = run("claude:big", "claude_ok", schema=SCHEMA, tries=1)
        check(
            "claude json out",
            json.loads(out),
            {"verdict": "reject", "reason": "made up"},
        )
        check(
            "claude usage",
            (
                j.last_usage["input_tokens"],
                j.last_usage["cache_read_tokens"],
                j.last_usage["total_tokens"],
            ),
            (50, 7, 60),
        )
        (c1,) = calls()
        check("claude ran in an empty temp dir", c1["files"], [])
        check("claude argv", c1["argv"][-2:], ["-p", "Question?"])
        j, out, err = run("claude", "claude_text", tries=1)
        check("claude text without schema", out, "plain words")
        j, out, err = run("claude", "claude_text", schema=SCHEMA, tries=1)
        check("claude no JSON with schema", err and err.kind, "parse")
        j, out, err = run("claude", "claude_list", schema=SCHEMA, tries=1)
        check("claude verbose list", json.loads(out)["verdict"], "pass")
        j, out, err = run("claude", "claude_bad_schema", schema=SCHEMA, tries=1)
        check("claude schema violation", err and err.kind, "parse")
        check("claude schema message", "verdict" in str(err), True)
        calls()
        j, out, err = run("claude", "claude_auth", tries=3)
        check("claude auth", err and err.kind, "auth")
        check("claude auth not retried", len(calls()), 1)
        j, out, err = run("claude", "claude_net", tries=2)
        check("claude network", err and err.kind, "network")
        check("claude network retried", len(calls()), 2)
        j, out, err = run("claude", "claude_max_turns", tries=1)
        check("claude error subtype", err is not None, True)
        calls()

        os.environ["JUDGE_CLAUDE_BIN"] = str(d / "missing")
        j, out, err = run("claude", "claude_ok", tries=3)
        check("missing CLI", err and err.kind, "request")

        os.environ["FAKE_MODE"] = "claude_ok"
        os.environ["JUDGE_CLAUDE_BIN"] = str(fake)
        obj = judge.ask_json("Q", SCHEMA, judge=judge.Judge("claude"), tries=1)
        check("module ask_json", obj["verdict"], "reject")
        calls()

    # --- parsers ------------------------------------------------------------------
    check("classify quota", judge.classify("HTTP 429 Too Many"), "quota")
    check("classify auth", judge.classify("PERMISSION_DENIED"), "auth")
    check("classify server", judge.classify("503 overloaded"), "server")
    check("classify network", judge.classify("read timed out"), "network")
    check("classify unknown", judge.classify("???"), "server")
    check("extract_json prose", judge.extract_json('a {"x": 1} b'), {"x": 1})
    check("extract_json dict", judge.extract_json({"y": 2}), {"y": 2})
    for bad in ("none", "[1, 2]", '{"x": }'):
        try:
            judge.extract_json(bad)
            failures.append(f"extract_json({bad!r}) did not raise")
        except ValueError:
            pass
    check(
        "schema ok", judge.schema_errors({"verdict": "pass", "reason": ""}, SCHEMA), []
    )
    check(
        "schema missing",
        judge.schema_errors({"verdict": "pass"}, SCHEMA),
        ["$.reason: missing"],
    )
    check(
        "schema enum",
        len(judge.schema_errors({"verdict": "x", "reason": ""}, SCHEMA)),
        1,
    )
    check(
        "schema bool is not int",
        judge.schema_errors(True, {"type": "integer"}),
        ["$: expected integer"],
    )
    check(
        "schema maxItems",
        judge.schema_errors([1, 2], {"type": "array", "maxItems": 1}),
        ["$: more than 1 items"],
    )
    st = "\n".join(
        json.dumps(e)
        for e in (
            {"event": "step_update", "step_update": {"usage": {"input_tokens": 3}}},
            {"event": "step_update", "step_update": {"usage": {"input_tokens": 4}}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "hi"}},
        )
    )
    status, resp, err, usage = judge.read_stream("junk\n" + st)
    check("read_stream", (status, resp, usage["input_tokens"]), ("SUCCESS", "hi", 7))
finally:
    os.environ.clear()
    os.environ.update(saved)

if failures:
    print("\n".join(failures))
    print(f"FAIL: {len(failures)} failure(s)")
    sys.exit(1)
print("OK: judge_test")
