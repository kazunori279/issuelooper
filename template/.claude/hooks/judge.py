"""The judge: one-shot JSON questions to a model, outside the agent sessions.

The review gate (review_gate.py) asks its question through here. The judge is an
agent CLI run once, with no workspace and no tools that change anything. One
setting picks it:

    JUDGE=agy[:<model>]      a fresh one-shot `agy -p` (any `agy models` name)
    JUDGE=claude[:<model>]   a fresh one-shot `claude -p`
    unset                    the reviewer role's CLI and model:
                             CLI   $CCROLE_REVIEWER_CLI, else $CCROLE_CLI, else agy
                             model $CCROLE_MODEL_REVIEWER, else $CCROLE_MODEL,
                                   else the CLI's default

Without a model the CLI's default model answers.

Both calls run in an empty temporary directory (no workspace, no hooks, no project
instructions). agy gets `--json-schema` when a schema is given; images are written
there as files and the prompt asks the model to open them with its file viewer.
Errors are JudgeError with a kind (quota, auth, server, network, parse, request) so
callers can log them and decide whether to retry; auth and request errors are not
retried. Token usage is kept in `last_usage` (the last call) and `usage` (totals).

    JUDGE_AGY_BIN      the agy executable (default agy)
    JUDGE_CLAUDE_BIN   the claude executable (default claude)
    JUDGE_AGY_MIN_S    the shortest timeout an agy call gets (default 300: agy
                       turns think longer than a plain completion)
"""

import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time

BACKENDS = ("agy", "claude")
AGY_MIN_S = int(os.environ.get("JUDGE_AGY_MIN_S", "300"))
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)
_KINDS = (
    ("quota", r"RESOURCE_EXHAUSTED|\b429\b|quota|rate.?limit|usage limit"),
    (
        "auth",
        r"UNAUTHENTICATED|PERMISSION_DENIED|\b40[13]\b|not (?:signed|logged) in|sign in"
        r"|log ?in required|/login|credential|invalid api key|api key",
    ),
    ("server", r"UNAVAILABLE|INTERNAL|DEADLINE_EXCEEDED|\b50[0234]\b|overloaded"),
    ("network", r"interrupted|timed? ?out|connection|network|EOF"),
)
# Claude Code tools the judge never needs. The call runs with all of them denied;
# with images, Read stays allowed so the model can open the image files.
CLAUDE_DENY = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
    "Glob",
    "Grep",
)


class JudgeError(RuntimeError):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


# --- replies ---------------------------------------------------------------------


def extract_json(text):
    """The first-to-last brace span of `text` as a dict (ValueError if none)."""
    if isinstance(text, dict):
        return text
    text = text or ""
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < i:
        raise ValueError("no braces")
    obj = json.loads(text[i : j + 1])
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def schema_errors(obj, schema, path="$"):
    """The ways `obj` breaks `schema` (type, enum, required, properties, items,
    maxItems): enough JSON Schema for the callers' schemas."""
    t = schema.get("type")
    if t:
        ok = isinstance(obj, _TYPES[t]) and not (
            t in ("integer", "number") and isinstance(obj, bool)
        )
        if not ok:
            return [f"{path}: expected {t}"]
    errs = []
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if t == "object":
        errs += [
            f"{path}.{k}: missing" for k in schema.get("required", []) if k not in obj
        ]
        for k, sub in schema.get("properties", {}).items():
            if k in obj:
                errs += schema_errors(obj[k], sub, f"{path}.{k}")
    if t == "array":
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
        for i, item in enumerate(obj):
            errs += schema_errors(item, schema.get("items", {}), f"{path}[{i}]")
    return errs


def classify(text):
    for kind, pat in _KINDS:
        if re.search(pat, text or "", re.I):
            return kind
    return "server"


def read_stream(text):
    """agy stream-json -> (status, response, error, usage). The response is the
    structured output when --json-schema produced one; usage is the result event's,
    else the sum of the agent_response steps."""
    status, resp, err, usage, steps = None, "", "", None, {}
    for line in (text or "").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("event") == "step_update":
            u = (e.get("step_update") or {}).get("usage") or {}
            for k in USAGE_KEYS:
                steps[k] = steps.get(k, 0) + int(u.get(k) or 0)
        elif e.get("event") == "result":
            r = e.get("result") or {}
            status, resp = r.get("status"), r.get("response") or ""
            err = str(r.get("error") or "")
            if r.get("structured_output"):
                resp = json.dumps(r["structured_output"])
            usage = r.get("usage") or None
    usage = {k: int((usage or steps).get(k) or 0) for k in USAGE_KEYS}
    return status, resp, err, usage


def read_claude(text):
    """`claude -p --output-format json` stdout -> (ok, result text, usage).

    The documented output is one result object: {"type": "result", "subtype":
    "success" | "error_...", "is_error": bool, "result": "<reply text>", "usage":
    {"input_tokens", "output_tokens", "cache_read_input_tokens", ...}}. With
    --verbose some versions print a list of messages instead; the last result
    message is taken. ValueError when there is no result object at all."""
    obj = json.loads(text or "")
    if isinstance(obj, list):
        obj = next(
            (
                m
                for m in reversed(obj)
                if isinstance(m, dict) and m.get("type") == "result"
            ),
            None,
        )
    if not isinstance(obj, dict) or ("result" not in obj and "is_error" not in obj):
        raise ValueError("no result object")
    u = obj.get("usage") or {}
    usage = {
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "thinking_tokens": 0,
        "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
    }
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    ok = not obj.get("is_error") and obj.get("subtype", "success") == "success"
    return ok, str(obj.get("result") or ""), usage


# --- the judge -------------------------------------------------------------------


def default_spec(model=None):
    """$JUDGE, else "<reviewer CLI>:<model>" from the role settings."""
    spec = os.environ.get("JUDGE", "").strip()
    if spec:
        return spec
    cli = (
        os.environ.get("CCROLE_REVIEWER_CLI", "").strip()
        or os.environ.get("CCROLE_CLI", "").strip()
        or "agy"
    )
    model = (
        model
        or os.environ.get("CCROLE_MODEL_REVIEWER", "").strip()
        or os.environ.get("CCROLE_MODEL", "").strip()
    )
    return f"{cli}:{model}" if model else cli


def parse_spec(spec):
    """ "agy", "agy:<model>", "claude", "claude:<model>" -> (backend, model or "")."""
    backend, _, model = (spec or "").strip().partition(":")
    backend, model = backend.strip(), model.strip()
    if backend not in BACKENDS:
        raise ValueError(f"JUDGE: {spec!r} (want agy[:<model>] or claude[:<model>])")
    return backend, model


class Judge:
    def __init__(self, spec=None, model=None, env=None, log_dir=None):
        self.spec = spec or default_spec(model)
        self.backend, self.model = parse_spec(self.spec)
        self.name = f"{self.backend}:{self.model}" if self.model else self.backend
        self.env, self.log_dir = env, pathlib.Path(log_dir) if log_dir else None
        self.agy = os.environ.get("JUDGE_AGY_BIN", "agy")
        self.claude = os.environ.get("JUDGE_CLAUDE_BIN", "claude")
        self.usage = {"calls": 0, **{k: 0 for k in USAGE_KEYS}}
        self.last_usage = {}
        self._lock = threading.Lock()

    def ask(self, text, max_tokens=1500, timeout=180, tries=3, images=(), schema=None):
        """One question; the reply text (JSON text for agy with a schema).
        `images` are (media type, bytes[, file name]) tuples. `max_tokens` is
        accepted for callers' sake; neither CLI takes an output limit."""
        call = self._agy if self.backend == "agy" else self._claude
        kind, err = "network", ""
        for attempt in range(tries):
            if attempt:
                time.sleep(5 + 10 * (attempt - 1))
            try:
                return call(text, timeout, images, schema)
            except JudgeError as e:
                kind, err = e.kind, str(e)
                if kind in ("auth", "request"):
                    break
        raise JudgeError(kind, err)

    def ask_json(
        self, text, max_tokens=1500, timeout=180, tries=3, images=(), schema=None
    ):
        """Like ask, and the JSON object in the reply (JudgeError "parse" if none).
        The callers validate the object against their schema themselves."""
        out = self.ask(text, max_tokens, timeout, tries, images, schema)
        try:
            return extract_json(out)
        except ValueError as e:
            raise JudgeError("parse", f"no JSON object in the reply: {e}") from e

    def agy_cmd(self, text, timeout, d, schema=None):
        cmd = [
            self.agy,
            "-p",
            text,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--print-timeout",
            f"{math.ceil(timeout / 60)}m",
        ]
        if self.model:
            cmd[3:3] = ["--model", self.model]
        if schema:
            (d / "schema.json").write_text(json.dumps(schema))
            cmd += ["--json-schema", str(d / "schema.json")]
        return cmd

    def claude_cmd(self, text, images=()):
        # Checked with Claude Code 2.1.286 on macOS (plain and verbose JSON).
        # --disallowedTools is the documented way to deny tools in print mode (the
        # newer `--tools ""` is not in every version). The list goes in as one
        # comma-separated value, and -p comes last so the variadic option cannot
        # swallow the prompt. --max-turns leaves room for Read calls with images.
        cmd = [self.claude, "--disallowedTools", ",".join(CLAUDE_DENY)]
        if images:
            cmd += ["--allowedTools", "Read"]
        cmd += [
            "--output-format",
            "json",
            "--max-turns",
            str(2 + len(images) if images else 1),
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + ["-p", text]

    def _run(self, name, cmd, d, timeout):
        try:
            return subprocess.run(
                cmd,
                cwd=d,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=self.env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise JudgeError("network", f"{name} did not finish in {timeout}s")
        except OSError as e:
            raise JudgeError("request", f"cannot run {cmd[0]}: {e}")

    def _count(self, usage):
        with self._lock:
            self.last_usage = usage
            self.usage["calls"] += 1
            for k in USAGE_KEYS:
                self.usage[k] += usage[k]

    def _agy(self, text, timeout, images, schema):
        timeout = max(timeout, AGY_MIN_S)
        d = pathlib.Path(tempfile.mkdtemp(prefix="judge-"))
        try:
            cmd = self.agy_cmd(
                text + image_note(d, images, "view_file"), timeout, d, schema
            )
            r = self._run("agy", cmd, d, timeout + 60)
            self._keep_log(r.stdout + r.stderr)
            status, resp, err, usage = read_stream(r.stdout)
            self._count(usage)
            if status == "SUCCESS" and resp.strip():
                return resp
            if status == "SUCCESS":
                raise JudgeError("parse", "agy returned an empty reply")
            msg = err or (r.stderr or r.stdout).strip()[-300:] or f"exit {r.returncode}"
            raise JudgeError(classify(msg), f"agy {status or 'no result'}: {msg[:300]}")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _claude(self, text, timeout, images, schema):
        """Claude Code has no structured-output flag in every version, so the
        schema is checked here on the JSON the reply text contains."""
        d = pathlib.Path(tempfile.mkdtemp(prefix="judge-"))
        try:
            cmd = self.claude_cmd(text + image_note(d, images, "Read"), images)
            r = self._run("claude", cmd, d, timeout + 60)
            self._keep_log(r.stdout + r.stderr)
            try:
                ok, resp, usage = read_claude(r.stdout)
            except ValueError:
                msg = (r.stderr or r.stdout).strip()[-300:] or f"exit {r.returncode}"
                raise JudgeError(
                    classify(msg) if r.returncode else "parse", f"claude: {msg[:300]}"
                )
            self._count(usage)
            if not ok:
                msg = resp or r.stderr.strip()[-300:] or f"exit {r.returncode}"
                raise JudgeError(classify(msg), f"claude error: {msg[:300]}")
            if not resp.strip():
                raise JudgeError("parse", "claude returned an empty reply")
            if schema:
                try:
                    obj = extract_json(resp)
                except ValueError as e:
                    raise JudgeError("parse", f"no JSON object in the reply: {e}")
                errs = schema_errors(obj, schema)
                if errs:
                    raise JudgeError("parse", "; ".join(errs[:3]))
                return json.dumps(obj)
            return resp
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _keep_log(self, out):
        if not self.log_dir:
            return
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            name = f"judge-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{threading.get_ident() % 10000}.jsonl"
            (self.log_dir / name).write_text(out)
        except OSError:
            pass


def image_note(d, images, viewer):
    """Write the images into the call's directory; the line telling the model to
    open them with `viewer` (empty with no images)."""
    names = []
    for k, img in enumerate(images, 1):
        ext = {"image/jpeg": ".jpg", "image/webp": ".webp"}.get(img[0], ".png")
        name = pathlib.Path(img[2]).name if len(img) > 2 else f"image-{k}{ext}"
        (d / name).write_bytes(img[1])
        names.append(name)
    if not names:
        return ""
    return (
        f"\n\nThe {len(names)} image(s) are the files {', '.join(names)} in the current "
        f"directory, in that order. Open each one with your file viewer ({viewer}) and "
        "look at it before you answer. Do not run terminal commands."
    )


def ask_json(prompt, schema, images=None, judge=None, **kw):
    """One question to the judge -> the reply object, checked against `schema`
    (JudgeError "parse" when it does not fit)."""
    obj = (judge or Judge()).ask_json(prompt, images=images or (), schema=schema, **kw)
    errs = schema_errors(obj, schema) if schema else []
    if errs:
        raise JudgeError("parse", "; ".join(errs[:3]))
    return obj
