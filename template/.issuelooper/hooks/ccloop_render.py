#!/usr/bin/env python3
"""Render an agent session's stream-json into lines a human can watch.

A session run with `--print` (agy) or `-p` (Claude Code) prints nothing until it
is over, so from the outside a session doing work and a session wedged on a
prompt look the same. `--output-format stream-json` carries the intermediate
steps; this turns them into one timestamped line each.

Reads NDJSON on stdin, writes text on stdout. ccpool.py tees the raw stream to
`session.jsonl` beside the rendered `session.log`, so nothing here has to be
lossless: it is a console view, and the audit trail is the file.

TWO SCHEMAS, BECAUSE THERE ARE TWO CLIs

A role runs on agy or on Claude Code (ccrole.sh, CCROLE_<ROLE>_CLI), so both wire
formats have to render. They share no field names:

    agy       {"event": "step_update", "step_update": {"step_type": ..., ...}}
    claude    {"type": "assistant", "message": {"content": [...]}}

The agy shape below was read off a live stream rather than from a document:

    init                          {model, cwd, permission_mode, tools}
    step_update/user_input        state
    step_update/agent_response    text_delta (one word or so per event),
                                  duration_seconds and usage on DONE
    step_update/tool              tool_name, tool_info{name, parameters},
                                  tool_info.output and duration_seconds on DONE,
                                  state ACTIVE | DONE | ERROR
    result                        status, response (the whole final message)

The one that shapes the code: **agy's assistant text arrives one `text_delta` at
a time**, dozens of events per sentence. Printing each as its own timestamped
line buries the tool calls, which are the part that answers "what is it doing".
So deltas accumulate per step and print when the step reaches DONE.

THE ONE RULE: NEVER SWALLOW A LINE

A renderer that drops what it does not recognize is worse than no renderer,
because the session keeps running and the owner is now watching a filtered feed
without knowing it. Anything unparseable is echoed verbatim, anything parseable
but unknown is printed as a compact dump. If either schema changes, the output
gets uglier and stays complete.
"""

import json
import sys

MAX_LEN = 200  # a Bash command is the useful part; a file body is not


def clip(text, limit=MAX_LEN):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def stamp():
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")


def emit(mark, text):
    print(f"  {stamp()} {mark} {clip(text)}", flush=True)


def block(text):
    """The final summary, indented and unclipped: it is the session's answer."""
    for line in str(text).splitlines():
        if line.strip():
            print(f"     {line}", flush=True)


def say(text):
    """What the model said. The only thing here that is never clipped.

    Everything else on the console is evidence of an action and 200 characters
    of it is plenty. This is the session's own account of what it did, and a
    summary cut at 200 characters loses the end, which is where a session says
    what it could not finish."""
    if len(text) <= MAX_LEN and "\n" not in text:
        emit("·", text)
    else:
        print(f"  {stamp()} ·", flush=True)
        block(text)


def args_of(params):
    """The useful part of a tool's arguments, in one short string.

    The command is what the owner is trying to see. Everything else is
    summarized, because a Write's content is the whole file. agy capitalizes its
    parameter names and Claude Code does not, so both spellings are tried.
    """
    if not isinstance(params, dict):
        return clip(params)
    for key in ("CommandLine", "command", "Query", "query", "Pattern", "pattern",
                "AbsolutePath", "file_path", "path", "SearchDirectory", "prompt"):
        if params.get(key):
            return clip(params[key])
    return clip(", ".join(f"{k}={clip(v, 40)}" for k, v in params.items()))


class Render:
    """Holds the two pieces of state a stream needs: pending text, and whether
    any was printed. The second decides if `result` repeats the final message:
    when the deltas already showed it, printing it again doubles the longest
    paragraph of the run; when they did not, it is the only summary there is."""

    def __init__(self):
        self.text = {}  # step_index -> the deltas seen so far
        self.spoke = False

    # ---- agy ------------------------------------------------------------

    def step(self, body):
        kind = body.get("step_type")
        state = body.get("state")
        secs = body.get("duration_seconds")
        took = f"{secs:.1f}s " if isinstance(secs, (int, float)) else ""

        if kind == "agent_response":
            index = body.get("step_index")
            delta = body.get("text_delta")
            if delta:
                self.text[index] = self.text.get(index, "") + delta
            if state in ("DONE", "ERROR"):
                said = self.text.pop(index, "").strip()
                if said:
                    self.spoke = True
                    say(said)
                elif state == "ERROR":
                    emit("!", f"{took}agent_response ended in ERROR")
            return

        if kind == "tool":
            name = body.get("tool_name") or "?"
            info = body.get("tool_info") or {}
            if state == "ACTIVE":
                emit("→", f"{name}: {args_of(info.get('parameters'))}")
            elif state == "ERROR":
                emit("!", f"{took}{name}: {clip(info.get('output', '(no output)'))}")
            else:
                emit("←", f"{took}{clip(info.get('output', '(no output)'))}")
            return

        if kind == "user_input":
            return  # the prompt is in prompt.txt; echoing it is noise

        emit("?", json.dumps(body, ensure_ascii=False))

    # ---- Claude Code ----------------------------------------------------

    def content(self, part):
        kind = part.get("type")
        if kind == "text":
            said = part.get("text", "").strip()
            if said:
                self.spoke = True
                say(said)
        elif kind == "thinking":
            said = part.get("thinking", "").strip()
            if said:
                emit("~", said)
        elif kind == "tool_use":
            emit("→", f"{part.get('name', '?')}: {args_of(part.get('input'))}")
        elif kind == "tool_result":
            body = part.get("content")
            if isinstance(body, list):
                body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
            emit("!" if part.get("is_error") else "←", body or "(no output)")
        else:
            emit("?", json.dumps(part, ensure_ascii=False))

    # ---- dispatch -------------------------------------------------------

    def event(self, event):
        # agy keys the payload by the event name; Claude Code uses `type`.
        name = event.get("event")

        if name == "step_update":
            self.step(event.get("step_update") or {})
            return

        if name == "init":
            body = event.get("init") or {}
            emit("#", "init " + " ".join(
                f"{k}={body[k]}" for k in ("model", "cwd", "permission_mode")
                if body.get(k)))
            return

        if name == "result":
            body = event.get("result") or {}
            emit("=", body.get("status", "result"))
            response = (body.get("response") or "").strip()
            if response and not self.spoke:
                block(response)
            return

        if name is not None:
            emit("?", json.dumps(event, ensure_ascii=False))
            return

        kind = event.get("type")

        if kind in ("assistant", "user"):
            content = (event.get("message") or {}).get("content")
            if isinstance(content, str):
                emit("·", content)
            elif isinstance(content, list):
                for part in content:
                    self.content(part) if isinstance(part, dict) else emit("·", part)
            else:
                emit("?", json.dumps(event, ensure_ascii=False))

        elif kind == "system":
            bits = {k: v for k, v in event.items() if k not in ("type", "tools")}
            emit("#", f"{event.get('subtype', 'system')} "
                      f"{json.dumps(bits, ensure_ascii=False)}")

        elif kind == "result":
            # Cost and duration are the two numbers worth having on the console,
            # because they are what a wasted session costs.
            cost, ms = event.get("total_cost_usd"), event.get("duration_ms")
            parts = [event.get("subtype", "result")]
            if ms is not None:
                parts.append(f"{ms / 1000:.0f}s")
            if cost is not None:
                parts.append(f"${cost:.4f}")
            emit("=", " ".join(str(p) for p in parts))
            block(event.get("result") or "")

        else:
            emit("?", json.dumps(event, ensure_ascii=False))


def main():
    render = Render()
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            # Not JSON: the flag was not honoured, or this is a stray banner.
            # Print it as it came rather than dropping it.
            print(line, flush=True)
            continue
        try:
            render.event(event if isinstance(event, dict) else {"raw": event})
        except Exception as exc:  # noqa: BLE001 - a render bug must not blind the loop
            print(f"  {stamp()} ? render failed ({exc}): {clip(line, 400)}", flush=True)


if __name__ == "__main__":
    main()
