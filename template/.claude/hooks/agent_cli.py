#!/usr/bin/env python3
"""What differs between the agent CLIs a session can run on: agy and Claude Code.

    python3 agent_cli.py cli ROLE        the CLI ROLE's sessions run on
    python3 agent_cli.py model ROLE      the model ROLE's sessions use ('' = the CLI's default)
    python3 agent_cli.py clis ROLE...    the distinct CLIs those roles need, one per line

The supervisor (ccpool.py) and the monitor host (broker.py) start sessions, read
their stream-json output and decide when to resume one. Everything that depends on
the CLI lives here: the arguments for a first turn and for a resume, the session id
in the stream, whether the model went silent, whether the CLI is retrying a failed
model call, and whether a result ended on a quota error with no answer.

Which CLI a role uses: CCROLE_<ROLE>_CLI, else CCROLE_CLI, else agy. The model:
CCROLE_MODEL_<ROLE>, else CCROLE_MODEL, else none (the CLI picks). ccrole.sh applies
the same order and adds --model, --add-dir and --dangerously-skip-permissions, so the
arguments built here leave those out.

agy's format is in AGY_PROTOCOL.md. Claude Code's stream-json format has been
checked against Claude Code 2.1.286 on macOS (see docs/macos-verification.md):

    {"type": "system", "subtype": "init", "session_id": ..., "model": ...}
    {"type": "assistant", "message": {"content": [{"type": "text", ...},
                                                  {"type": "tool_use", "id": ..., ...}]}}
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ...}]}}
    {"type": "result", "subtype": "success" | "error_max_turns" | "error_during_execution",
     "is_error": bool, "result": "<final text>", "session_id": ..., "usage": {...}}
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT = "agy"


def parse(lines) -> list[dict]:
    """The JSON objects among stream lines; anything else is skipped."""
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _proto(b: bytes) -> dict[int, list]:
    """One level of a protobuf message: field number -> its varints and byte strings."""
    out: dict[int, list] = {}
    i = 0

    def varint() -> int:
        nonlocal i
        v = shift = 0
        while True:
            x = b[i]
            i += 1
            v |= (x & 0x7F) << shift
            shift += 7
            if not x & 0x80:
                return v

    while i < len(b):
        key = varint()
        field, wire = key >> 3, key & 7
        if wire == 0:
            out.setdefault(field, []).append(varint())
        elif wire == 2:
            n = varint()
            out.setdefault(field, []).append(b[i:i + n])
            i += n
        elif wire in (1, 5):
            i += 8 if wire == 1 else 4
        else:
            raise ValueError(f"wire type {wire}")
    return out


class Cli:
    name = ""
    prompt_flag = ""
    quota: re.Pattern = re.compile(r"(?!x)x")
    # Text on stderr that means the CLI's own time limit ended the run ('' = it has none).
    timeout_text = ""
    # A last stderr line that means the agent's turn is over while the CLI waits for a
    # background task it started (None = the CLI does not do that).
    idle_wait: re.Pattern | None = None

    def first_turn(self, prompt: str, timeout_s: float) -> list[str]:
        raise NotImplementedError

    def resume(self, prompt: str, session_id: str, timeout_s: float) -> list[str]:
        raise NotImplementedError

    def is_init(self, e: dict) -> bool:
        raise NotImplementedError

    def is_result(self, e: dict) -> bool:
        raise NotImplementedError

    def current(self, events: list[dict]) -> list[dict]:
        """The events of the newest process: a resume appends a second stream to the
        same file, and a tool the killed process left running never gets its result."""
        for k in range(len(events) - 1, -1, -1):
            if self.is_init(events[k]):
                return events[k:]
        return events

    def session_id(self, events: list[dict]) -> str | None:
        raise NotImplementedError

    def model(self, events: list[dict]) -> str:
        """The model the stream's newest init event names, or ''."""
        for e in reversed(events):
            if self.is_init(e):
                return self.init_model(e)
        return ""

    def init_model(self, e: dict) -> str:
        return str(e.get("model") or "")

    def usage(self, events: list[dict]) -> dict:
        """Tokens over every turn of the stream (a resume adds turns), and the cost in
        USD when the CLI reports one (None when it does not)."""
        raise NotImplementedError

    def last_tool(self, events: list[dict]) -> str:
        """The newest tool call in the stream, as "<tool> <its main argument>", or ''."""
        for e in reversed(events):
            r = self.read(e)
            for step in reversed(r.get("steps") or []):
                if step.get("type") == "tool":
                    return f"{step.get('tool') or ''} {step.get('text') or ''}".strip()
        return ""

    def model_stalled(self, events: list[dict]) -> bool:
        """No tool is running and the stream is not finished: if nothing comes next for
        a while, the model went silent."""
        raise NotImplementedError

    def in_model_retry(self, events: list[dict]) -> bool:
        """The CLI is retrying a failed model call inside the turn, so the wait is the
        model's, not the session's. False for a CLI whose stream does not show it."""
        return False

    def quota_result(self, e: dict) -> bool:
        """A result that ended on a model quota or rate-limit error with no answer."""
        raise NotImplementedError

    def finished_unseen(self, events: list[dict], quiet: float) -> bool:
        """The stream stopped behind a step while the conversation went on and ended:
        its last step is a finished reply, unchanged for `quiet` s. False for a CLI
        whose stream is all there is."""
        return False

    # --- a long-lived session (the monitor, fed through stdin) ---
    def stream_args(self, session_id: str | None, logs: pathlib.Path) -> list[str]:
        raise NotImplementedError

    def user_message(self, text: str) -> dict:
        raise NotImplementedError

    def read(self, e: dict) -> dict:
        """One stream event, normalized for broker.py:
        {"kind": "init", "session_id"}, {"kind": "step", "steps": [...]},
        {"kind": "result", "status": SUCCESS|ERROR, "response", "error", "session_id", "quota"},
        or {"kind": "other"}. A step is {"i", "type", "state", "sec"} plus "tool" and
        "text" for a tool call and "role" for a subagent."""
        raise NotImplementedError


class Agy(Cli):
    name = "agy"
    prompt_flag = "--print"
    # A model quota or capacity error in any wording agy has used. One wording ("TPM
    # quota exhausted. No spare capacity ...") came with no RESOURCE_EXHAUSTED in the
    # result (that was only on stderr); without it those sessions were never resumed
    # and each counted as a failed claim attempt.
    quota = re.compile(
        r"RESOURCE_EXHAUSTED|TPM quota exhausted|No spare capacity|\bcode 429\b"
    )
    timeout_text = "print timeout"
    # When the model ends its turn while a command it ran is still going (a server, a
    # sweep moved to the background), agy prints this and waits for the task, up to its
    # print timeout; the model does not act again in that run.
    idle_wait = re.compile(r"root agent idle; waiting up to .* for \d+ background task")
    # agy keeps each conversation in a database of its own (AGY_CONVERSATIONS_DIR in
    # tests). Its stream prints steps in order, so one it loses track of (a command it
    # stopped reading after five silent minutes) holds back every later step, and the
    # process does not exit after the last one. Step codes as agy wrote them: a reply
    # is type 15; status 1 is running, 3 done.
    REPLY, DONE = 15, 3

    @staticmethod
    def conversations() -> pathlib.Path:
        return pathlib.Path(os.environ.get("AGY_CONVERSATIONS_DIR")
                            or "~/.gemini/antigravity-cli/conversations").expanduser()

    def first_turn(self, prompt, timeout_s):
        return [
            "--print",
            prompt,
            "--print-timeout",
            f"{int(timeout_s)}s",
            "--output-format",
            "stream-json",
        ]

    def resume(self, prompt, session_id, timeout_s):
        return self.first_turn(prompt, timeout_s) + ["--conversation", session_id]

    def is_init(self, e):
        return e.get("event") == "init"

    def init_model(self, e):
        init = e.get("init") if isinstance(e.get("init"), dict) else {}
        return str(init.get("model") or e.get("model") or "")

    def usage(self, events):
        # agy reports tokens in each result and no price. A turn that ends without one
        # (killed at a timeout, or a stream stuck behind a step) left 42 sessions in run 8
        # at zero: then the conversation's own record has every model call, and failing
        # that the stream's finished steps carry theirs (they add up to the result's).
        out = {"input": 0, "output": 0, "thinking": 0, "cache_read": 0, "turns": 0, "cost_usd": None}
        inits = sum(1 for e in events if self.is_init(e))
        results = [e["result"] for e in events if e.get("event") == "result" and isinstance(e.get("result"), dict)]
        if len(results) < inits:
            whole = self.recorded_usage(self.session_id(events))
            if whole is None:
                results = [s for e in events if e.get("event") == "step_update"
                           for s in [e.get("step_update") or {}] if s.get("state") == "DONE" and s.get("usage")]
            else:
                return {**out, **whole, "turns": inits}
        for r in results:
            u = r.get("usage") or {}
            out["input"] += int(u.get("input_tokens") or 0)
            out["output"] += int(u.get("output_tokens") or 0)
            out["thinking"] += int(u.get("thinking_tokens") or 0)
            out["cache_read"] += int(u.get("cache_read_tokens") or 0)
        out["turns"] = max(inits, sum(1 for e in events if e.get("event") == "result"))
        return out

    def recorded_usage(self, conv: str | None) -> dict | None:
        """Tokens over every model call of the conversation, from agy's own record: one
        gen_metadata row per call, its usage at field 1.4 (2 input, 3 output, 5 cache read,
        9 thinking: the numbers its stream reports). None when there is no record."""
        if not conv:
            return None
        db = self.conversations() / f"{conv}.db"
        if not db.exists():
            return None
        out = {"input": 0, "output": 0, "thinking": 0, "cache_read": 0}
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                rows = c.execute("select data from gen_metadata").fetchall()
            finally:
                c.close()
            for (data,) in rows:
                for call in _proto(data).get(1, []):
                    for u in _proto(call).get(4, []):
                        f = _proto(u)
                        for key, num in (("input", 2), ("output", 3), ("cache_read", 5), ("thinking", 9)):
                            out[key] += sum(v for v in f.get(num, []) if isinstance(v, int))
        except (sqlite3.Error, ValueError, IndexError, TypeError):
            return None
        return out

    def last_tool(self, events):
        # A running tool shows as an ACTIVE step, which read() leaves out.
        for e in reversed(events):
            s = e.get("step_update") if e.get("event") == "step_update" else None
            if isinstance(s, dict) and s.get("step_type") == "tool":
                c = self._condense(s)
                return f"{c.get('tool') or ''} {c.get('text') or ''}".strip()
        return ""

    def is_result(self, e):
        return e.get("event") == "result"

    def session_id(self, events):
        sid = None
        for e in events:
            if e.get("event") == "init" and e.get("conversation_id"):
                sid = e["conversation_id"]
            elif e.get("event") == "result" and isinstance(e.get("result"), dict):
                sid = (
                    e["result"].get("conversation_id") or sid
                )  # the result names it too
        return sid

    def model_stalled(self, events):
        # Any step but a running tool, including a reply that started and went quiet:
        # streams have hung inside an ACTIVE agent_response for half an hour.
        events = self.current(events)
        last = events[-1] if events else {}
        step = last.get("step_update") if last.get("event") == "step_update" else None
        return isinstance(step, dict) and not (step.get("state") == "ACTIVE" and step.get("step_type") == "tool")

    def in_model_retry(self, events):
        # agy marks each failed model call it retries with an error_message step.
        events = self.current(events)
        last = events[-1] if events else {}
        step = last.get("step_update") if last.get("event") == "step_update" else None
        return isinstance(step, dict) and step.get("step_type") == "error_message"

    def finished_unseen(self, events, quiet):
        events = self.current(events)
        last = events[-1] if events else {}
        step = last.get("step_update") if last.get("event") == "step_update" else None
        conv = self.session_id(events)
        if not (isinstance(step, dict) and step.get("state") == "ACTIVE" and conv):
            return False
        db = self.conversations() / f"{conv}.db"
        try:
            # A reader leaves an empty -wal touched; agy's writes are the ones in it.
            wal = db.with_name(db.name + "-wal")
            changed = db.stat().st_mtime
            if wal.exists() and wal.stat().st_size > 0:
                changed = max(changed, wal.stat().st_mtime)
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                row = c.execute("select idx, step_type, status from steps order by idx desc limit 1").fetchone()
            finally:
                c.close()
        except (ValueError, OSError, sqlite3.Error):
            return False
        return (bool(row) and row[0] > int(step.get("step_index") or 0) and (row[1], row[2]) == (self.REPLY, self.DONE)
                and time.time() - changed >= quiet)

    def quota_result(self, e):
        # A result that has a full answer next to a stale 429 from a retried call did finish.
        r = e.get("result") if e.get("event") == "result" else None
        if not isinstance(r, dict) or len((r.get("response") or "").strip()) > 20:
            return False
        return bool(self.quota.search(json.dumps(r)))

    def stream_args(self, session_id, logs):
        # Its own CLI log: the retry and backoff lines behind a slow turn are there.
        args = [
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--log-file",
            str(logs / "agy-cli.log"),
        ]
        return args + (["--conversation", session_id] if session_id else [])

    def user_message(self, text):
        return {"event": "user", "message": {"content": text}}

    def read(self, e):
        kind = e.get("event")
        if kind == "init":
            return {"kind": "init", "session_id": e.get("conversation_id")}
        if kind == "step_update":
            s = e.get("step_update") or {}
            if s.get("state") in ("DONE", "ERROR"):
                return {"kind": "step", "steps": [self._condense(s)]}
            return {"kind": "step", "steps": []}
        if kind == "result":
            r = dict(e.get("result") or {})
            quota = self.quota_result(e)
            # A stale 429 from a retried call next to a full answer is not a failed
            # turn: report it as a success, with the error kept.
            if (
                r.get("status") == "ERROR"
                and not quota
                and self.quota.search(json.dumps(r))
            ):
                r["stale_error"] = r.pop("error", None)
                r["status"] = "SUCCESS"
            return {
                "kind": "result",
                "status": r.get("status") or "SUCCESS",
                "response": r.get("response") or "",
                "error": r.get("error"),
                "stale_error": r.get("stale_error"),
                "session_id": r.get("conversation_id"),
                "quota": quota,
            }
        return {"kind": "other"}

    @staticmethod
    def _condense(s):
        out = {
            "i": s.get("step_index"),
            "type": s.get("step_type"),
            "state": s.get("state"),
            "sec": round(s.get("duration_seconds") or 0, 1),
        }
        if s.get("step_type") == "tool":
            params = (s.get("tool_info") or {}).get("parameters") or {}
            out["tool"] = s.get("tool_name")
            out["text"] = str(
                params.get("CommandLine")
                or params.get("Question")
                or params.get("TargetFile")
                or params.get("AbsolutePath")
                or ""
            )[:200]
        if s.get("step_type") == "subagent" or s.get("tool_name") == "invoke_subagent":
            subs = (s.get("subagent_info") or {}).get("subagents") or []
            if subs:
                out["role"] = ", ".join(x.get("role") or "" for x in subs)
        return out


class Claude(Cli):
    """Claude Code stream-json. It has no print timeout: supervisor timeouts apply."""

    name = "claude"
    prompt_flag = "-p"
    quota = re.compile(r"\b429\b|rate_limit_error|overloaded_error|usage limit", re.I)

    def first_turn(self, prompt, timeout_s):
        return ["-p", prompt, "--output-format", "stream-json", "--verbose"]

    def resume(self, prompt, session_id, timeout_s):
        return [
            "-p",
            prompt,
            "--resume",
            session_id,
            "--output-format",
            "stream-json",
            "--verbose",
        ]

    def is_init(self, e):
        return e.get("type") == "system" and e.get("subtype") == "init"

    def is_result(self, e):
        return e.get("type") == "result"

    def session_id(self, events):
        sid = None
        for e in events:
            sid = e.get("session_id") or sid
        return sid

    @staticmethod
    def _parts(e):
        content = (e.get("message") or {}).get("content")
        return (
            [p for p in content if isinstance(p, dict)]
            if isinstance(content, list)
            else []
        )

    def pending_tool(self, events) -> bool:
        """A tool_use with no tool_result after it."""
        asked, answered = [], set()
        for e in events:
            for p in self._parts(e):
                if e.get("type") == "assistant" and p.get("type") == "tool_use":
                    asked.append(p.get("id"))
                elif e.get("type") == "user" and p.get("type") == "tool_result":
                    answered.add(p.get("tool_use_id"))
        return any(x not in answered for x in asked)

    def model_stalled(self, events):
        events = self.current(events)
        if not events or events[-1].get("type") not in ("assistant", "user"):
            return False
        return not self.pending_tool(events)

    def quota_result(self, e):
        if e.get("type") != "result":
            return False
        text = str(e.get("result") or "")
        if e.get("is_error") or (e.get("subtype") or "success") != "success":
            return bool(
                self.quota.search(text + " " + json.dumps(e.get("error") or ""))
            )
        # The plan's usage limit can arrive as a short, non-error result.
        return text.strip().lower().startswith("claude ai usage limit reached")

    def stream_args(self, session_id, logs):
        args = [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        return args + (["--resume", session_id] if session_id else [])

    def user_message(self, text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    def usage(self, events):
        # Claude Code puts its own cost estimate in each result (total_cost_usd). A turn
        # killed before its result counts its assistant messages' usage, each message once
        # (a message's events repeat it).
        out = {"input": 0, "output": 0, "thinking": 0, "cache_read": 0, "turns": 0, "cost_usd": None}

        def add(u: dict) -> None:
            out["input"] += int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
            out["output"] += int(u.get("output_tokens") or 0)
            out["cache_read"] += int(u.get("cache_read_input_tokens") or 0)

        pending: dict[str, dict] = {}  # message id -> usage, in the turn since the last result
        for e in events:
            if e.get("type") == "assistant":
                m = e.get("message") or {}
                if m.get("usage"):
                    pending[str(m.get("id") or len(pending))] = m["usage"]
            if e.get("type") != "result":
                continue
            pending.clear()
            out["turns"] += 1
            add(e.get("usage") or {})
            if e.get("total_cost_usd") is not None:
                out["cost_usd"] = round((out["cost_usd"] or 0) + float(e["total_cost_usd"]), 6)
        if pending:
            out["turns"] += 1
            for u in pending.values():
                add(u)
        return out

    def read(self, e):
        kind = e.get("type")
        if self.is_init(e):
            return {"kind": "init", "session_id": e.get("session_id")}
        if kind in ("assistant", "user"):
            steps = []
            for p in self._parts(e):
                if p.get("type") == "tool_use":
                    params = p.get("input") or {}
                    step = {
                        "i": p.get("id"),
                        "type": "tool",
                        "state": "DONE",
                        "sec": 0,
                        "tool": p.get("name"),
                        "text": str(
                            params.get("command")
                            or params.get("file_path")
                            or params.get("pattern")
                            or params.get("description")
                            or ""
                        )[:200],
                    }
                    if p.get("name") == "Task":
                        step["role"] = str(
                            params.get("subagent_type")
                            or params.get("description")
                            or ""
                        )
                    steps.append(step)
                elif p.get("type") == "text" and kind == "assistant":
                    steps.append(
                        {"i": None, "type": "agent_response", "state": "DONE", "sec": 0}
                    )
            return {"kind": "step", "steps": steps}
        if kind == "result":
            quota = self.quota_result(e)
            ok = (
                not quota
                and not e.get("is_error")
                and (e.get("subtype") or "success") == "success"
            )
            text = str(e.get("result") or "")
            return {
                "kind": "result",
                "status": "SUCCESS" if ok else "ERROR",
                "response": text if ok else "",
                "error": None if ok else (text or e.get("subtype")),
                "session_id": e.get("session_id"),
                "quota": quota,
            }
        return {"kind": "other"}


CLIS = {"agy": Agy(), "claude": Claude()}


def get(name: str) -> Cli:
    try:
        return CLIS[name]
    except KeyError:
        raise ValueError(
            f"unknown agent CLI {name!r}: use one of {', '.join(CLIS)}"
        ) from None


def cli_for(role: str, env=None) -> str:
    env = os.environ if env is None else env
    return (
        env.get(f"CCROLE_{role.upper()}_CLI") or env.get("CCROLE_CLI") or DEFAULT
    ).strip()


def model_for(role: str, env=None) -> str:
    env = os.environ if env is None else env
    return (
        env.get(f"CCROLE_MODEL_{role.upper()}") or env.get("CCROLE_MODEL") or ""
    ).strip()


def for_role(role: str, env=None) -> Cli:
    return get(cli_for(role, env))


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(HERE))
    import config

    config.apply()
    if len(argv) >= 3 and argv[1] == "cli":
        print(cli_for(argv[2]))
        return 0
    if len(argv) >= 3 and argv[1] == "model":
        print(model_for(argv[2]))
        return 0
    if len(argv) >= 3 and argv[1] == "clis":
        for name in dict.fromkeys(cli_for(r) for r in argv[2:]):
            print(name)
        return 0
    print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
