#!/usr/bin/env python3
"""Cases for agent_cli.py and ccrole.sh's CLI and model choice. Run: `python3 agent_cli_test.py`.

The Claude Code cases are built from its documented stream-json format, not from
a recorded live run.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_cli  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


AGY, CLAUDE = agent_cli.get("agy"), agent_cli.get("claude")


def test_choice_precedence() -> None:
    check(
        "nothing set: agy, no model",
        (agent_cli.cli_for("worker", {}), agent_cli.model_for("worker", {})),
        ("agy", ""),
    )
    env = {"CCROLE_CLI": "claude", "CCROLE_MODEL": "m-all"}
    check(
        "CCROLE_CLI and CCROLE_MODEL apply to every role",
        (agent_cli.cli_for("monitor", env), agent_cli.model_for("tester", env)),
        ("claude", "m-all"),
    )
    env.update(CCROLE_WORKER_CLI="agy", CCROLE_MODEL_WORKER="m-w")
    check(
        "a per-role value wins over the global one",
        (
            agent_cli.cli_for("worker", env),
            agent_cli.model_for("worker", env),
            agent_cli.cli_for("merger", env),
        ),
        ("agy", "m-w", "claude"),
    )
    try:
        agent_cli.get("other")
        check("an unknown CLI is refused", False, True)
    except ValueError as exc:
        check(
            "an unknown CLI is refused, naming the choices",
            "agy, claude" in str(exc),
            True,
        )


def test_arguments() -> None:
    check(
        "agy first turn",
        AGY.first_turn("do it", 600),
        [
            "--print",
            "do it",
            "--print-timeout",
            "600s",
            "--output-format",
            "stream-json",
        ],
    )
    check(
        "agy resume continues the conversation",
        AGY.resume("go on", "c1", 90)[-2:],
        ["--conversation", "c1"],
    )
    check(
        "claude first turn: stream-json needs --verbose, no print timeout",
        CLAUDE.first_turn("do it", 600),
        ["-p", "do it", "--output-format", "stream-json", "--verbose"],
    )
    r = CLAUDE.resume("go on", "s1", 90)
    check("claude resume", (r[:2], r[r.index("--resume") + 1]), (["-p", "go on"], "s1"))
    check(
        "claude long-lived session reads stream-json on stdin",
        CLAUDE.stream_args(None, pathlib.Path("/x")),
        [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ],
    )
    check(
        "and resumes its session",
        CLAUDE.stream_args("s1", pathlib.Path("/x"))[-2:],
        ["--resume", "s1"],
    )
    check(
        "agy long-lived session",
        AGY.stream_args("c1", pathlib.Path("/x"))[-2:],
        ["--conversation", "c1"],
    )
    check(
        "user messages",
        (AGY.user_message("hi"), CLAUDE.user_message("hi")),
        (
            {"event": "user", "message": {"content": "hi"}},
            {"type": "user", "message": {"role": "user", "content": "hi"}},
        ),
    )


def agy_step(state, kind="tool"):
    return {
        "event": "step_update",
        "step_update": {
            "step_index": 1,
            "state": state,
            "step_type": kind,
            "tool_name": "run_command",
        },
    }


def test_agy_stream() -> None:
    init = {"event": "init", "conversation_id": "c1"}
    check("session id from init", AGY.session_id([init]), "c1")
    check(
        "or from the result",
        AGY.session_id([{"event": "result", "result": {"conversation_id": "c2"}}]),
        "c2",
    )
    check(
        "silent after a finished step",
        AGY.model_stalled([init, agy_step("DONE")]),
        True,
    )
    check("not while a tool runs", AGY.model_stalled([init, agy_step("ACTIVE")]), False)
    check(
        "silent in the middle of a reply",
        AGY.model_stalled([init, agy_step("ACTIVE", "agent_response")]),
        True,
    )
    check(
        "retrying a model call: the last step is an error_message",
        (
            AGY.in_model_retry([init, agy_step("DONE", "error_message")]),
            AGY.in_model_retry([init, agy_step("DONE")]),
            CLAUDE.in_model_retry([cl_init(), cl_text()]),
        ),
        (True, False, False),
    )
    check(
        "not after a result",
        AGY.model_stalled([init, {"event": "result", "result": {}}]),
        False,
    )
    q = {
        "event": "result",
        "result": {
            "status": "ERROR",
            "response": "",
            "error": "TPM quota exhausted. No spare capacity is available.",
        },
    }
    check("a quota result with no answer", AGY.quota_result(q), True)
    full = {
        "event": "result",
        "result": {
            "status": "ERROR",
            "error": "RESOURCE_EXHAUSTED (code 429)",
            "response": "Merged PR #9 and moved the issue on, as the skill says.",
        },
    }
    check("a full answer next to a stale 429 is not", AGY.quota_result(full), False)
    r = AGY.read(full)
    check(
        "read: the stale error is reported as a success",
        (r["kind"], r["status"], r["quota"]),
        ("result", "SUCCESS", False),
    )
    check(
        "read: a quota result",
        (AGY.read(q)["status"], AGY.read(q)["quota"]),
        ("ERROR", True),
    )
    step = AGY.read(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 4,
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {"parameters": {"CommandLine": "gh issue list"}},
                "duration_seconds": 1.23,
            },
        }
    )
    check(
        "read: a finished tool step",
        step["steps"],
        [
            {
                "i": 4,
                "type": "tool",
                "state": "DONE",
                "sec": 1.2,
                "tool": "run_command",
                "text": "gh issue list",
            }
        ],
    )
    check(
        "read: a running step records nothing",
        AGY.read(agy_step("ACTIVE"))["steps"],
        [],
    )


def cl_init(sid="s1"):
    return {"type": "system", "subtype": "init", "session_id": sid, "model": "m"}


def cl_tool(tid, name="Bash", cmd="ls"):
    return {
        "type": "assistant",
        "session_id": "s1",
        "message": {
            "content": [
                {"type": "tool_use", "id": tid, "name": name, "input": {"command": cmd}}
            ]
        },
    }


def cl_result(tid):
    return {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]
        },
    }


def cl_text(text="Looking."):
    return {
        "type": "assistant",
        "session_id": "s1",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def test_claude_stream() -> None:
    check(
        "session id: the newest one seen",
        CLAUDE.session_id([cl_init("s1"), cl_init("s2")]),
        "s2",
    )
    check(
        "silent after a tool result",
        CLAUDE.model_stalled([cl_init(), cl_tool("t1"), cl_result("t1")]),
        True,
    )
    check(
        "not while a tool runs", CLAUDE.model_stalled([cl_init(), cl_tool("t1")]), False
    )
    check(
        "not while one of two parallel tools runs",
        CLAUDE.model_stalled(
            [cl_init(), cl_tool("t1"), cl_tool("t2"), cl_result("t1")]
        ),
        False,
    )
    check(
        "silent after text with no tool pending",
        CLAUDE.model_stalled([cl_init(), cl_text()]),
        True,
    )
    check(
        "a tool the killed process left running does not count after a resume",
        CLAUDE.model_stalled([cl_init(), cl_tool("t1"), cl_init(), cl_text()]),
        True,
    )
    check(
        "not after the result",
        CLAUDE.model_stalled([cl_init(), {"type": "result", "result": "x"}]),
        False,
    )
    check("not with only the init", CLAUDE.model_stalled([cl_init()]), False)
    rate = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "session_id": "s1",
        "result": 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
    }
    check("a 429 error result is a quota result", CLAUDE.quota_result(rate), True)
    over = dict(rate, result="API Error: 529 overloaded_error")
    check("so is overloaded_error", CLAUDE.quota_result(over), True)
    limit = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Claude AI usage limit reached|1760000000",
    }
    check("so is the usage limit", CLAUDE.quota_result(limit), True)
    fine = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Fixed the retry on HTTP 429 and opened PR #12.",
    }
    check(
        "a finished answer that mentions 429 is not", CLAUDE.quota_result(fine), False
    )
    other = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "tool crashed",
    }
    check("another error is not", CLAUDE.quota_result(other), False)
    r = CLAUDE.read(fine)
    check(
        "read: a success",
        (r["status"], r["response"], r["session_id"]),
        ("SUCCESS", fine["result"], None),
    )
    r = CLAUDE.read(rate)
    check(
        "read: a quota error",
        (r["status"], r["response"], r["quota"]),
        ("ERROR", "", True),
    )
    steps = CLAUDE.read(cl_tool("t9", cmd="gh issue list"))["steps"]
    check(
        "read: a tool call is a step",
        [(s["type"], s["tool"], s["text"]) for s in steps],
        [("tool", "Bash", "gh issue list")],
    )
    check(
        "read: init", CLAUDE.read(cl_init("s5")), {"kind": "init", "session_id": "s5"}
    )


FAKE_CLI = r"""#!/bin/sh
echo "CC_ROLE=$CC_ROLE" > "$FAKE_OUT"
echo "GIT_EDITOR=$GIT_EDITOR GIT_TERMINAL_PROMPT=$GIT_TERMINAL_PROMPT GIT_MERGE_AUTOEDIT=$GIT_MERGE_AUTOEDIT GH_PAGER=$GH_PAGER" >> "$FAKE_OUT"
for a in "$@"; do echo "$a" >> "$FAKE_OUT"; done
"""


def run_ccrole(env_extra: dict, role="worker", cli="agy", args=("-p", "hello")):
    d = pathlib.Path(tempfile.mkdtemp(prefix="ccrole-test-"))
    # A copy away from the repository: ccrole.sh points the repo it sits in at .githooks.
    hooks = d / "a" / "b" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(HERE / "ccrole.sh", hooks / "ccrole.sh")
    bindir = d / "bin"
    bindir.mkdir()
    for name in ("agy", "claude"):
        (bindir / name).write_text(FAKE_CLI)
        (bindir / name).chmod(0o755)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CCROLE") and k != "CC_ROLE"
    }
    env.update(
        PATH=f"{bindir}{os.pathsep}{env['PATH']}",
        HOME=str(d),
        FAKE_OUT=str(d / "out"),
        **env_extra,
    )
    r = subprocess.run(
        ["bash", str(hooks / "ccrole.sh"), role, "--", *args],
        env=env,
        cwd=d,
        capture_output=True,
        text=True,
    )
    out = (d / "out").read_text().splitlines() if (d / "out").exists() else []
    return r, out


def test_ccrole_launch() -> None:
    r, out = run_ccrole({})
    check("ccrole: defaults to agy, says so", "worker session on agy" in r.stderr, True)
    check("ccrole: the role is exported", out[:1], ["CC_ROLE=worker"])
    check("ccrole: no model flag without a model", "--model" in out, False)
    check(
        "ccrole: skip-permissions and --add-dir are added",
        ("--dangerously-skip-permissions" in out, "--add-dir" in out),
        (True, True),
    )
    check("ccrole: the caller's arguments come last", out[-2:], ["-p", "hello"])
    r, out = run_ccrole(
        {"CCROLE_CLI": "claude", "CCROLE_WORKER_CLI": "agy", "CCROLE_MODEL": "m-all"}
    )
    check(
        "ccrole: the per-role CLI wins over CCROLE_CLI",
        "worker session on agy" in r.stderr,
        True,
    )
    check("ccrole: the global model applies", out[out.index("--model") + 1], "m-all")
    r, out = run_ccrole(
        {
            "CCROLE_CLI": "claude",
            "CCROLE_MODEL": "m-all",
            "CCROLE_MODEL_MONITOR": "m-mon",
        },
        role="monitor",
    )
    check(
        "ccrole: claude gets --model too, the per-role one",
        ("monitor session on claude" in r.stderr, out[out.index("--model") + 1]),
        (True, "m-mon"),
    )
    r, out = run_ccrole({"CCROLE_CLI": "other"})
    check(
        "ccrole: an unknown CLI is refused",
        (r.returncode, "unknown CLI" in r.stderr),
        (1, True),
    )


def test_ccrole_sessions_never_wait_for_a_terminal() -> None:
    """An editor or a credential prompt in a session never returns (19 of 60 mergers)."""
    def env_seen(extra):
        _, out = run_ccrole(extra)
        return next(ln for ln in out if ln.startswith("GIT_EDITOR="))

    for k in ("GIT_EDITOR", "GIT_TERMINAL_PROMPT", "GIT_MERGE_AUTOEDIT", "GH_PAGER"):
        os.environ.pop(k, None)
    check("ccrole: no editor, no prompt, no pager by default", env_seen({}),
          "GIT_EDITOR=true GIT_TERMINAL_PROMPT=0 GIT_MERGE_AUTOEDIT=no GH_PAGER=cat")
    check("ccrole: an exported value wins", env_seen({"GIT_EDITOR": "vi"}).split()[0], "GIT_EDITOR=vi")


def test_model_from_init() -> None:
    agy, claude = agent_cli.get("agy"), agent_cli.get("claude")
    check("agy: the init event's model",
          agy.model([{"event": "init", "init": {"model": "m-1"}}, {"event": "step_update"}]), "m-1")
    check("agy: the newest init after a resume",
          agy.model([{"event": "init", "init": {"model": "old"}}, {"event": "init", "init": {"model": "new"}}]), "new")
    check("claude: the system init's model",
          claude.model([{"type": "system", "subtype": "init", "model": "claude-x"}, {"type": "assistant"}]), "claude-x")
    check("no init: empty", agy.model([{"event": "step_update"}]), "")


def test_usage_and_last_tool() -> None:
    agy, claude = agent_cli.get("agy"), agent_cli.get("claude")
    ev = [
        {"event": "init", "conversation_id": "c"},
        {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE", "tool_name": "run_command",
                                                  "tool_info": {"parameters": {"CommandLine": "pytest -q"}}}},
        {"event": "result", "result": {"usage": {"input_tokens": 100, "output_tokens": 10, "thinking_tokens": 5,
                                                 "cache_read_tokens": 50}}},
        {"event": "result", "result": {"usage": {"input_tokens": 20, "output_tokens": 2}}},
    ]
    check("agy: tokens over every turn, no cost", agy.usage(ev),
          {"input": 120, "output": 12, "thinking": 5, "cache_read": 50, "turns": 2, "cost_usd": None})
    check("agy: the running tool", agy.last_tool(ev), "run_command pytest -q")
    cev = [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                       "input": {"command": "make verify"}}]}},
        {"type": "result", "subtype": "success", "result": "ok", "total_cost_usd": 0.25,
         "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "output_tokens": 3,
                   "cache_read_input_tokens": 40}},
        {"type": "result", "subtype": "success", "result": "ok", "total_cost_usd": 0.5, "usage": {}},
    ]
    check("claude: tokens and the CLI's own cost", claude.usage(cev),
          {"input": 15, "output": 3, "thinking": 0, "cache_read": 40, "turns": 2, "cost_usd": 0.75})
    check("claude: the newest tool call", claude.last_tool(cev), "Bash make verify")
    check("no tool yet", agy.last_tool([{"event": "init"}]), "")


def agy_conversation(root: pathlib.Path, conv: str, steps: list[tuple[int, int, int]], age: float = 0) -> None:
    """agy's own record of a conversation: (index, type, status) per step, `age` s old."""
    import sqlite3
    db = root / f"{conv}.db"
    c = sqlite3.connect(db)
    c.execute("create table steps (idx integer primary key, step_type integer, status integer, step_payload blob)")
    c.executemany("insert into steps (idx, step_type, status) values (?, ?, ?)", steps)
    c.commit()
    c.close()
    os.utime(db, (os.path.getmtime(db) - age,) * 2)


def test_agy_finished_unseen() -> None:
    # Nine mergers in one run: agy's stream stopped at a command it lost track of
    # (still ACTIVE), the agent went on and finished, and the process never exited.
    agy, claude = agent_cli.get("agy"), agent_cli.get("claude")
    root = pathlib.Path(tempfile.mkdtemp(prefix="agy-conv-"))
    old = os.environ.get("AGY_CONVERSATIONS_DIR")
    os.environ["AGY_CONVERSATIONS_DIR"] = str(root)
    try:
        stuck = [{"event": "init", "conversation_id": "c1"},
                 {"event": "step_update", "step_update": {"step_index": 33, "state": "ACTIVE", "step_type": "tool",
                                                          "tool_name": "run_command"}}]
        agy_conversation(root, "c1", [(32, 15, 3), (33, 132, 1), (60, 132, 3), (85, 15, 3)], age=600)
        check("agy: a reply 50 steps past the stuck one, ten minutes old: finished", agy.finished_unseen(stuck, 180), True)
        check("agy: not yet quiet long enough", agy.finished_unseen(stuck, 900), False)
        agy_conversation(root, "c2", [(33, 132, 1), (40, 132, 1)], age=600)
        running = [dict(stuck[0], conversation_id="c2"), stuck[1]]
        check("agy: the conversation's last step still runs: not finished", agy.finished_unseen(running, 180), False)
        agy_conversation(root, "c3", [(32, 15, 3), (33, 132, 1)], age=600)
        level = [dict(stuck[0], conversation_id="c3"), stuck[1]]
        check("agy: nothing past the stream's last step: a long command, not a lost one",
              agy.finished_unseen(level, 180), False)
        done = stuck[:1] + [{"event": "step_update", "step_update": {"step_index": 33, "state": "DONE",
                                                                     "step_type": "tool"}}]
        check("agy: a stream not stopped on a running step", agy.finished_unseen(done, 180), False)
        check("agy: no record of the conversation", agy.finished_unseen([dict(stuck[0], conversation_id="nope"),
                                                                        stuck[1]], 180), False)
        check("claude: its stream is all there is", claude.finished_unseen(stuck, 0), False)
    finally:
        if old is None:
            os.environ.pop("AGY_CONVERSATIONS_DIR", None)
        else:
            os.environ["AGY_CONVERSATIONS_DIR"] = old
        shutil.rmtree(root, ignore_errors=True)


def _pb(fields: list[tuple[int, object]]) -> bytes:
    """A protobuf message from (field, int or bytes) pairs."""
    def varint(v: int) -> bytes:
        out = b""
        while True:
            out += bytes([(v & 0x7F) | (0x80 if v > 0x7F else 0)])
            v >>= 7
            if not v:
                return out
    out = b""
    for f, v in fields:
        out += varint(f << 3 | (0 if isinstance(v, int) else 2))
        out += varint(v) if isinstance(v, int) else varint(len(v)) + v
    return out


def test_usage_without_a_result() -> None:
    # Issue #4: 42 sessions in run 8 (stuck streams, timeouts) logged zero tokens.
    import sqlite3
    agy, claude = agent_cli.get("agy"), agent_cli.get("claude")
    root = pathlib.Path(tempfile.mkdtemp(prefix="agy-usage-"))
    old = os.environ.get("AGY_CONVERSATIONS_DIR")
    os.environ["AGY_CONVERSATIONS_DIR"] = str(root)
    try:
        killed = [{"event": "init", "conversation_id": "u1"},
                  {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response",
                                                           "usage": {"input_tokens": 100, "output_tokens": 10,
                                                                     "thinking_tokens": 5, "cache_read_tokens": 50}}},
                  {"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool"}}]
        check("agy: no result and no record: the stream's finished steps",
              agy.usage(killed), {"input": 100, "output": 10, "thinking": 5, "cache_read": 50, "turns": 1,
                                  "cost_usd": None})
        c = sqlite3.connect(root / "u1.db")
        c.execute("create table gen_metadata (idx integer primary key, data blob, size integer)")
        for k, (i, o, cr, th) in enumerate(((100, 10, 50, 5), (300, 20, 150, 7))):
            usage = _pb([(1, 999), (2, i), (3, o), (5, cr), (9, th), (10, 1)])
            c.execute("insert into gen_metadata values (?, ?, 0)", (k, _pb([(1, _pb([(3, 999), (4, usage)]))])))
        c.commit()
        c.close()
        check("agy: no result: every model call in agy's own record of the conversation",
              agy.usage(killed), {"input": 400, "output": 30, "thinking": 12, "cache_read": 200, "turns": 1,
                                  "cost_usd": None})
        done = killed + [{"event": "result", "result": {"usage": {"input_tokens": 7, "output_tokens": 1}}}]
        check("agy: with its result, the result counts, as before", (agy.usage(done)["input"], agy.usage(done)["turns"]),
              (7, 1))
    finally:
        if old is None:
            os.environ.pop("AGY_CONVERSATIONS_DIR", None)
        else:
            os.environ["AGY_CONVERSATIONS_DIR"] = old
        shutil.rmtree(root, ignore_errors=True)
    msg = {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 40, "output_tokens": 4}}}
    cut = [{"type": "system", "subtype": "init", "session_id": "s"},
           {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 1}, "total_cost_usd": 0.1},
           msg, msg, {"type": "assistant", "message": {"id": "m2", "usage": {"input_tokens": 60, "output_tokens": 6}}}]
    check("claude: a turn killed before its result counts its messages, each once",
          claude.usage(cut), {"input": 110, "output": 11, "thinking": 0, "cache_read": 0, "turns": 2, "cost_usd": 0.1})


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all agent_cli cases pass ({len(tests)} groups, {CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
