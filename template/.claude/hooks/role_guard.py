#!/usr/bin/env python3
"""PreToolUse guard: no GitHub write without a role, and no role writing outside it.

The issue loop runs sessions in five roles: worker (one issue to a pull request),
merger (lands a pull request that conflicts with main), tester (live checks and the
UX run), monitor (triage and the owner's decisions) and aide (an ad-hoc session
that files and comments). All of them authenticate to GitHub as the same login, so
which one acted is carried by a comment marker and by which labels each may touch.
This hook enforces both: a session with no role cannot write to GitHub at all, and
a session with a role cannot post unsigned or touch another role's labels.

check_pr_merge holds the merge gates (0-5). ccpool.py runs this guard with
CC_ROLE=merger for its own merges of mergeable pull requests, so a merge by the
supervisor and a merge by a merger session pass the same gates.

It is not a security boundary: a session could export CC_ROLE itself. It defends
against a session that never considered the question.

It runs before every shell command in every session, so it stays cheap: settings
come from the environment and `.issuelooper/config.toml` (config.py), and the only
network calls are the `gh` reads on a close or a merge.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
# No __pycache__ beside the hooks: in the shared checkout an untracked file there
# would make Gate 0 refuse every merge.
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
import config  # noqa: E402

config.apply()

# The marker is the role name.
ROLES = {
    "monitor": "<!-- monitor -->",
    "merger": "<!-- merger -->",
    "tester": "<!-- tester -->",
    "aide": "<!-- aide -->",
    "worker": "<!-- worker -->",
}

# Who may apply (add) and clear (remove) each label. The worker moves stage labels
# only on the issue it claimed (worker_label_check). ccpool.py's own label edits
# (claims, auto-merges, stale claims) go through claim.py and gh directly, not
# through this hook.
LABEL_RULES = {
    "stage:ready": {"add": {"worker"}, "remove": {"merger", "monitor", "worker"}},
    "stage:pr": {"add": {"worker"}, "remove": {"merger", "monitor"}},
    "stage:triage": {
        "add": {"merger", "monitor", "tester", "aide", "worker"},
        "remove": {"monitor", "aide", "worker"},
    },
    "stage:doing": {"add": {"worker"}, "remove": {"worker", "monitor"}},
    "on hold": {"add": set(), "remove": set()},
    "hold:monitor": {"add": {"monitor"}, "remove": {"monitor"}},
    # `needs:live`: acceptance needs the live environment, not the sweep. Taking it
    # off is how Gate 4 gets bypassed, so only the monitor (and the owner) clear it.
    "needs:live": {
        "add": {"monitor", "tester", "aide", "worker"},
        "remove": {"monitor", "aide"},
    },
    # `stage:live`: a `needs:live` issue after its merge (the PR said `Refs #N`),
    # waiting for the tester's live check.
    "stage:live": {"add": {"merger", "monitor"}, "remove": {"monitor"}},
    "P0": {"add": {"monitor", "tester", "aide"}, "remove": {"monitor"}},
    "P1": {"add": {"monitor", "tester", "aide"}, "remove": {"monitor"}},
    "P2": {"add": {"monitor", "tester", "aide"}, "remove": {"monitor"}},
}

# The tester and the aide set a priority when they file and never move one
# afterwards (add, no remove): priority is exactly-one-of, so raising and lowering
# are the same remove. They may not merge or close.

# Advancing to stage:pr must clear prior stage labels so issues do not carry
# multiple stage labels. The triage -> pr shortcut leaves stage:triage attached
# if the command only removes stage:ready.
STAGE_PR_REQUIRED_CLEARS = {"stage:triage", "stage:ready", "stage:owner"}

# The worker works one claimed issue per session; ccpool.py writes the claim
# (claim.py's JSON) to $WRK_CLAIM_FILE before the session starts.
# Every stage label it moves must be on that issue, and its `stage:pr` must also
# clear its own `stage:doing`.
CLAIM_FILE_ENV = "WRK_CLAIM_FILE"
ISSUE_EDIT_SEGMENT = re.compile(r"\bgh\s+issue\s+edit\b([^|;&]*)")
REST_LABELS_TARGET = re.compile(r"/issues/(\d+)/labels\b")
# A push whose refspec lands on main, in any spelling: `main`, `HEAD:main`,
# `refs/heads/main`, `+main`. Pushing a branch named `issue-N-main` is not one.
# A command ends at a newline too: a push followed on the next line by `gh pr create
# --base main` is not a push to main.
GIT_PUSH = re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+push\b([^|;&\n]*)")
PUSH_TO_MAIN = re.compile(r"(?:^|\s)\+?(?:[^\s:]*:)?(?:refs/heads/)?main(?=\s|$)")
# The worker commits in its worktree; .claude/, .agents/ and .issuelooper/ are the
# owner's (the pre-commit hook refuses them too).
GIT_FRAMEWORK_PATHS = re.compile(
    r"\bgit\b[^|;&\n]*\b(?:add|commit|rm|mv|checkout|restore)\b[^|;&\n]*(?:^|[\s/])\.(?:claude|agents|issuelooper)(?:/|\s|$)"
)
NO_VERIFY = re.compile(r"\bgit\b[^|;&]*\b(?:commit|push)\b[^|;&]*--no-verify\b")

ROLE_DIR = pathlib.Path.home() / ".claude" / "cc-role"
HEARTBEAT_FILE = pathlib.Path(
    os.environ.get("ROLE_GUARD_HEARTBEAT_FILE", "/tmp/issuelooper-guard.last")
)
RUNS_DIR = pathlib.Path(
    os.environ.get("ROLE_GUARD_RUNS_DIR", "/tmp/issuelooper-guard-runs")
)

# A write is anything that changes state. Reads — view, list, plain api, search —
# fall through untouched, because the loop reads constantly and a guard that slows
# that down would be turned off within the hour.
WRITE_PATTERNS = [
    r"\bgh\s+issue\s+(create|comment|edit|close|reopen|delete|transfer|pin|unpin|lock|unlock)\b",
    r"\bgh\s+pr\s+(create|comment|edit|close|merge|ready|reopen|review)\b",
    r"\bgh\s+label\s+(create|edit|delete|clone)\b",
    r"\bgh\s+api\b[^|;&]*?(-X|--method)\s+(POST|PATCH|PUT|DELETE)",
    # `gh api` with a field or input flag is a POST even with no -X, which is
    # how issue dependencies are recorded: `-F issue_id=<id>`.
    r"\bgh\s+api\b[^|;&]*?(-F|-f|--field|--raw-field|--input)[=\s]",
]
BODY_PATTERNS = [
    r"\bgh\s+issue\s+(create|comment)\b",
    r"\bgh\s+pr\s+(create|comment)\b",
    r"\bgh\s+issue\s+edit\b[^|;&]*--body",
    # `close --comment` and `reopen --comment` post a comment while looking like a
    # state change, so they need the marker too. `close` and `reopen` on their own
    # carry no text and are left alone.
    r"\bgh\s+issue\s+(close|reopen)\b[^|;&]*--comment",
    r"\bgh\s+pr\s+(close|reopen)\b[^|;&]*--comment",
]
LABEL_FLAG = re.compile(
    r"(--add-label|--remove-label|--label|(?<![\w-])-l)[=\s]+"
    r"""("([^"]*)"|'([^']*)'|([^\s"']+))"""
)
# `--body-file` only. This feeds `cli_comment_status`, which reads `gh issue` and
# `gh pr` subcommands; none of them takes `-F`. Widening it to `-F` would make
# `gh issue create … -F foo=bar` try to open a file called `foo=bar`, fail, and be
# refused as an unreadable body. The REST `-F body=@…` route is read by
# `rest_comment_body`, which tokenizes properly and never comes through here.
BODY_FILE = re.compile(r"""--body-file[=\s]+("([^"]*)"|'([^']*)'|([^\s"']+))""")
PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")
DRAFT_FLAG = re.compile(r"(?:--draft\b|(?<![\w-])-d\b)")
PR_BODY_FILE = re.compile(
    r"""(?:--body-file|(?<![\w-])-F)[=\s]+("([^"]*)"|'([^']*)'|([^\s"']+))"""
)

# The REST route to the same labels, which LABEL_FLAG structurally cannot see.
#
# `gh api -X POST .../issues/N/labels -f 'labels[]=on hold'` applies an owner-only
# label with no `--label` flag anywhere on the command line. This is not an exotic
# path: it is the documented way to *delete* a label whose name contains a colon,
# because `gh issue edit --remove-label stage:ready` can 404 unless the colon is
# percent-encoded, so sessions find it on their own.
#
# Residual gap, left open on purpose: a `DELETE` on `/issues/N/labels` with no
# label name clears *every* label, and nothing in the command says which those
# were. Refusing it outright would be the honest rule, but nobody has run it, and
# a guard that refuses a command it has never seen is a guess.
REST_LABEL_PATH = re.compile(r"/issues/\d+/labels/([^\s\"'&|;]+)")
REST_LABEL_FIELD = re.compile(
    r"""(?:-f|-F|--field|--raw-field)[=\s]+"""
    r"""(?:"labels(?:\[\])?=([^"]*)"|'labels(?:\[\])?=([^']*)'|labels(?:\[\])?=([^\s"']+))"""
)
REST_METHOD = re.compile(r"(?:-X|--method)[=\s]+(\w+)")

# The REST route to comments, which BODY_PATTERNS structurally cannot see:
# `gh api repos/.../issues/N/comments -f body=...` posts a comment with no
# `gh issue comment` on the line. All sessions authenticate as the same GitHub
# login, so the marker is the only audit trail of who acted.
#
# Comment writes whose body the guard cannot read (--input -, -F body=@-, stdin
# pipes, heredocs) are refused. Letting unreadable bodies through as a fail-open
# gap was tried, and the gap was hit several times a day, on the very comments
# that matter most (post-merge verification), so hiding the payload is treated as
# a refusal rather than a pass. The caller writes the body to
# /tmp/<role>-<N>-body.md and passes --body-file.
REST_COMMENT_PATH = re.compile(r"/issues/\d+/comments\b")
REST_BODY_FIELD = re.compile(
    r"""(?:-f|-F|--field|--raw-field)[=\s]+"""
    r"""(?:"body=([^"]*)"|'body=([^']*)'|body=(@"([^"]*)"|@'([^']*)'|@[^\s"';&|]+|"([^"]*)"|'([^']*)'|[^\s"';&|]+))"""
)
REST_INPUT_FLAG = re.compile(r"""--input[=\s]+(?:"([^"]*)"|'([^']*)'|([^\s"';&|]+))""")
CLI_BODY_FLAG = re.compile(
    r"""(?:--body|-b)[=\s]+(?:"([^"]*)"|'([^']*)'|([^\s"';&|]+))"""
)
CLI_COMMENT_FLAG = re.compile(
    r"""--comment[=\s]+(?:"([^"]*)"|'([^']*)'|([^\s"';&|]+))"""
)

# Only the supervisor (ccpool.py, with CC_ROLE=merger) merges. Both spellings,
# because `gh pr merge` is a thin wrapper over the second.
MERGE = re.compile(r"\bgh\s+pr\s+merge\b|/pulls/\d+/merge\b")

# Closing is a judgement about the backlog, so the tester does not get it. The REST
# form is `PATCH /issues/N` with `state=closed`, which is why the field is matched
# as well as the subcommand.
CLOSES = re.compile(
    r"\bgh\s+issue\s+close\b"
    r"|\bstate=closed\b"  # -f / --field / --raw-field state=closed
    r"|--state[=\s]+closed\b"
    r"|[\"']state[\"']\s*:\s*[\"']closed[\"']"
)

# The hold label: the monitor contesting a patch, as a label rather than prose.
#
# The merger and the supervisor pick work off labels and `gh pr list`. A hold
# written in a comment ("hold this patch, it rebuilds what the issue was opened to
# remove") is on neither, so the patch it names can be opened, merged and deployed
# within minutes with nobody doing anything wrong. **The loop's start signals are
# labels and its reasoning is comment bodies, so an objection expressed as
# reasoning is structurally invisible to the thing it objects to.** Whether a
# given hold is right does not enter into it: a wrong one would be merged through
# just as fast, which is the tell that this is a protocol defect rather than a
# judgement one.
#
# Deliberately a second label rather than a share of `on hold`. That one is the
# owner parking an issue; this one is the monitor contesting a patch. They lift
# on different evidence and by different hands, and collapsing them into one
# string would make "the owner said wait" and "a session disagreed"
# indistinguishable in the list view.
HOLD = "hold:monitor"

# The two labels behind the live-verification gate, and the one line that ends it.
#
# A pull request that says `Closes #N` closes its issue the moment it merges, so
# nearly every issue leaves the queue within seconds of its merge. Every rule
# about verifying afterwards is then a rule about work nobody is holding, and it
# is skipped at the rate you would expect. `needs:live` marks an issue whose
# acceptance can only be checked in the live environment; its pull request
# writes `Refs #N` instead, the merge leaves the issue open at `stage:live`, and
# the close is the last thing to happen rather than the first.
LIVE = "needs:live"
LIVE_STAGE = "stage:live"

# What closes a `needs:live` issue: a comment carrying a line that names what was
# run where the behavior was actually observed.
LIVE_EVIDENCE = re.compile(r"(?im)^[ \t>*-]*live-verified:[ \t]*\S")
LIVE_LINE = "live-verified: <what you ran, and where> <YYYY-MM-DD>"

# Which issue or pull request a close or a merge is aimed at, so the hold can be
# looked up. Both `gh issue close` and `gh pr merge` take the number anywhere in
# their flags and both default to the current branch when it is absent, so the
# segment is scanned for the first bare integer and an absent one is not an error.
CLOSE_SEGMENT = re.compile(r"\bgh\s+issue\s+close\b([^|;&]*)")
MERGE_SEGMENT = re.compile(r"\bgh\s+pr\s+merge\b([^|;&]*)")
BARE_NUMBER = re.compile(r"(?<![\w.=/-])#?(\d+)(?![\w.-])")
REST_ISSUE_TARGET = re.compile(r"/issues/(\d+)(?:\b|$)")
REST_PR_TARGET = re.compile(r"/pulls/(\d+)/merge\b")
REPO_FLAG = re.compile(r"""--repo[=\s]+("([^"]*)"|'([^']*)'|([^\s"']+))""")


# The one non-`gh` rule here, and it binds every role equally.
#
# Some endpoints answer with a secret in the clear because a browser needs it (a
# client-side API key in a config response, say). A health check that looks
# entirely innocent then prints a live secret into the session transcript, and
# into whatever conversation store the agent CLI keeps on disk. Sessions do this
# even after reading a skill paragraph that names the endpoint, so it is a
# refusal rather than a warning. $GUARD_SECRET_URLS lists such URL paths,
# comma-separated; empty turns the rule off.
def secret_urls() -> list[str]:
    raw = os.environ.get("GUARD_SECRET_URLS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


# Requires a fetcher, so `grep -rn "/api/token" src/` still works. Reading the source
# is not calling the endpoint, and a rule that caught the grep would be turned off.
# `http`/`https` are deliberately absent: they match inside the URL itself, so a grep
# for the full `http://…/api/token` would be refused for containing its own scheme.
FETCHERS = re.compile(r"\b(curl|wget|httpie|xh|httpx|urlopen|requests\.get)\b")
# A python filter selects what it prints, so it is the shape the refusal recommends.
SAFE_FILTER = re.compile(r"\|\s*[^|]*\bpython3?\b[^|]*\s-c\b")
DISCARDS_BODY = re.compile(r"-o\s+/dev/null")


# Gate 6. Some files are the owner's: the spec, reference data, a fixture a demo
# replays. Workers edited such a fixture to fit their own tests, which changed what
# the owner's demo showed. $GUARD_PROTECTED_PATHS lists them, comma-separated: a
# path ending in `/` covers everything under it. Empty turns the gate off.
def protected_paths() -> list[str]:
    raw = os.environ.get("GUARD_PROTECTED_PATHS", "")
    return [p.strip().removeprefix("./") for p in raw.split(",") if p.strip()]


def is_protected(path: str, protected: list[str]) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p if p.endswith("/") else p + "/")
               for p in protected)


def secret_url_in(command: str) -> str | None:
    """The first configured secret URL path the command names, or None."""
    for path in secret_urls():
        if re.search(re.escape(path) + r"(?![\w-])", command):
            return path
    return None


# The word for "refuse this" differs by host: Claude Code wants `block`, agy
# wants `deny`. Set from whichever payload shape arrived — see
# read_call. Defaults to Claude Code's, which is what an unrecognized payload
# almost certainly is.
DENY = "block"

# Keys unique to each host's payload, matched against the *raw* stdin text
# rather than a parsed object on purpose: the fail-open in __main__ has to know
# which host to answer even when the payload is the reason it is running.
# Reading the host out of the parse meant a truncated payload left DENY at Claude
# Code's default, the agy branch printed no `decision`, and agy reads that as a
# refusal.
#
# The quoted-key form matters. A bare `toolCall` also appears inside the command
# being run, every time anyone greps this file — and JSON escapes the quotes
# around a string *inside* a string, so `\"toolCall\":` in a command does not
# match while `"toolCall":` in the envelope does.
CC_PAYLOAD = re.compile(r'"(?:tool_input|session_id)"\s*:')
AGY_PAYLOAD = re.compile(r'"(?:conversationId|toolCall)"\s*:')


def sniff_deny(raw: str) -> str:
    """Which word this host uses for a refusal, without parsing.

    Claude Code wins a tie and wins an unrecognized payload, because its shape
    fails to *no opinion* — the command still goes through the normal permission
    prompt — while agy's `{"decision": "allow"}` skips the prompt outright. When
    the guard cannot tell who is asking, the answer that still asks the owner is
    the right one.
    """
    if CC_PAYLOAD.search(raw):
        return "block"
    return "deny" if AGY_PAYLOAD.search(raw) else "block"


def block(reason: str) -> None:
    print(json.dumps({"decision": DENY, "reason": reason}))
    sys.exit(0)


def allow() -> None:
    if DENY == "deny":
        print(json.dumps({"decision": "allow"}))
    else:
        print("{}")
    sys.exit(0)


def resolve_role(session_id: str) -> str:
    """Env var first, then a file keyed by session id.

    The file exists so a session already running when this landed can be given a
    role without losing its context — relaunching the merger mid-fix to satisfy a
    guard would cost more than the guard saves.
    """
    role = os.environ.get("CC_ROLE", "").strip().lower()
    if role:
        return role
    if session_id:
        f = ROLE_DIR / session_id
        try:
            return f.read_text().strip().lower()
        except OSError:
            pass
    return ""


def record_heartbeat(session_id: str, role: str, command: str) -> None:
    """Record proof that the guard executed, making silent non-execution observable."""
    import datetime

    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        snippet = (command or "").strip().split("\n")[0][:80]
        entry = f"{now}\tpid={os.getpid()}\tsession={session_id or '-'}\trole={role or '-'}\tcmd={snippet}\n"
        with HEARTBEAT_FILE.open("a", encoding="utf-8") as f:
            f.write(entry)
        if session_id and session_id != "-":
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            sess_file = RUNS_DIR / session_id
            sess_file.write_text(f"{now}\t{role}\t{snippet}\n", encoding="utf-8")
    except OSError:
        pass


def check_alive(session_id: str | None = None, max_age_seconds: int = 900) -> int:
    """Verify that role_guard has executed recently, making silent failure observable."""
    import time

    now = time.time()

    if session_id:
        sess_file = RUNS_DIR / session_id
        if not sess_file.is_file():
            print(
                f"role_guard heartbeat not found for session {session_id} in {RUNS_DIR}.",
                file=sys.stderr,
            )
            return 1
        try:
            mtime = sess_file.stat().st_mtime
            age = now - mtime
            if age > max_age_seconds:
                print(
                    f"role_guard heartbeat for session {session_id} is stale ({age:.0f}s old > {max_age_seconds}s limit).",
                    file=sys.stderr,
                )
                return 1
            print(
                f"role_guard is active for session {session_id} (heartbeat {age:.1f}s ago)."
            )
            return 0
        except OSError as exc:
            print(
                f"role_guard heartbeat check failed for session {session_id}: {exc}",
                file=sys.stderr,
            )
            return 1

    # If session_id is omitted: check RUNS_DIR for any active session
    if not RUNS_DIR.is_dir():
        print(
            f"role_guard runs directory not found ({RUNS_DIR}). The guard has not run.",
            file=sys.stderr,
        )
        return 1

    try:
        session_files = [
            p for p in RUNS_DIR.iterdir() if p.is_file() and not p.name.startswith(".")
        ]
    except OSError as exc:
        print(
            f"role_guard failed to scan runs directory ({RUNS_DIR}): {exc}",
            file=sys.stderr,
        )
        return 1

    if not session_files:
        print(
            f"role_guard runs directory is empty ({RUNS_DIR}). No active sessions recorded.",
            file=sys.stderr,
        )
        return 1

    active_sessions: list[tuple[str, float]] = []
    newest_mtime = -1.0

    for p in session_files:
        try:
            mtime = p.stat().st_mtime
            if mtime > newest_mtime:
                newest_mtime = mtime
            age = now - mtime
            if age <= max_age_seconds:
                active_sessions.append((p.name, age))
        except OSError:
            continue

    if not active_sessions:
        newest_age = now - newest_mtime if newest_mtime > 0 else 0
        print(
            f"role_guard heartbeat is stale across all {len(session_files)} sessions "
            f"(most recent was {newest_age:.0f}s old > {max_age_seconds}s limit).",
            file=sys.stderr,
        )
        return 1

    newest_session, newest_age = min(active_sessions, key=lambda item: item[1])
    print(
        f"role_guard is active ({len(active_sessions)} active session(s); "
        f"most recent {newest_session} heartbeat {newest_age:.1f}s ago)."
    )
    return 0


def labels_in(command: str) -> tuple[set[str], set[str]]:
    added: set[str] = set()
    removed: set[str] = set()
    for m in LABEL_FLAG.finditer(command):
        flag = m.group(1)
        value = next(g for g in (m.group(3), m.group(4), m.group(5)) if g is not None)
        target = removed if flag == "--remove-label" else added
        target.update(part.strip() for part in value.split(",") if part.strip())

    rest_added, rest_removed = rest_labels(command)
    return added | rest_added, removed | rest_removed


def rest_labels(command: str) -> tuple[set[str], set[str]]:
    """Labels moved through `gh api` rather than through `gh issue edit`.

    The method decides the direction: `DELETE` names the label in the path and
    removes it, anything else carries it in a `labels[]` field and adds it. `gh
    api` defaults to POST once a field flag is present, which is why the fallback
    is POST rather than GET.
    """
    added: set[str] = set()
    removed: set[str] = set()
    if "/labels" not in command:
        return added, removed

    m = REST_METHOD.search(command)
    method = (m.group(1) if m else "POST").upper()

    if method == "DELETE":
        for mm in REST_LABEL_PATH.finditer(command):
            removed.add(urllib.parse.unquote(mm.group(1)).strip())
        return added, removed

    for mm in REST_LABEL_FIELD.finditer(command):
        value = next(g for g in mm.groups() if g is not None)
        added.update(part.strip() for part in value.split(",") if part.strip())
    return added, removed


def rest_comment_body(command: str) -> tuple[bool, str | None]:
    """Inspect a REST comment command for its body.

    Returns (is_comment_post, body).
    - If is_comment_post is False: not a POST to /issues/<N>/comments.
    - If is_comment_post is True and body is None: unreadable body (stdin, `--input -`,
      `-F body=@-`, unreadable file, or omitted body) -> refused by caller.
    - If is_comment_post is True and body is a string: readable body to check.
    """
    if "api" not in command or not REST_COMMENT_PATH.search(command):
        return False, None

    m = REST_METHOD.search(command)
    method = (m.group(1) if m else "POST").upper()
    if method != "POST":
        return False, None

    raw_body: str | None = None
    input_file: str | None = None
    try:
        tokens = shlex.split(command)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("-f", "-F", "--field", "--raw-field") and i + 1 < len(tokens):
                i += 1
                arg = tokens[i]
                if arg.startswith("body="):
                    raw_body = arg[5:]
            elif tok.startswith(("--field=", "--raw-field=", "-f=", "-F=")):
                arg = tok.split("=", 1)[1]
                if arg.startswith("body="):
                    raw_body = arg[5:]
            elif tok == "--input" and i + 1 < len(tokens):
                i += 1
                input_file = tokens[i]
            elif tok.startswith("--input="):
                input_file = tok.split("=", 1)[1]
            i += 1
    except ValueError:
        pass

    if raw_body is None and input_file is None:
        mf = REST_BODY_FIELD.search(command)
        if mf:
            raw_body = next(g for g in mf.groups() if g is not None)
            if raw_body.startswith('@"') and raw_body.endswith('"'):
                raw_body = "@" + raw_body[2:-1]
            elif raw_body.startswith("@'") and raw_body.endswith("'"):
                raw_body = "@" + raw_body[2:-1]
            elif raw_body.startswith('"') and raw_body.endswith('"'):
                raw_body = raw_body[1:-1]
            elif raw_body.startswith("'") and raw_body.endswith("'"):
                raw_body = raw_body[1:-1]
        mi = REST_INPUT_FLAG.search(command)
        if mi:
            input_file = next(g for g in mi.groups() if g is not None)

    if raw_body is not None:
        if raw_body.startswith("@"):
            file_path = raw_body[1:]
            if file_path == "-":
                return True, None
            try:
                return True, pathlib.Path(file_path).read_text(errors="replace")
            except OSError:
                return True, None
        return True, raw_body

    if input_file is not None:
        if input_file == "-":
            return True, None
        try:
            content = pathlib.Path(input_file).read_text(errors="replace")
        except OSError:
            return True, None
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "body" in data:
                return True, str(data["body"])
        except Exception:
            pass
        return True, content

    return True, None


def cli_comment_status(command: str, marker: str) -> tuple[bool, bool]:
    """Check a CLI command matching BODY_PATTERNS for readable body and signature.

    Returns (has_readable_body, is_signed).
    - has_readable_body: False if stdin, pipe, heredoc, missing body, or unreadable file.
    - is_signed: True if the readable body contains the role marker.

    There is deliberately no test for `<<` here, and that is the whole subtlety.
    A heredoc feeding `gh` its body — `gh issue comment 1 <<EOF` — carries no body
    flag at all, so it falls through to the last line and is refused without one.
    A `<<` test would also refuse the *dominant* shape in this repo, which writes
    the heredoc to a file and then names that file:

        cat > /tmp/mon-470-body.md <<'MD' … MD
        gh issue comment 470 --body-file /tmp/mon-470-body.md

    That is one Bash call, it contains `<<`, and its body is on disk and perfectly
    readable. Refusing it would break every session in the loop for a case the
    fallthrough already handles.
    """
    m_file = (
        PR_BODY_FILE.search(command)
        if PR_CREATE.search(command)
        else BODY_FILE.search(command)
    )
    if m_file:
        path = next(
            g
            for g in (m_file.group(2), m_file.group(3), m_file.group(4))
            if g is not None
        )
        if path == "-":
            return False, False
        try:
            head = pathlib.Path(path).read_text(errors="replace")[:400]
            return True, marker in head
        except OSError:
            return False, False

    m_body = CLI_BODY_FLAG.search(command)
    if m_body:
        return True, marker in command

    m_comment = CLI_COMMENT_FLAG.search(command)
    if m_comment:
        return True, marker in command

    return False, False


def read_call(payload: dict) -> tuple[str, str]:
    """The shell command and the session id, from either host's PreToolUse payload.

    Sessions run on agy or Claude Code, so the guard reads two shapes:

        Claude Code   {"tool_input": {"command": ...}, "session_id": ...}
        agy           {"toolCall": {"name": "run_command",
                                    "args": {"CommandLine": ...}},
                       "conversationId": ...}

    Nothing else about the guard differs — the same commands are refused for the
    same reasons — so the two are normalized here and the rest of the file never
    learns which CLI it is running under.

    The `args` key is read case-insensitively and under several spellings on
    purpose. agy's own docs show `CommandLine`, but everything else in its
    hook payload is camelCase protojson, and a key this guard fails to find reads
    as "no command", which is a silent allow. Failing open on a *bug* is the right
    call (see __main__); failing open on a spelling is not.

    **It does not decide which host is asking, and used to.** It returned a
    refusal word beside the command, and `main` assigned that over the one
    `sniff_deny` had already read off the raw bytes — which undid that fix on the
    one path where the host is known and only the envelope is not. The two
    unknowns are different: an envelope with no branch here says nothing about who
    sent it. `sniff_deny` is the only thing that answers that question now.
    """
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and tool_input.get("command"):
        return str(tool_input["command"]), str(payload.get("session_id", ""))

    call = payload.get("toolCall")
    if isinstance(call, dict):
        args = call.get("args")
        if isinstance(args, dict):
            wanted = {"commandline", "command", "cmd"}
            for key, value in args.items():
                if key.lower() in wanted and isinstance(value, str) and value:
                    return value, str(payload.get("conversationId", ""))
        return "", str(payload.get("conversationId", ""))

    return "", str(payload.get("session_id", ""))


# `pkill -f <pattern>` and `killall` match command lines, and an agent session's own
# command line contains its prompt, which names the very server it was asked to
# stop. Sessions that ran `pkill -f "<server> --port <n>"` killed themselves.
KILL_BY_PATTERN = re.compile(r"\b(?:pkill|killall)\b")


def kill_by_pattern_check(command: str) -> None:
    if KILL_BY_PATTERN.search(command):
        block(
            "`pkill` and `killall` match command lines, and this session's own command line "
            "contains its prompt, so a pattern that names your server can match, and kill, "
            "this session too.\n\n"
            "Stop a server by its port or its PID instead: `lsof -tiTCP:<port> -sTCP:LISTEN | xargs kill`, or start it "
            "with `... & echo $! > /tmp/<name>.pid` and later `kill $(cat /tmp/<name>.pid)`."
        )


def secret_check(command: str) -> None:
    """Refuse an unfiltered fetch of a $GUARD_SECRET_URLS path. Binds every role."""
    if not FETCHERS.search(command):
        return
    path = secret_url_in(command)
    if path is None:
        return
    if DISCARDS_BODY.search(command):
        return  # a status-code probe never renders the body
    if SAFE_FILTER.search(command):
        return
    block(
        f"This fetches `{path}`, which this project lists in GUARD_SECRET_URLS: its "
        "response holds a secret in the clear. Unfiltered, the secret lands in your "
        "transcript and in the agent CLI's conversation store on disk. A `jq` or `sed` "
        "redaction is refused too: when it fails or misses a field, the secret is "
        "printed anyway.\n\n"
        "If you only need to know the endpoint answers, discard the body: "
        f"`curl -s -o /dev/null -w '%{{http_code}}' <host>{path}`. If you need a field it "
        "alone has, filter on the host with python and print only that field, or the "
        f'field *names*: `curl -s <host>{path} | python3 -c "import json,sys; '
        'd=json.load(sys.stdin); print(sorted(d))"`. Reading the source that serves it '
        "is always fine."
    )


def refuse_unreadable_body(role: str) -> None:
    block(
        f"Role is `{role}`, and comment writes whose body cannot be read "
        "(--input -, -F body=@-, stdin pipes or heredocs) are refused.\n\n"
        "Every session authenticates as the same GitHub login, so the "
        "marker is the only thing in the timeline that says which of you acted. "
        "Write the body to /tmp/<role>-<N>-body.md and pass --body-file."
    )


def gh_json(args: list[str], repo: str) -> object | None:
    """Run a read-only `gh` query and parse it, or return None meaning *unknown*.

    None is fail-open and every failure path returns it: `gh` missing, not signed
    in, offline, a number that does not exist, a timeout, malformed output. The
    guard's whole design is that it never bricks the loop, and a hold check that
    refused whenever GitHub was slow would be turned off within the day.

    This is the only place the guard touches the network, and it runs only on a
    close or a merge that every other rule has already allowed — so the cost is
    one query on the rarest commands in the loop, not on every Bash call.
    """
    if repo:
        args = args + ["--repo", repo]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=8)
    except Exception:  # noqa: BLE001
        return None
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def issue_labels(number: str, repo: str) -> set[str] | None:
    """Label names on one issue, or None when GitHub could not be asked."""
    data = gh_json(["gh", "issue", "view", number, "--json", "labels"], repo)
    if not isinstance(data, dict):
        return None
    return {
        str(lab.get("name"))
        for lab in (data.get("labels") or [])
        if isinstance(lab, dict) and lab.get("name")
    }


def held_issues(command: str) -> list[str]:
    """Issue numbers this command would close or merge that carry the hold.

    A merge is resolved through the pull request's linked issues
    (`closingIssuesReferences`, which is what GitHub populates from `Closes #N`),
    because the hold lives on the issue and the argument is a PR.
    """
    repo_match = REPO_FLAG.search(command)
    repo = ""
    if repo_match:
        repo = next(g for g in repo_match.groups()[1:] if g is not None)

    issues: list[str] = []
    prs: list[str] = []

    if CLOSES.search(command):
        segment = CLOSE_SEGMENT.search(command)
        if segment:
            found = BARE_NUMBER.search(segment.group(1))
            if found:
                issues.append(found.group(1))
        issues += REST_ISSUE_TARGET.findall(command)

    if MERGE.search(command):
        segment = MERGE_SEGMENT.search(command)
        if segment:
            found = BARE_NUMBER.search(segment.group(1))
            prs.append(found.group(1) if found else "")
        prs += REST_PR_TARGET.findall(command)

    for pr in prs[:3]:
        view = ["gh", "pr", "view"]
        if pr:
            view.append(pr)
        data = gh_json(view + ["--json", "closingIssuesReferences"], repo)
        if not isinstance(data, dict):
            continue
        for ref in (data.get("closingIssuesReferences") or [])[:5]:
            if isinstance(ref, dict) and ref.get("number"):
                issues.append(str(ref["number"]))

    held: list[str] = []
    for number in dict.fromkeys(issues):
        names = issue_labels(number, repo)
        if names and HOLD in names:
            held.append(number)
    return held


def refuse_held(role: str, numbers: list[str]) -> None:
    which = ", ".join(f"#{n}" for n in numbers)
    block(
        f"{which} carries `{HOLD}`, so it does not close and its pull request does "
        f"not merge — including for the {role}.\n\n"
        "The monitor puts that label on when it is contesting the patch itself "
        "rather than the diagnosis, and the thread says what it is contesting and "
        "what would lift it. Read the most recent `<!-- monitor -->` comment "
        f"on {which} before doing anything else.\n\n"
        "The hold is not a veto and it is not the owner's `on hold`. If you think "
        "it is wrong, say so on the thread — the monitor clears its own label, and "
        "a hold nobody argues with is a hold the monitor should be lifting. What is "
        "not available is landing the change while the objection is unanswered."
    )


def check_issue_close(command: str) -> None:
    """Refuse closing a `needs:live` issue until the thread says where it was seen.

    The other end of gate 4. Keeping the issue open past the merge only buys
    anything if closing it later costs something, and the something is one line
    naming what was run in the live environment. It is a line somebody types, so
    it is a weak gate; what it is not is a close that happens as a side effect of
    a merge nobody read.
    """
    repo_match = REPO_FLAG.search(command)
    repo = ""
    if repo_match:
        repo = next(g for g in repo_match.groups()[1:] if g is not None)

    numbers: list[str] = []
    segment = CLOSE_SEGMENT.search(command)
    if segment:
        found = BARE_NUMBER.search(segment.group(1))
        if found:
            numbers.append(found.group(1))
    numbers += REST_ISSUE_TARGET.findall(command)

    for number in list(dict.fromkeys(numbers))[:3]:
        names = issue_labels(number, repo)
        if names is None or LIVE not in names:
            continue
        data = gh_json(["gh", "issue", "view", number, "--json", "comments"], repo)
        if not isinstance(data, dict):
            continue  # fail open: an unreachable thread is not evidence either way
        bodies = [
            str(c.get("body") or "")
            for c in (data.get("comments") or [])
            if isinstance(c, dict)
        ]
        if any(LIVE_EVIDENCE.search(b) for b in bodies):
            continue
        block(
            f"#{number} carries `{LIVE}` and no comment on it says the behavior was "
            "observed live, so it does not close yet.\n\n"
            "Check it in the live environment and post what you saw, with a line "
            f"reading\n\n```\n{LIVE_LINE}\n```\n\n"
            "as its own line. The tester is the role that runs live checks; issues "
            f"sitting at `{LIVE_STAGE}` are its queue.\n\n"
            "If the behavior cannot be reached live either, say that on the thread "
            "and take the label off rather than closing around it — an issue closed "
            "with nobody having looked is the state this label exists to prevent."
        )


def refuse_unsigned_comment(role: str, marker: str) -> None:
    block(
        f"Role is `{role}`, so this comment must be signed `{marker}` on its first "
        "line.\n\nEvery session authenticates as the same GitHub login, so the "
        "marker is the only thing in the timeline that says which of you acted. Add "
        "it to the top of the body (or of the --body-file) and run this again."
    )


CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})", re.MULTILINE)
SWEEP_SHA_PATTERN = re.compile(r"(?i)\bSweep\s+SHA\b[*`:\s]*([0-9a-fA-F]{7,40})")
SWEEP_LINE_PATTERN = re.compile(
    r"(?:^|[\r\n])[`*#\s-]*(?:(\d+)\s*(?:->|→)\s*)?(\d+)\s+green:\s*([^\r\n`]+)"
)
SWEEP_COUNT_PATTERN = re.compile(
    r"(?i)\b(\d+)\s*/\s*(\d+)\s*(?:scripts|check scripts|suites|green)\b"
)
SWEEP_WAIVER = re.compile(
    r"(?i)\bSweep\b[*`:\s]*waived\s*[-–—:]\s*"
    r"(loop/process|docs\s+only|owner\s+ruling)\b"
)

# The sweep runner, vendored with the framework. A PR head that has it can be
# swept, so its body must carry the sweep's `green:` line or a sweep waiver.
SWEEP_RUNNER = ".claude/hooks/sweep_run.py"
SWEEP_RECORD_DIR_DEFAULT = "/tmp/issuelooper-sweep"
SWEEP_HOW = "`python3 .claude/hooks/sweep_locked.py` in your worktree"


def load_sweep_record(sha: str) -> dict | None:
    """The provenance record sweep_run.py writes for a full, green sweep started
    from a clean tree at `sha`. None when there is none, or when it cannot be
    read, which for the gate means the same thing."""
    record_dir = pathlib.Path(
        os.environ.get("SWEEP_RECORD_DIR") or SWEEP_RECORD_DIR_DEFAULT
    )
    try:
        record = json.loads((record_dir / f"{sha.lower()}.json").read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


# Gate 3. `Fail-before` written only as a rule in a skill stays prose: across a run
# of merged PRs, `Sweep SHA` (which this hook refused a merge over) was present in
# nearly all of them and `Fail-before` in a minority. Same authors, same loop. The
# difference was whether something refused.
#
# The required form, in the PR body, one line:
#
#     - **Fail-before:** tests/test_parser.py 7 -> 8
#
# Two passing-test counts from the owning pytest file under tests/: at the base,
# then at the branch. The second must exceed the first, which is what makes it a
# statement that a case went red before the fix and green after.
FAIL_BEFORE_PATTERN = re.compile(
    r"(?i)\bFail[\s-]*before\b[*`:\s]*`?([\w./-]+\.py)`?"
    r"[*`:\s]*(\d+)\s*(?:->|→|to)\s*(\d+)"
)

# The escape hatch, and it is a real hole: a waiver is a line somebody types.
# It is here because the alternative is worse — a gate with no waiver wedges the
# loop on the issues that genuinely cannot carry a case, and a wedged gate gets
# switched off, which is how a hook becomes prose again. The reason has to be one
# of four, so the waiver is greppable and the monitor can audit its use.
FAIL_BEFORE_WAIVER = re.compile(
    r"(?i)\bFail[\s-]*before\b[*`:\s]*waived\s*[-–—:]\s*"
    r"(needs:\s*tester\s+repro|loop/process|docs\s+only|owner\s+ruling)\b"
)

# Gate 3 names one pytest file under tests/ (any depth), and its test functions
# are counted.
TEST_FILE = re.compile(r"^tests/(?:[\w.-]+/)*test_[\w-]*\.py$")
FAIL_BEFORE_EXAMPLE = "    - **Fail-before:** tests/test_parser.py 7 -> 8\n\n"

# The counts above are typed by the author, so they are a claim like any other.
# This one is not: every pytest case is a `def test_` at the start of a line, so
# counting them in the named file at the fork point and at the PR head measures
# whether a case was added without asking anybody. It is what separates a new
# case from a rewritten expectation: a diff that changes lines in a test file,
# keeps the case count, and inverts one existing assertion to match the new
# behavior passes every line-based check, and is exactly the change that gets
# reopened minutes after the merge.
CHECK_CASE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+test_", re.MULTILINE)
REWRITTEN_EXPECTATION = (
    "A change that keeps the case count and rewrites an expected value is pinned by "
    "nothing: the sweep stays green and the defect comes back."
)


def check_case_count(ref: str, path: str) -> int | None:
    """`def test_` cases in one pytest file at one ref, or None if the blob is unreadable."""
    content = get_file_content_at_commit(ref, path)
    return None if content is None else len(CHECK_CASE.findall(content))


def merge_base(base: str, head: str) -> str | None:
    """Where the branch actually forked, or None if this checkout cannot say.

    Not the base tip. The loop runs many workers at once, so by the time one
    merges, `baseRefOid` has moved past the point its branch left. Compared
    against the base tip, a branch that forked before another PR added cases
    looks as if it deleted them; against the fork point it does not.

    None when the objects are not here to compare — an unfetched branch is not
    evidence of anything, and a gate that refuses on missing data is a gate
    somebody turns off.
    """
    if not base or not head:
        return None
    try:
        proc = subprocess.run(
            ["git", "merge-base", base, head],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def get_file_content_at_commit(head_sha: str, path: str) -> str | None:
    """Read file content at a specific commit SHA using git show."""
    if not head_sha or not path:
        return None
    clean_path = path.lstrip("./")
    try:
        proc = subprocess.run(
            ["git", "show", f"{head_sha}:{clean_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass

    # If git show failed because the commit isn't local, try fetching from origin
    try:
        subprocess.run(
            ["git", "fetch", "origin", head_sha],
            capture_output=True,
            timeout=8,
        )
        proc = subprocess.run(
            ["git", "show", f"{head_sha}:{clean_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass
    return None


def sweep_runner_at(head_sha: str) -> bool:
    """Whether the tree at head_sha holds the sweep runner. False when the commit
    cannot be read, so an unfetched head asks for nothing (as Gate 3 does)."""
    if not head_sha:
        return False
    try:
        local = (
            subprocess.run(
                ["git", "cat-file", "-e", f"{head_sha}^{{commit}}"],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
        if local:
            return (
                subprocess.run(
                    ["git", "cat-file", "-e", f"{head_sha}:{SWEEP_RUNNER}"],
                    capture_output=True,
                    timeout=5,
                ).returncode
                == 0
            )
    except Exception:
        return False
    return get_file_content_at_commit(head_sha, SWEEP_RUNNER) is not None


SHARED_REPO_DEFAULT = str(pathlib.Path(__file__).resolve().parents[2])


def get_shared_repo_dir() -> str:
    """Return the path to the shared checkout, overridable by SHARED_REPO_PATH for tests."""
    repo = os.environ.get("SHARED_REPO_PATH", "").strip()
    return repo or SHARED_REPO_DEFAULT


def check_shared_checkout_clean() -> None:
    """Refuse merge if shared checkout is dirty or local main is ahead of origin/main."""
    repo_dir = get_shared_repo_dir()
    if not os.path.exists(repo_dir):
        return

    # Check 1: git status --porcelain must be empty
    try:
        proc_status = subprocess.run(
            ["git", "-C", repo_dir, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc_status.returncode == 0 and proc_status.stdout.strip():
            dirty = proc_status.stdout.strip()
            block(
                f"Shared repository checkout at `{repo_dir}` is dirty.\n\n"
                f"Unreviewed writes or untracked changes were detected:\n"
                f"```\n{dirty}\n```\n\n"
                "Nothing merges while unreviewed edits exist in the shared checkout: "
                "the next deploy would ship them. Every session works in its own "
                "worktree, never in the shared checkout.\n"
                "Revert, stash, or clean unreviewed changes in the shared checkout before merging."
            )
    except Exception:
        pass

    # Check 2: git rev-list origin/main..main must be empty
    try:
        proc_rev = subprocess.run(
            ["git", "-C", repo_dir, "rev-list", "origin/main..main"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc_rev.returncode == 0 and proc_rev.stdout.strip():
            commits = proc_rev.stdout.strip()
            block(
                f"Local `main` at `{repo_dir}` has unpushed stray commits ahead of `origin/main`.\n\n"
                f"Unpushed commits detected:\n"
                f"```\n{commits}\n```\n\n"
                "Nothing merges while stray commits exist on local `main`.\n"
                "Unreviewed commits written directly to local main bypass the pull request review loop.\n"
                "Reset or clean local `main` to `origin/main` before merging."
            )
    except Exception:
        pass


def check_pr_merge(command: str) -> None:
    """Pre-merge gates 0-6, in the order a refusal is cheapest to act on.

    Gate 0: the shared checkout is clean (`git status --porcelain` is empty) and
    local main has no unpushed commits ahead of origin/main. Catches direct
    writes to the shared checkout, which the next deploy would ship unreviewed.

    Gate 1: every changed .py file at the PR head passes ast.parse and holds no
    git conflict markers (^(<{7}|={7}|>{7})). Catches a hand-resolved conflict
    that left a marker or a broken line behind.

    Gate 2: the PR body's `Sweep SHA` is the head, and the `green:` line matches
    the record the sweep wrote for that head. Catches a sweep claimed for an
    older head (a rebase or a later push) and a line typed rather than pasted.

    Gate 3: `- **Fail-before:** tests/<path>/test_<name>.py <N> -> <M>` with
    M > N, or an explicit waiver, and the claim is checked: the named pytest file
    gains lines, and its `def test_` count rises between the fork point and the
    head. Catches an expected value rewritten to match a change.

    Gate 4: a merge that would auto-close an issue labelled `needs:live` is
    refused. Its acceptance is only observable in the live environment, so the
    pull request writes `Refs #N` and the close waits for the observation.

    Gate 5: a passing review record exists for the PR head ($REVIEW_DIR/<sha>.json,
    written by review_gate.py). See review_gate_check.

    Gate 6: the PR changes nothing under $GUARD_PROTECTED_PATHS, the owner's files.
    """
    check_shared_checkout_clean()
    repo_match = REPO_FLAG.search(command)
    repo = ""
    if repo_match:
        repo = next(g for g in repo_match.groups()[1:] if g is not None)

    prs: list[str] = []
    if MERGE.search(command):
        segment = MERGE_SEGMENT.search(command)
        if segment:
            found = BARE_NUMBER.search(segment.group(1))
            prs.append(found.group(1) if found else "")
        prs += REST_PR_TARGET.findall(command)

    for pr in prs[:3]:
        view = ["gh", "pr", "view"]
        if pr:
            view.append(pr)
        data = gh_json(
            view
            + ["--json", "headRefOid,baseRefOid,body,files,closingIssuesReferences"],
            repo,
        )
        if not isinstance(data, dict):
            continue

        head_sha = str(data.get("headRefOid") or "")
        base_sha = str(data.get("baseRefOid") or "") or "origin/main"
        body = str(data.get("body") or "")
        files = data.get("files") or []
        target = f"PR #{pr}" if pr else "PR"

        # Gate 4: a merge must not close an issue whose acceptance is only
        # observable live. `closingIssuesReferences` is populated from `Closes
        # #N` and from nothing else, so a non-empty answer here is GitHub
        # confirming the auto-close, not a guess about the body's wording.
        closing = [
            str(ref["number"])
            for ref in (data.get("closingIssuesReferences") or [])[:10]
            if isinstance(ref, dict) and ref.get("number")
        ]
        # With live checks off (POOL_LIVE unset or 0) nobody is there to run the
        # check, so the label has no owner and the gate would only strand the issue.
        live_on = os.environ.get("POOL_LIVE", "0").strip().lower() in (
            "1",
            "on",
            "true",
            "yes",
        )
        live = (
            [n for n in closing if LIVE in (issue_labels(n, repo) or set())]
            if live_on
            else []
        )
        if live:
            which = ", ".join(f"#{n}" for n in live)
            block(
                f"{which} carries `{LIVE}`, and this merge would close it.\n\n"
                "That label says the acceptance criterion cannot be settled by the "
                "sweep — it needs the live environment. An issue like that has to "
                "stay open past the merge, because closing it is what takes it out "
                "of everyone's queue, and a check owed after the close is a check "
                "nobody is holding.\n\n"
                f"Change `Closes #{live[0]}` to `Refs #{live[0]}` in the pull request "
                "body, then merge. The issue stays open, the merger swaps "
                f"`stage:pr` for `{LIVE_STAGE}`, and it closes when somebody posts a "
                "comment with a line reading\n\n"
                f"```\n{LIVE_LINE}\n```\n\n"
                "naming what was run where the behavior was actually seen.\n\n"
                "If the label is wrong — the acceptance really is checkable from a "
                "test — say so on the issue. The monitor clears it; a merge does not."
            )

        # Gate 6: the protected paths are the owner's.
        protected = protected_paths()
        touched = [
            str(f.get("path") or "")
            for f in (files if isinstance(files, list) else [])
            if isinstance(f, dict) and is_protected(str(f.get("path") or ""), protected)
        ]
        if touched:
            block(
                f"{target} changes the owner's files: {', '.join(f'`{p}`' for p in touched[:5])}.\n\n"
                "This project lists them in GUARD_PROTECTED_PATHS "
                f"({', '.join(f'`{p}`' for p in protected)}): the owner's spec, reference data or "
                "fixtures. A pull request does not change them.\n\n"
                f"Take those files out of the branch (`git checkout origin/main -- {' '.join(touched[:5])}`, "
                "commit, sweep again). A test that needs other data brings its own copy under the "
                "tests. If the owner's file itself looks wrong, say so on the issue for the owner."
            )

        # Gate 1: Check AST parse and conflict markers on changed .py files of PR head commit
        changed_py: list[str] = []
        if isinstance(files, list):
            for f in files:
                if isinstance(f, dict):
                    p = str(f.get("path") or "")
                    if p.endswith(".py") and f.get("changeType") != "DELETED":
                        changed_py.append(p)

        if not changed_py and head_sha:
            try:
                proc = subprocess.run(
                    [
                        "git",
                        "diff-tree",
                        "--no-commit-id",
                        "--name-only",
                        "-r",
                        head_sha,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    for line in proc.stdout.splitlines():
                        line = line.strip()
                        if line.endswith(".py"):
                            changed_py.append(line)
            except Exception:
                pass

        for p in changed_py:
            content = get_file_content_at_commit(head_sha, p)
            if content is None:
                continue
            m = CONFLICT_MARKER.search(content)
            if m:
                block(
                    f"Conflict marker `{m.group(1)}` found in `{p}` at PR head `{head_sha[:7]}`.\n\n"
                    "A hand-resolved conflict left a marker in the file. "
                    "Resolve the conflict markers and update the PR before merging."
                )
            try:
                ast.parse(content, filename=p)
            except SyntaxError as e:
                block(
                    f"Syntax error in `{p}` at PR head `{head_sha[:7]}` (line {e.lineno}): {e.msg}.\n\n"
                    "The Python file fails `ast.parse`. Fix syntax errors before merging."
                )

        # Gate 2: Extract Sweep SHA from PR body and assert head_sha.startswith(sweep_sha)
        m_sweep = SWEEP_SHA_PATTERN.search(body)
        if not m_sweep:
            block(
                f"{target} body is missing `Sweep SHA`.\n\n"
                "Every PR body must record `- **Sweep SHA:** <HEAD sha>` to verify that "
                "the full check suite was measured against the head being merged.\n\n"
                f"Run the sweep at the current head ({SWEEP_HOW}) and add `Sweep SHA:` "
                "to the PR body."
            )

        sweep_sha = m_sweep.group(1).lower()
        if not head_sha.lower().startswith(sweep_sha):
            block(
                f"{target} head `{head_sha}` does not match Sweep SHA `{sweep_sha}`.\n\n"
                "The PR's head commit has moved since the sweep was measured (a rebase "
                "or a later push). A new head needs a new sweep.\n\n"
                f"Re-run the sweep at the current head ({SWEEP_HOW}; with "
                "`--update-pr <N>` it also fixes the body) before merging."
            )

        # Gate 2: the green line, checked against the record the sweep wrote.
        if not SWEEP_WAIVER.search(body):
            m_breakdown = SWEEP_LINE_PATTERN.search(body)
            if m_breakdown:
                stated_total = int(m_breakdown.group(2))
                breakdown_str = m_breakdown.group(3)
                pairs = re.findall(r"([a-zA-Z0-9_-]+)\s+(\d+)", breakdown_str)
                actual_sum = sum(int(n) for _, n in pairs)
                if stated_total != actual_sum:
                    diff = actual_sum - stated_total
                    diff_s = f"+{diff}" if diff > 0 else f"{diff}"
                    block(
                        f"{target} sweep breakdown sum does not match stated total: "
                        f"`{stated_total} green:` vs sum of areas `{actual_sum}` ({diff_s}).\n\n"
                        "The stated total must equal the sum of the per-area counts.\n"
                        "Paste the sweep's literal `green:` line into the PR body "
                        "rather than retyping it."
                    )
                # The line above is typed into the body; the record is written by
                # the sweep itself, and only for a green run on a clean commit. A
                # real line measured on a dirty tree before the commit fails here.
                record = load_sweep_record(head_sha)
                if record is None:
                    record_dir = (
                        os.environ.get("SWEEP_RECORD_DIR") or SWEEP_RECORD_DIR_DEFAULT
                    )
                    block(
                        f"{target} has no sweep record for its head `{head_sha[:7]}`.\n\n"
                        f"The sweep writes `{record_dir}/<sha>.json` only for a full, green "
                        "sweep started from a clean tree, so the `green:` line in the body "
                        "was not measured at this commit.\n\n"
                        "Commit first, then run the full sweep at that commit "
                        f"({SWEEP_HOW}; its `git:` line must read `(clean)`, and it prints "
                        "`sweep record: <path>`), paste its `green:` line, and push."
                    )
                rec_areas = record.get("areas") or {}
                stated_areas = {s: int(n) for s, n in pairs}
                if record.get("total") != stated_total or rec_areas != stated_areas:
                    diffs = [
                        f"{s} {stated_areas.get(s, '-')} vs {rec_areas.get(s, '-')}"
                        for s in sorted(set(stated_areas) | set(rec_areas))
                        if stated_areas.get(s) != rec_areas.get(s)
                    ]
                    block(
                        f"{target} sweep line does not match the sweep record for `{head_sha[:7]}`: "
                        f"body `{stated_total} green:` vs record `{record.get('total')}`"
                        + (f"; {', '.join(diffs)}" if diffs else "")
                        + ".\n\n"
                        "Paste the `green:` line from the sweep that ran at this head, "
                        "every area included."
                    )
            else:
                m_count = SWEEP_COUNT_PATTERN.search(body)
                if m_count:
                    block(
                        f"{target} reports summary count `{m_count.group(0)}` but is missing the full sweep line.\n\n"
                        "Every PR body must record the sweep's line, every area included:\n\n"
                        "    <total> green: <area1> <n1> · <area2> <n2> · ...\n\n"
                        "Paste the sweep's literal `green:` line into the PR body."
                    )
                elif sweep_runner_at(head_sha):
                    block(
                        f"{target} body is missing the sweep's `green:` line or a valid waiver.\n\n"
                        "Every PR body must record the line the sweep printed for this head:\n\n"
                        "    <total> green: <area1> <n1> · <area2> <n2> · ...\n\n"
                        "Or an explicit waiver:\n\n"
                        "    - **Sweep:** waived - loop/process\n"
                        "    - **Sweep:** waived - docs only\n"
                        "    - **Sweep:** waived - owner ruling\n\n"
                        f"Run the sweep ({SWEEP_HOW}) and paste its literal `green:` line "
                        "into the PR body."
                    )

        # Gate 5: a second model reviewed this head (review_gate.py writes the
        # record; a reject has already been handed back to a worker).
        review_gate_check(pr, head_sha, target)

        # Gate 3: the fix must be pinned by a test case that was red before it.
        if FAIL_BEFORE_WAIVER.search(body):
            continue

        m_fb = FAIL_BEFORE_PATTERN.search(body)
        if not m_fb:
            block(f"{target} body is missing `Fail-before`.\n\n" + FAIL_BEFORE_HOW)

        script, before_s, after_s = m_fb.group(1), m_fb.group(2), m_fb.group(3)
        if int(after_s) <= int(before_s):
            block(
                f"{target} `Fail-before` does not show a case going green: "
                f"`{script} {before_s} -> {after_s}`.\n\n"
                "The count at the branch must exceed the count at the base. Equal "
                f"counts mean the fix is pinned by nothing. {REWRITTEN_EXPECTATION}\n\n"
                "Write a case that reproduces the defect, show it red at the base, "
                "and update the line."
            )

        script = script.strip("`")
        while script.startswith("./"):
            script = script[2:]
        if not TEST_FILE.match(script):
            block(
                f"{target} `Fail-before` names `{script}`, which is not a pytest file "
                "under `tests/`.\n\n"
                "Name the test file that holds the reproducing case, e.g.\n\n"
                + FAIL_BEFORE_EXAMPLE
                + "or waive the gate with one of the four reasons."
            )

        touched_checks = [
            str(f.get("path") or "")
            for f in (files if isinstance(files, list) else [])
            if isinstance(f, dict)
            and str(f.get("path") or "") == script
            and int(f.get("additions") or 0) > 0
        ]
        if not touched_checks:
            block(
                f"{target} claims `{script} {before_s} -> {after_s}` but adds no "
                f"lines to `{script}`.\n\n"
                "A count that rose without the test file growing means the case "
                "was already there and its expected value was rewritten. That is "
                "not a fix; it is the issue's `Acceptance` being renegotiated "
                "inside a diff, where nobody reviews it.\n\n"
                "Add the case. If you believe the existing expectation is wrong, "
                "stop and say so on the issue: that is the owner's call."
            )

        # Count the cases rather than the lines. An unreadable head means the
        # branch is not fetched here, and a gate that cannot see is a gate that
        # says nothing — the sweep gate above treats a missing blob the same way.
        # A file absent at a fork point this checkout knows is a new test file,
        # which starts from zero cases.
        grew = False
        measured = False
        deltas: list[str] = []
        fork_sha = merge_base(base_sha, head_sha)
        for path in touched_checks if fork_sha else []:
            after_n = check_case_count(head_sha, path)
            if after_n is None:
                continue
            before_n = check_case_count(fork_sha, path) or 0
            measured = True
            deltas.append(f"`{path}` {before_n} -> {after_n}")
            if after_n > before_n:
                grew = True

        if measured and not grew:
            block(
                f"{target} adds lines to a test file but no case: "
                + ", ".join(deltas)
                + f".\n\nCounting `def test_` functions at the fork point `{(fork_sha or '')[:7]}` and "
                f"at the PR head `{head_sha[:7]}`, the number did not rise. Lines changed "
                "and cases did not, which means an existing assertion was "
                f"rewritten to match the new behavior. {REWRITTEN_EXPECTATION}\n\n"
                "Write the case that reproduces the defect and show it red at the "
                "base. If you believe an existing expectation is wrong, stop and "
                "say so on the issue — that is the owner's call, not a diff."
            )


FAIL_BEFORE_HOW = (
    "Every PR body must record one line:\n\n"
    "    - **Fail-before:** tests/<path>/test_<name>.py <passing at base> -> "
    "<passing at branch>\n\n"
    "Run the test file that holds the reproducing case (`pytest -q <file>`, or "
    "however this project runs one file) against the unpatched base and against "
    "your branch, and put both passing counts in. The second must be higher: that "
    "is what says a case went red before the fix and green after.\n\n"
    "If no case can be written, say which of the four situations this is "
    "and waive it explicitly:\n\n"
    "    - **Fail-before:** waived - needs: tester repro\n"
    "    - **Fail-before:** waived - loop/process\n"
    "    - **Fail-before:** waived - docs only\n"
    "    - **Fail-before:** waived - owner ruling\n\n"
    "See the merge gates in .claude/skills/issuelooper-shared/SKILL.md."
)


# Gate 5, the review gate. `.claude/hooks/review_gate.py --pr N` has a second
# model read the diff and writes its verdict for the PR's head sha to
# $REVIEW_DIR/<sha>.json. pass, skipped (no production file), error (the judge
# failed: fail open) and cap (rejected too often already) let the merge go
# ahead; reject and no record do not. REVIEW_GATE=off (or 0) skips the gate.
REVIEW_DIR_DEFAULT = "/tmp/issuelooper-review"
REVIEW_ALLOWS = {"pass", "skipped", "error", "cap"}


def review_gate_on() -> bool:
    return os.environ.get("REVIEW_GATE", "on").strip().lower() not in (
        "off",
        "0",
        "false",
        "no",
    )


def review_gate_check(pr: str, head_sha: str, target: str) -> None:
    if not review_gate_on() or not head_sha:
        return
    record_dir = pathlib.Path(os.environ.get("REVIEW_DIR") or REVIEW_DIR_DEFAULT)
    try:
        record = json.loads((record_dir / f"{head_sha.lower()}.json").read_text())
    except (OSError, ValueError):
        record = None
    verdict = record.get("verdict") if isinstance(record, dict) else None
    if verdict in REVIEW_ALLOWS:
        return
    n = pr or "<N>"
    if verdict == "reject":
        block(
            f"{target} head `{head_sha[:7]}` was rejected by the review gate: "
            f"{str(record.get('reason') or '')[:600]}\n\n"
            "review_gate.py has already commented on the PR and moved the issue from "
            "`stage:pr` to `stage:triage`, so a worker fixes it on the same branch. "
            "Do not merge and do not hand it back again; stop here."
        )
    block(
        f"{target} head `{head_sha[:7]}` has no review record.\n\n"
        "Before a merge, a second model reads the diff for what tests do not catch "
        "(made-up data shown as real, code that exists to pass tests, and this "
        "project's review rules). Run\n\n"
        f'    python3 "$SHARED_REPO_PATH/.claude/hooks/review_gate.py" --pr {n}\n\n'
        "on the head you are merging (again after every rebase or push). Exit 0: merge. "
        "Exit 1: it rejected the PR and handed it back; stop. Exit 2: GitHub could not be "
        "read; run it again."
    )


def claimed_issue() -> str | None:
    """The issue number in $WRK_CLAIM_FILE, or None when there is no readable claim."""
    path = os.environ.get(CLAIM_FILE_ENV, "").strip()
    if not path:
        return None
    try:
        return str(int(json.loads(pathlib.Path(path).read_text())["issue"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ccpool.py starts one merger per conflicting pull request and names it in
# $MERGE_PR. Mergers run side by side, so one landing another's PR would race it.
MERGE_PR_ENV = "MERGE_PR"


def merger_pr_scope_check(command: str) -> None:
    """A merger session merges nothing: the supervisor merges (its units are `merge:...`,
    a merger session's `mrg:...`). Otherwise a merge names only the assigned PR."""
    mine = os.environ.get(MERGE_PR_ENV, "").strip().lstrip("#")
    if os.environ.get("POOL_UNIT", "").startswith("mrg:"):
        block(
            "A merger session does not merge. Stop with the issue still at `stage:pr`: the "
            "supervisor merges your new head on its next poll, after checking it merged with "
            "the current main and running the merge gates, so every merge passes the same "
            "checks. A merger that merged a head behind main broke main more than once.\n\n"
            "If the PR conflicts again by then, the supervisor starts another merger session."
        )
    if not mine:
        return
    prs: list[str] = []
    segment = MERGE_SEGMENT.search(command)
    if segment:
        found = BARE_NUMBER.search(segment.group(1))
        prs.append(found.group(1) if found else "")
    prs += REST_PR_TARGET.findall(command)
    other = [p for p in dict.fromkeys(prs) if p != mine]
    if other:
        which = (
            ", ".join(f"#{p}" for p in other if p)
            or "a pull request this guard cannot identify"
        )
        block(
            f"This merger was started for pull request #{mine}, and this merges {which}.\n\n"
            "The supervisor runs one merger per pull request at the same time; each one "
            "lands only its own, so two mergers never race for one merge. Name the PR "
            f"explicitly: `gh pr merge {mine} --squash --delete-branch --match-head-commit <sha>`. "
            "The other PR has, or will get, its own merger."
        )


def worker_git_check(command: str) -> None:
    """The worker pushes branches, never main, and never commits the framework."""
    for m in GIT_PUSH.finditer(command):
        if PUSH_TO_MAIN.search(m.group(1)):
            block(
                "The worker pushes its issue branch, never `main`.\n\n"
                "`main` moves only through the merger's `gh pr merge`, after it has read "
                "the diff: that is the one independent look this loop has. Push "
                "`issue-<N>` and open the pull request."
            )
    if NO_VERIFY.search(command):
        block(
            "`--no-verify` skips the repository's git hooks, which are what keep "
            "the framework's files out of your commit and forced pushes off `main`. "
            "Drop the flag."
        )
    if GIT_FRAMEWORK_PATHS.search(command):
        block(
            "`.claude/`, `.agents/` and `.issuelooper/` belong to the owner; a "
            "worker's commit never carries them. Stage only the project files "
            "you changed (`git add src tests ...`). To propose a change to the "
            "framework or its settings, say so on the issue."
        )


def worker_label_check(command: str, added: set[str], removed: set[str]) -> None:
    """Stage labels move only on the claimed issue, and stage:pr clears stage:doing."""
    stage = {label for label in added | removed if label.startswith("stage:")}
    if not stage:
        return
    targets: list[str] = []
    seg = ISSUE_EDIT_SEGMENT.search(command)
    if seg:
        found = BARE_NUMBER.search(seg.group(1))
        if found:
            targets.append(found.group(1))
    targets += REST_LABELS_TARGET.findall(command)
    mine = claimed_issue()
    if mine is None:
        block(
            f"This moves {', '.join(sorted(stage))}, and this worker session has no "
            f"readable claim (${CLAIM_FILE_ENV}).\n\n"
            "A worker moves stage labels only on the issue the supervisor claimed for it. "
            "Claims are made by `.claude/hooks/claim.py claim` through ccpool.py, "
            "before the session starts; to give one up, run `claim.py release`."
        )
    other = [t for t in dict.fromkeys(targets) if t != mine]
    if other or not targets:
        which = (
            ", ".join(f"#{t}" for t in other) or "an issue this guard cannot identify"
        )
        block(
            f"This worker's claim is #{mine}, and this command moves "
            f"{', '.join(sorted(stage))} on {which}.\n\n"
            "Each worker session owns exactly one issue, and the claim protocol is "
            "the only thing keeping two workers off the same issue. Say what you found on "
            "the other issue's thread instead; its own claimant or the monitor moves it."
        )
    if "stage:pr" in added and "stage:doing" not in removed:
        block(
            "Advancing your claimed issue to `stage:pr` must also clear your claim: "
            "`--remove-label stage:doing,stage:triage,stage:ready,stage:owner`."
        )


def is_draft_pr(command: str) -> bool:
    """Return True if command includes --draft or -d flag."""
    try:
        tokens = shlex.split(command)
        return any(t in ("--draft", "-d") for t in tokens)
    except ValueError:
        return bool(DRAFT_FLAG.search(command))


def extract_pr_create_body(command: str) -> str | None:
    """Extract PR body from --body-file, -F, --body, or -b flags."""
    m_file = PR_BODY_FILE.search(command)
    if m_file:
        path = next(
            g
            for g in (m_file.group(2), m_file.group(3), m_file.group(4))
            if g is not None
        )
        if path == "-":
            return None
        try:
            return pathlib.Path(path).read_text(errors="replace")
        except OSError:
            return None

    try:
        tokens = shlex.split(command)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("--body", "-b") and i + 1 < len(tokens):
                return tokens[i + 1]
            if tok.startswith(("--body=", "-b=")):
                return tok.split("=", 1)[1]
            i += 1
    except ValueError:
        pass

    m_body = CLI_BODY_FLAG.search(command)
    if m_body:
        return next(
            g
            for g in (m_body.group(1), m_body.group(2), m_body.group(3))
            if g is not None
        )

    return None


def check_pr_create(command: str) -> None:
    """Pre-creation gates: draft carve-out, Sweep SHA, and Fail-before verification.

    Draft carve-out: If --draft or -d is present, allow without Gate 2/3 checks.
    Gate 2: Must contain `- **Sweep SHA:** <HEAD sha>`. Refuse if missing.
    Gate 3: Must contain `- **Fail-before:** tests/<path>/test_<name>.py <N> -> <M>`
    or one of the 4 valid waivers. Refuse if missing.
    """
    if is_draft_pr(command):
        return

    body = extract_pr_create_body(command)
    if body is None:
        return

    m_sweep = SWEEP_SHA_PATTERN.search(body)
    if not m_sweep:
        block(
            "PR body is missing `Sweep SHA`.\n\n"
            "Every PR body must record `- **Sweep SHA:** <HEAD sha>` to verify that "
            "the full check suite was measured against the head commit being submitted.\n\n"
            "Run the sweep at the current head and add `- **Sweep SHA:** <HEAD sha>` to the PR body."
        )

    if FAIL_BEFORE_WAIVER.search(body):
        return

    m_fb = FAIL_BEFORE_PATTERN.search(body)
    if not m_fb:
        block("PR body is missing `Fail-before`.\n\n" + FAIL_BEFORE_HOW)

    script, before_s, after_s = m_fb.group(1), m_fb.group(2), m_fb.group(3)
    if int(after_s) <= int(before_s):
        block(
            f"PR `Fail-before` does not show a case going green: "
            f"`{script} {before_s} -> {after_s}`.\n\n"
            "The count at the branch must exceed the count at the base. Equal "
            f"counts mean the fix is pinned by nothing. {REWRITTEN_EXPECTATION}\n\n"
            "Write a case that reproduces the defect, show it red at the base, "
            "and update the line."
        )

    script = script.strip("`")
    while script.startswith("./"):
        script = script[2:]
    if not TEST_FILE.match(script):
        block(
            f"PR `Fail-before` names `{script}`, which is not a pytest file under "
            "`tests/`.\n\n"
            "Name the test file that holds the reproducing case, e.g.\n\n"
            + FAIL_BEFORE_EXAMPLE
            + "or waive the gate with one of the four reasons."
        )


def main(raw: str) -> None:
    payload = json.loads(raw)
    command, session_id = read_call(payload)
    role = resolve_role(session_id)
    record_heartbeat(session_id, role, command)

    if not command:
        call = payload.get("toolCall") if isinstance(payload, dict) else None
        if (
            isinstance(call, dict)
            and call.get("name")
            and call.get("name") != "run_command"
        ):
            allow()

        if CC_PAYLOAD.search(raw) or AGY_PAYLOAD.search(raw):
            block(
                "Unparseable command execution payload from recognized host: "
                "the guard could not extract a command to inspect. "
                "Refusing rather than failing open."
            )
        allow()

    # Before the write test, because this one is not a `gh` write.
    secret_check(command)
    kill_by_pattern_check(command)

    if role == "worker":
        worker_git_check(command)

    if not any(re.search(p, command) for p in WRITE_PATTERNS):
        allow()

    if role not in ROLES:
        block(
            "This session has no role, so it may not write to GitHub.\n\n"
            "The roles are worker, merger, tester, monitor and aide; which one you are "
            "decides what you may label and how your comments are signed. Do not pick a "
            "role from a skill. Ask the owner which one this session is, then either "
            "relaunch with `.claude/hooks/ccrole.sh <role>` or have them run "
            f"`.claude/hooks/ccrole.sh assign {session_id or '<session-id>'} <role>`.\n\n"
            "Reads are unaffected: you can keep investigating and report what you found."
        )

    if MERGE.search(command) and role != "merger":
        block(
            f"Only the supervisor merges, and this session is the {role}.\n\n"
            "Mergeable pull requests are landed by the supervisor (ccpool.py) once the "
            "merge gates and the review gate pass; a merger session resolves one that "
            "conflicts with main and leaves the merge to it. Open the PR, put `stage:pr` "
            "on the issue, and leave it."
        )

    if CLOSES.search(command) and role in ("tester", "aide", "worker"):
        block(
            f"The {role} files and comments; it does not close.\n\n"
            "You see one failing page, or one thing the owner asked you to write "
            "down — not the queue. Whether the issue behind it is fixed, duplicated, "
            "overtaken or not worth doing is a judgement made from the backlog, and "
            "that is the monitor's.\n\n"
            "Say it on the thread and leave the state alone."
        )

    marker = ROLES[role]
    if any(re.search(p, command) for p in BODY_PATTERNS):
        has_body, is_signed = cli_comment_status(command, marker)
        if not has_body:
            refuse_unreadable_body(role)
        if not is_signed:
            refuse_unsigned_comment(role, marker)

    is_comment, body = rest_comment_body(command)
    if is_comment:
        if body is None:
            refuse_unreadable_body(role)
        if not body.lstrip("\ufeff \t\r\n").startswith(marker):
            refuse_unsigned_comment(role, marker)

    added, removed = labels_in(command)
    for label in sorted(added):
        rule = LABEL_RULES.get(label)
        if rule and role not in rule["add"]:
            who = (
                ", ".join(sorted(rule["add"]))
                if rule["add"]
                else "no session — this is the owner's"
            )
            block(
                f"`{label}` may not be applied by the {role}: that label belongs to "
                f"{who} — see the label table in issuelooper-shared/SKILL.md.\n\n"
                "If you need the effect it would have had, say so on the thread and ask. "
                "For keeping an issue out of the pick order, the correct instrument is an "
                "issue dependency, which any role may record and which says what lifts it."
            )
    for label in sorted(removed):
        rule = LABEL_RULES.get(label)
        if rule and role not in rule["remove"]:
            who = (
                ", ".join(sorted(rule["remove"]))
                if rule["remove"]
                else "no session — this is the owner's"
            )
            block(
                f"`{label}` may not be cleared by the {role}: clearing it belongs to "
                f"{who} — see the label table in issuelooper-shared/SKILL.md.\n\n"
                "Clearing another role's label overrules a judgement you cannot see the "
                "reasons for. Ask on the thread instead."
            )

    if role == "worker":
        worker_label_check(command, added, removed)

    stage_added = {label for label in added if label.startswith("stage:")}
    if len(stage_added) > 1:
        block(
            f"Cannot apply multiple stage labels at once ({', '.join(sorted(stage_added))}).\n\n"
            "An issue may carry exactly one `stage:` label at any moment."
        )

    if "stage:pr" in added:
        missing = STAGE_PR_REQUIRED_CLEARS - removed
        if missing:
            block(
                "Advancing an issue to `stage:pr` must clear prior stage labels "
                f"({', '.join(sorted(STAGE_PR_REQUIRED_CLEARS))}).\n\n"
                f"Missing from remove: {', '.join(sorted(missing))}.\n\n"
                "An issue may carry exactly one `stage:` label at any moment. The triage -> "
                "pr shortcut leaves stage:triage set if only stage:ready is cleared. "
                "Include `--remove-label stage:ready,stage:triage,stage:owner` in the command."
            )

    # Last, and only on a close or a merge, because it is the one rule that costs
    # a network round trip. A command that lifts the hold in the same breath is
    # the sanctioned way through: `gh issue edit N --remove-label hold:monitor &&
    # gh issue close N` reads the label as still set, and refusing that would
    # refuse the monitor's own way of ending a hold.
    if HOLD not in removed and (CLOSES.search(command) or MERGE.search(command)):
        held = held_issues(command)
        if held:
            refuse_held(role, held)

    if MERGE.search(command):
        merger_pr_scope_check(command)
        check_pr_merge(command)

    if CLOSES.search(command) and LIVE not in removed:
        check_issue_close(command)

    if PR_CREATE.search(command):
        check_pr_create(command)

    allow()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-alive":
        session_arg = (
            sys.argv[2]
            if len(sys.argv) > 2 and not sys.argv[2].startswith("-")
            else None
        )
        sys.exit(check_alive(session_id=session_arg))
    if len(sys.argv) > 1:
        # Sessions have run `role_guard.py check-pr <N>`, which is not a command; the
        # hook then waited on a stdin that never closed until a watchdog killed it.
        sys.exit(
            "role_guard.py is the tool-call hook, not a command: it takes no arguments and "
            "reads a tool call on stdin. Your own merge or PR command is checked when you run "
            "it. To check a PR body, read it (`gh pr view <N> --json body`) against the PR body "
            "section of your skill."
        )

    # Read stdin and classify the host before anything that can throw, so the
    # handler below answers in that host's vocabulary rather than in whichever
    # one the module happened to default to.
    try:
        RAW = sys.stdin.read()
    except Exception:  # noqa: BLE001
        RAW = ""
    DENY = sniff_deny(RAW)

    try:
        main(RAW)
    except Exception as exc:  # noqa: BLE001
        note = f"role_guard error, allowing: {exc}"
        if DENY == "deny":
            # Bare `decision`, and the note goes to stderr. agy's PreToolHookResult
            # carries decision, reason, overwrite, permission_overrides, allow_tool
            # and deny_reason — there is no `systemMessage` on it. (The binary does
            # contain that string, which is why this looks wrong at a glance: it
            # belongs to `HookSystemMessage` and `HookInjectedStep`, which are a
            # different message. Checked against the getters on the result type.)
            # The binary also carries `DisallowUnknownFields`, so an undeclared
            # field risks the object being rejected outright — inside the error
            # handler, which would turn the fail-open into a hard failure. stderr
            # costs nothing and agy already surfaces it.
            print(json.dumps({"decision": "allow"}))
        else:
            # Claude Code does declare `systemMessage`, so it keeps it.
            print(json.dumps({"systemMessage": note}))
        print(note, file=sys.stderr)
