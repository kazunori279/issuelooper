#!/usr/bin/env python3
"""PreToolUse guard: no GitHub write without a role, and no role writing outside it.

The issue loop runs sessions in five roles: worker (one issue to a pull request),
merger (lands a pull request that conflicts with main), tester (live checks and the
UX run), monitor (triage and the owner's decisions) and aide (an ad-hoc session
that files and comments). All of them authenticate to GitHub as the same login, so
which one acted is carried by a comment marker and by which labels each may touch.
This hook enforces both: a session with no role cannot write to GitHub at all, and
a session with a role cannot post unsigned or touch another role's labels.

check_pr_merge holds the merge gates (0-7). ccpool.py runs this guard with
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
    # `stage:accept`: an issue whose PR changed product code ([accept] paths), after its
    # merge (the PR said `Refs #N`), waiting for the tester's acceptance check. The
    # supervisor moves it on; the tester only comments.
    "stage:accept": {"add": {"merger", "monitor"}, "remove": {"monitor"}},
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
    os.environ.get("ROLE_GUARD_HEARTBEAT_FILE", "") or config.scratch_root() / "guard.last"
)
RUNS_DIR = pathlib.Path(
    os.environ.get("ROLE_GUARD_RUNS_DIR", "") or config.scratch_root() / "guard-runs"
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

# The acceptance check (POOL_ACCEPT, [accept] paths): a PR that changes product code
# says `Refs #N`, and its issue waits at `stage:accept` (or `stage:live`, from before)
# for the tester's `accept-verified:` line.
ACCEPT_STAGE = "stage:accept"
ACCEPT_EVIDENCE = re.compile(r"(?im)^[ \t>*-]*(?:accept|live)-verified:[ \t]*\S")
CLOSING_REF = re.compile(r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)\b")


# Only the chore that turns tester-verified scripts into tests adds files under
# tests/accept/; its issue carries this marker (ccpool.ACCEPT_CHORE). In cohort 55, 117 of
# its 138 files were workers' tests of their own issues, and it took 14 minutes.
ACCEPT_CHORE = "<!-- accept-chore -->"
ACCEPT_TESTS = "tests/accept/"


def is_accept_chore(issues: list[str], repo: str) -> bool:
    """Whether one of these issues is an acceptance chore (its body has the marker)."""
    for n in list(dict.fromkeys(issues))[:5]:
        data = gh_json(["gh", "issue", "view", str(n), "--json", "body"], repo)
        if isinstance(data, dict) and ACCEPT_CHORE in str(data.get("body") or ""):
            return True
    return False


# A PR whose issue or spec changes what an older acceptance guard asserts names each guard
# file it changes or replaces: `Guard change: tests/accept/test_accept_38.py: <why>`. The
# review gate checks the change against the issue and the spec.
GUARD_CHANGE = re.compile(r"(?im)^[ \t>*-]*(?:\*\*)?Guard change(?:\*\*)?:[*`\s]*(tests/accept/[\w./-]+)")


def guard_changes(body: str) -> set[str]:
    return {m.group(1).rstrip("`.:,") for m in GUARD_CHANGE.finditer(body or "")}


def refuse_accept_tests(added: list[str]) -> None:
    block(
        f"This pull request adds {', '.join(f'`{p}`' for p in added[:3])} under `{ACCEPT_TESTS}`.\n\n"
        "That directory holds only the scripts an independent tester verified, added by an acceptance chore "
        "(an issue the supervisor files with `" + ACCEPT_CHORE + "`). The supervisor runs it on every merge "
        "batch and deploy, so each file there costs every later merge. Put your test in tests/e2e or "
        "tests/unit, extending the test of the same flow where there is one."
    )


def accept_on() -> bool:
    """POOL_ACCEPT (on unless 0) with [accept] paths set."""
    return os.environ.get("POOL_ACCEPT", "1").strip().lower() not in ("0", "off", "false", "no") \
        and bool(config.accept_paths())


def product_paths(paths: list[str]) -> list[str]:
    """The paths among these under [accept] paths (product code)."""
    return [p for p in paths if any(config.path_matches(p, g) for g in config.accept_paths())] if accept_on() else []


def refuse_closing_product_change(which: str, product: list[str]) -> None:
    block(
        f"This pull request changes product code ({', '.join(f'`{p}`' for p in product[:3])}) and would close "
        f"{which} on merge.\n\nA change to `[accept] paths` is accepted after the merge: a tester who did not "
        "write it drives it on the deployed build, through the checklist, and only that closes the issue. "
        f"Write `Refs {which.split(',')[0]}` instead of `Fixes`/`Closes`/`Resolves` in the body; the merge moves "
        "the issue to `stage:accept`."
    )

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
    # GUARD_EXPLICIT_ALLOW: the supervisor's own merges ask for a positive answer, so an
    # empty or broken reply is never read as a pass (Claude Code's allow is `{}`).
    if DENY == "deny" or os.environ.get("GUARD_EXPLICIT_ALLOW", "").strip() == "1":
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
            "with `nohup ... > <log> 2>&1 < /dev/null & echo $! > $ISSUELOOPER_TMP/<name>.pid` and later "
            "`kill $(cat $ISSUELOOPER_TMP/<name>.pid)`."
        )


# A command started in the background keeps this command's stdout, stderr and stdin
# unless it redirects them, and the agent CLI waits until every process holding them
# exits. Run 11: an acceptance tester's `nohup uv run concierge --port 8121 ... &` held
# agy until its 70-minute timeout, and the unit gave no verdict.
BG_OPS = ("&>>", "&>", ">>", ">&", ">|", "<&", "<<<", "<>", ">", "<", "&&", "||", ";;", "|&", "|", "&", ";", "(", ")", "\n")
BG_SEPARATORS = {"&&", "||", ";;", "|&", "|", "&", ";", "(", ")", "\n"}
BG_FD = re.compile(r"(?<![\w$@-])([0-2])(?=[<>])")


def background_on() -> bool:
    return os.environ.get("GUARD_BACKGROUND", "1").strip().lower() not in ("0", "false", "no", "off")


def _bg_tokens(command: str) -> list[str]:
    """Words and shell operators; a file descriptor written before a redirection
    (`2>&1`) becomes the word `@FD2`. [] when the quotes do not balance."""
    text = BG_FD.sub(lambda m: f" @FD{m.group(1)}", HEREDOC.sub("", command))
    lex = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace, lex.whitespace_split = " \t\r", True
    try:
        raw = list(lex)
    except ValueError:
        return []
    out = []
    for t in raw:
        if t and set(t) <= set("();<>|&\n"):
            while t:  # a run of operators, longest first
                op = next(o for o in BG_OPS + tuple(t[0]) if t.startswith(o))
                out.append(op)
                t = t[len(op):]
        else:
            out.append(t)
    return out


def background_jobs(command: str) -> list[str]:
    """The jobs this command leaves running with stdout, stderr or stdin still attached:
    each pipeline that ends in `&` (or starts with `nohup` or `setsid -f`) and does not
    redirect all three away, unless a `wait` later in the command waits for them. One
    level of `bash -c '...'` is looked into."""
    tokens = _bg_tokens(command)
    jobs, cur, pipeline, waited = [], [], [], False

    def close(seg: list[str], sep: str) -> None:
        nonlocal pipeline, waited
        words, fds, fd, k = [], {"0": "tty", "1": "tty", "2": "tty"}, None, 0
        if pipeline:
            fds["0"] = "pipe"
        while k < len(seg):
            t = seg[k]
            if t.startswith("@FD"):
                fd = t[3:]
            elif t in (">", ">>", ">|", "&>", "&>>", ">&", "<", "<&", "<>", "<<<"):
                target = seg[k + 1] if k + 1 < len(seg) else ""
                k += 1
                if t in ("&>", "&>>") or (t == ">&" and fd is None and not target.isdigit()):
                    fds["1"] = fds["2"] = "file"
                elif t in (">&", "<&"):
                    fds[fd or ("1" if t == ">&" else "0")] = fds.get(target, "file") if target != "-" else "closed"
                elif t in ("<", "<>", "<<<"):
                    fds[fd or "0"] = "file"
                else:
                    fds[fd or "1"] = "file"
                fd = None
            else:
                words.append(t)
                fd = None
            k += 1
        while words and ASSIGN.match(words[0]):
            words = words[1:]
        if words[:1] == ["wait"]:
            waited = True
        if sep == "|":
            fds["1"] = "pipe"
        elif sep == "|&":
            fds["1"] = fds["2"] = "pipe"
        pipeline.append((words, fds))
        if sep in ("|", "|&"):
            return
        first = pipeline[0][0]
        lead = os.path.basename(first[0]) if first else ""
        bg = sep == "&" or lead == "nohup" or (lead == "setsid" and ({"-f", "--fork"} & set(first[1:3])))
        attached = pipeline[0][1]["0"] == "tty" or any(f["1"] == "tty" or f["2"] == "tty" for _, f in pipeline)
        if bg and attached:
            jobs.append(" ".join(w for w, _ in pipeline for w in w)[:120] or "(a group)")
        elif not bg and attached:
            for words_, _ in pipeline:
                if words_[:1] and os.path.basename(words_[0]) in ("bash", "sh", "zsh") and "-c" in words_:
                    i = words_.index("-c")
                    if i + 1 < len(words_):
                        jobs.extend(background_jobs(words_[i + 1]))
        pipeline = []

    for t in tokens + ["\n"]:
        if t in BG_SEPARATORS:
            if cur or t == "&" or pipeline:
                close(cur, t)
            cur = []
        else:
            cur.append(t)
    return [] if waited else jobs


def background_check(command: str) -> None:
    if not background_on() or "&" not in command and "nohup" not in command and "setsid" not in command:
        return
    jobs = background_jobs(command)
    if jobs:
        block(
            f"This starts `{jobs[0]}` in the background with its output (or input) still attached to "
            "this command. The agent CLI waits until every process holding them exits, so the session "
            "hangs until its timeout: in run 11 an acceptance tester's `nohup uv run concierge ... &` "
            "blocked agy for 70 minutes and gave no verdict.\n\n"
            "Redirect all three in the same command: "
            "`nohup <cmd> > $ISSUELOOPER_TMP/<name>.log 2>&1 < /dev/null &` (or "
            "`setsid -f <cmd> > $ISSUELOOPER_TMP/<name>.log 2>&1 < /dev/null`), then wait for it with a bounded "
            "loop (`for i in $(seq 60); do curl -sf localhost:<port>/ > /dev/null && break; sleep 1; done`). "
            "Background jobs this same command `wait`s for are fine."
        )


# The loop's own logs: the supervisor's run directory (other sessions' streams and reports)
# and the guard's records. Run 12: UX run 3 found the previous run's ux_report.md there and
# copied its score; run 4 spent its time grepping run 2's session.jsonl and guard.last.
LOOP_LOGS = re.compile(r"\.issuelooper/run\b|\bguard-runs\b|\bguard\.last\b|\bpool\.jsonl\b|\bsession\.jsonl\b")


def loop_logs_check(command: str) -> None:
    """A worker, merger or tester does not read the loop's logs; its own $LOGDIR is fine."""
    if os.environ.get("GUARD_LOOP_LOGS", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    text = command
    own = os.environ.get("LOGDIR", "").strip()
    for mine in [own, os.path.realpath(own) if own else "", "${LOGDIR}", "$LOGDIR"]:
        if mine:
            text = text.replace(mine, "")
    m = LOOP_LOGS.search(text)
    if m:
        block(
            f"This reads the loop's own logs (`{m.group(0)}`): other sessions' streams, reports and the guard's "
            "records. They are not part of your unit, and what they say (an earlier score, another session's "
            "steps) is not evidence about the app or your issue. Write your own files under `$LOGDIR`, and work "
            "from your prompt, the issue, the repository and the running app."
        )


# One `git` call: its global options (-C <dir>, -c <k=v>, --long[=v]) and its subcommand.
GIT_CALL = re.compile(
    r"""\bgit((?:\s+(?:-[Cc]\s+(?:"[^"]*"|'[^']*'|[^\s;&|]+)|--[\w-]+(?:=\S+)?))*)\s+([a-z][\w-]*)"""
)
GIT_C_DIR = re.compile(r"""-C\s+("[^"]*"|'[^']*'|[^\s;&|]+)""")
CD_DIR = re.compile(r"""(?:^|[;&|(\n])\s*cd\s+("[^"]*"|'[^']*'|[^\s;&|)]+)""")
# Subcommands that change the checkout they run in: its files, index, HEAD or branch.
CHECKOUT_WRITES = {
    "reset", "checkout", "switch", "restore", "clean", "rebase", "merge", "pull", "commit",
    "cherry-pick", "revert", "am", "apply", "rm", "mv", "stash",
}


def is_shared_dir(path: str | None) -> bool:
    """Whether a directory, as written in a command, is the shared checkout. None is a
    command with no `cd` or `-C`: it runs in the session's working directory, which is
    the shared checkout unless the host says otherwise (agy starts each command there)."""
    if path is None:
        return True
    path = path.strip("\"'")
    if path.rstrip("/") in ("$SHARED_REPO_PATH", "${SHARED_REPO_PATH}"):
        return True
    if "$" in path or not path.startswith(("/", "~")):
        return False  # another variable or a relative path: not known to be it
    shared = get_shared_repo_dir()
    return bool(shared) and os.path.realpath(os.path.expanduser(path)) == os.path.realpath(shared)


def git_checkout_check(command: str, cwd: str | None) -> None:
    """No `git stash` anywhere, and nothing that changes the shared checkout. A worker's
    `git stash push` in its worktree (the stash is one stack, refs/stash, for the shared
    checkout and every worktree) left the shared checkout conflicted; merges stopped on
    Gate 0 for ten minutes, until the worker ran `reset --hard` there."""
    for m in GIT_CALL.finditer(command):
        sub = m.group(2)
        if sub == "stash":
            block(
                "`git stash` is refused. The stash is one stack shared by the shared checkout and "
                "every worktree (refs/stash), so what one session stashes another can pop into the "
                "wrong checkout; one stash left the shared checkout conflicted and stopped every merge.\n\n"
                "Commit work in progress on your issue branch instead (`git commit -m \"WIP ...\"`, "
                "amended or squashed later). To see origin/main without your change, add a second "
                "worktree: `git -C \"$SHARED_REPO_PATH\" worktree add --detach $ISSUELOOPER_TMP/<your prefix>-base-wt origin/main`."
            )
        if sub not in CHECKOUT_WRITES:
            continue
        dirs = GIT_C_DIR.findall(m.group(1))
        if dirs:
            where = dirs[-1]
        else:
            cds = [c for c in CD_DIR.finditer(command) if c.start() < m.start()]
            where = cds[-1].group(1) if cds else cwd
        if is_shared_dir(where):
            block(
                f"`git {sub}` would change the shared checkout ($SHARED_REPO_PATH). The supervisor "
                "merges and deploys from it, and Gate 0 stops every merge while it is not clean.\n\n"
                "Run git in your own worktree: `cd <your worktree> && git ...` in the same command, "
                "or `git -C <your worktree> ...`. If the shared checkout itself is dirty or "
                "conflicted, say so on your issue and leave it to the owner."
            )


# Commands that remove, move or overwrite files, and the paths in the shared checkout
# a session may still write (the tester's screenshots, which git ignores).
FILE_WRITERS = {"rm", "mv", "cp", "ln", "unlink", "truncate", "shred"}
SHARED_WRITABLE = (".ux/", ".ux")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)
SEPARATORS = {";", "&&", "||", "|", "&", "\n", "|&", ";;", "(", ")"}
REDIRECT = re.compile(r"^\d*>>?(.*)$")


def shared_rest(path: str) -> str | None:
    """The part of `path` inside the shared checkout ('' for its root), or None when it
    is not known to be there (another variable, or a relative path)."""
    for var in ("${SHARED_REPO_PATH}", "$SHARED_REPO_PATH"):
        if path == var or path.startswith(var + "/"):
            return path[len(var):].lstrip("/")
    if not path.startswith(("/", "~")):
        return None
    full = os.path.normpath(os.path.expanduser(path))
    for root in {get_shared_repo_dir(), os.path.realpath(get_shared_repo_dir())}:
        if root and (full == root or full.startswith(root.rstrip("/") + "/")):
            return full[len(root):].lstrip("/")
    return None


def file_targets(words: list[str]) -> list[str]:
    """The paths a file command writes or removes: every operand of rm and the like,
    the last one of mv, cp and ln (ln with one operand links into the directory)."""
    cmd, args = os.path.basename(words[0]), [w for w in words[1:] if not w.startswith("-")]
    if cmd in ("mv", "cp", "ln"):
        if len(args) >= 2:
            return [args[-1]]
        return [os.path.basename(args[0])] if args and cmd == "ln" else []
    return args


def command_segments(command: str) -> list[tuple[list[str], str]]:
    """The simple commands in `command`, each as (words, the separator after it); [] when
    the quotes do not balance (the shell refuses it too)."""
    text = HEREDOC.sub("", command)  # a here-document's body is text, not commands
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()\n")
    lex.whitespace, lex.whitespace_split = " \t\r", True
    try:
        tokens = list(lex)
    except ValueError:
        return []
    # shlex joins a run of separators ("\n\n", ")\n"): split it into operators again.
    split = []
    for t in tokens + ["\n"]:
        split += re.findall(r"&&|\|\||;;|\|&|[;&|()\n]", t) if set(t) <= set(";&|()\n") else [t]
    segments, cur = [], []
    for t in split:
        if t in SEPARATORS:
            segments.append((cur, t))
            cur = []
        else:
            cur.append(t)
    return segments


def shared_file_check(command: str, cwd: str | None) -> None:
    """Nothing removes, moves or overwrites a file in the shared checkout: by a path in
    it, or by a relative path in a command that runs there (agy starts each command in
    it). The shared checkout's .env, which only the owner has, disappeared in one run and
    live checks stopped for hours; workers link it into their worktrees and remove the
    link again, and a bare `rm -f .env` run there, or after a `cd` that failed, removes
    the real file."""
    segments = command_segments(command)
    here = None if cwd is None else cwd  # where the next command runs (None: the session's cwd)
    shaky = False  # an earlier `cd` may have failed, leaving the command where it started
    strict = False  # `set -e`: a failed `cd` ends the command
    outer = []  # (here, shaky) outside each open subshell: a `cd` in `( ... )` stays in it
    for words, sep in segments:
        if sep == "(":
            outer.append((here, shaky))
        elif sep == ")" and outer:
            here, shaky = outer.pop()
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        if words[:2] in (["set", "-e"], ["set", "-eu"], ["set", "-euo"]) or (words[0] == "set" and "-e" in words[1:2]):
            strict = True
        if words[0] == "cd":
            shaky = shaky or (sep not in ("&&", "||") and not strict and is_shared_dir(here))
            here = words[1] if len(words) > 1 else "~"
            continue
        targets, operands, skip = [], [], False
        for i, w in enumerate(words):
            if skip:
                skip = False
                continue
            m = REDIRECT.match(w)
            if not m:
                operands.append(w)
                continue
            target = m.group(1) or (words[i + 1] if i + 1 < len(words) else "")
            skip = not m.group(1)
            if target and not target.startswith("&"):
                targets.append(target)
        if operands and os.path.basename(operands[0]) in FILE_WRITERS:
            targets += file_targets(operands)
        for t in targets:
            if t in ("/dev/null", "-") or t.startswith("/dev/") or not t.strip() or "\n" in t:
                continue
            rest = shared_rest(t)
            relative = not t.startswith(("/", "~", "$"))
            if rest is None and relative and is_shared_dir(here):
                rest = os.path.normpath(t)
            if rest is not None and not rest.startswith(SHARED_WRITABLE):
                block(
                    f"This would remove, move or overwrite `{t}` in the shared checkout "
                    "($SHARED_REPO_PATH). It holds what only the owner has (such as `.env`), and the "
                    "supervisor merges and deploys from it.\n\n"
                    "Name your own path: `rm -f \"$WT/.env\"` rather than `rm -f .env` (a command with "
                    "no `cd` runs in the shared checkout), or `cd \"$WT\" && ...` in the same command. "
                    "If something in the shared checkout looks wrong, say so on your issue."
                )
            if relative and shaky:
                block(
                    f"`{t}` is a relative path after a `cd` that is not checked: if the `cd` fails, "
                    "the command goes on in the shared checkout ($SHARED_REPO_PATH) and removes or "
                    "overwrites the file there.\n\n"
                    "Join it with `&&` (`cd \"$WT\" && rm -f .env`), write `cd \"$WT\" || exit 1`, "
                    "or name the full path (`rm -f \"$WT/.env\"`)."
                )


# git subcommands that can stop halfway (a conflict) and leave HEAD somewhere else:
# during a stopped rebase HEAD is the commit it is rebasing onto, origin/main itself.
STOPPABLE = {"rebase", "merge", "pull", "cherry-pick", "am", "revert"}
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
ISSUE_BRANCH = re.compile(r"^(?:refs/heads/)?issue-\d+$")


def git_words(words: list[str]) -> tuple[str | None, str | None, list[str]]:
    """(subcommand, the `-C` directory, the arguments) of a git command, or Nones."""
    if not words or os.path.basename(words[0]) != "git":
        return None, None, []
    where, i = None, 1
    while i < len(words) and words[i].startswith("-"):
        if words[i] in ("-C", "-c") and i + 1 < len(words):
            where = words[i + 1] if words[i] == "-C" else where
            i += 1
        i += 1
    return (words[i], where, words[i + 1:]) if i < len(words) else (None, where, [])


def git_out(where: str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", where, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def operation_in_progress(where: str) -> str | None:
    """'rebase' or 'merge' when the worktree at `where` is in the middle of one."""
    for name, what in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge"),
                       ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert")):
        path = git_out(where, "rev-parse", "--git-path", name)
        if path and os.path.exists(path if os.path.isabs(path) else os.path.join(where, path)):
            return what
    return None


def expand_dir(path: str, env: dict[str, str]) -> str | None:
    """A directory as a command names it, with $VARs from the command's own assignments
    or the environment; None when it does not name an existing absolute directory."""
    path = os.path.expanduser(os.path.expandvars(re.sub(
        r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: env.get(m.group(1), m.group(0)), path)))
    return path if "$" not in path and os.path.isabs(path) and os.path.isdir(path) else None


def command_dir(command: str, cwd: str | None, starts: tuple[str, ...]) -> str | None:
    """Where the first simple command whose words begin with `starts` runs: after the
    `cd`s before it (agy runs each command in the shared checkout), else cwd."""
    env: dict[str, str] = {}
    here = cwd
    for words, _ in command_segments(command):
        while words and ASSIGN.match(words[0]):
            name, value = ASSIGN.match(words[0]).groups()
            env[name] = value
            words = words[1:]
        if words[:1] == ["cd"]:
            here = words[1] if len(words) > 1 else "~"
        elif tuple(words[:len(starts)]) == starts:
            return expand_dir(here, env) if here else None
    return None


def git_push_check(command: str, cwd: str | None) -> None:
    """No push of a head that is not the branch's work. A rebase that stops on a conflict
    leaves HEAD at origin/main; pushed, the issue branch has no commits beyond main and
    GitHub closes its pull request (#100, #109 and #120 in one run: `git rebase
    origin/main` and `git push --force-with-lease` on the next line of one command; #100
    and #120 stayed closed and their issues needed new pull requests)."""
    segments = [(w, sep) for w, sep in command_segments(command) if w]
    env: dict[str, str] = {}
    here = cwd
    strict = False
    pending = None  # the stoppable git command whose failure the commands since have not ended on
    unchecked = None  # the stoppable git command a push would run after, even when it stopped
    stopped = False  # a stoppable git command comes earlier in this command
    for k, (words, sep) in enumerate(segments):
        while words and ASSIGN.match(words[0]):
            name, value = ASSIGN.match(words[0]).groups()
            env[name] = value
            words = words[1:]
        if not words:
            continue
        if words[0] == "set" and any(re.match(r"^-\w*e", w) for w in words[1:]):
            strict = True
        if words[0] == "cd":
            here = words[1] if len(words) > 1 else "~"
            continue
        sub, where, args = git_words(words)
        if sub == "push" and unchecked:
            block(
                f"This `git push` runs after `{unchecked}` even when that stops on a conflict. A "
                "stopped rebase leaves HEAD at origin/main, and pushing it gives the issue branch no "
                "commits beyond main: GitHub then closes the pull request (three times in one run).\n\n"
                f"Join them with `&&` (`{unchecked} ... && git push ...`), or push in a command of its "
                "own after checking that the rebase finished (`git status` shows no rebase in progress)."
            )
        if sub in STOPPABLE:
            stopped = True  # an --abort too: it changes the state a push would see
            if not {"--abort", "--quit"} & set(args):
                pending = f"git {sub}"
        # Its failure skips what `&&` joins to it; `|| exit` ends the command; anything else
        # goes on to the next command, the push among them.
        nxt = segments[k + 1][0][:1] if k + 1 < len(segments) else []
        if strict or (sep == "||" and nxt == ["exit"]):
            pending = None
        elif pending and sep != "&&":
            unchecked = pending
        if sub != "push" or stopped:
            continue  # after a stoppable command the state is the one it leaves: the rule above covers it
        dirname = expand_dir(where or here or get_shared_repo_dir(), env)
        if dirname is None:
            continue
        busy = operation_in_progress(dirname)
        if busy:
            block(
                f"A {busy} is in progress in `{dirname}`: HEAD is not your branch's work yet (during a "
                "stopped rebase it is origin/main). Pushing it would leave the issue branch with no "
                "commits beyond main, and GitHub closes its pull request.\n\n"
                f"Finish it first: resolve the conflicts, `git add` them, `git {busy} --continue`, check "
                f"`git status`, then push. Or `git {busy} --abort` to go back."
            )
        refspecs = [a for a in args if not a.startswith("-")][1:]
        for spec in refspecs:
            src, _, dst = spec.lstrip("+").partition(":")
            dst = dst or src
            if not src or not ISSUE_BRANCH.match(dst):
                continue
            sha = git_out(dirname, "rev-parse", "--verify", "-q", f"{src}^{{commit}}")
            if sha and git_out(dirname, "merge-base", "--is-ancestor", sha, "refs/remotes/origin/main") is not None:
                block(
                    f"`{src}` ({sha[:12]}) has no commits beyond origin/main. Pushed to `{dst}`, it "
                    "leaves the branch empty, and GitHub closes the branch's pull request.\n\n"
                    "Check `git log --oneline origin/main..HEAD` in your worktree: when a rebase "
                    "stopped, finish it (`git status`); when the work is gone, recover it from "
                    f"`origin/{dst.removeprefix('refs/heads/')}` or the reflog. Do not push until the log "
                    "shows your commits."
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
        "Write the body to $ISSUELOOPER_TMP/<role>-<N>-body.md and pass --body-file."
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
        if names is None or not (LIVE in names or ACCEPT_STAGE in names):
            continue
        data = gh_json(["gh", "issue", "view", number, "--json", "comments"], repo)
        if not isinstance(data, dict):
            continue  # fail open: an unreachable thread is not evidence either way
        # Only the tester's signed comment or the owner's own (no role marker), from an
        # account that can write (on a public repository anyone can type the line). The
        # monitor shares the loop's account and closed four needs:live issues on its own
        # reading of their PRs, with no live check run.
        bodies = [
            str(c.get("body") or "")
            for c in (data.get("comments") or [])
            if isinstance(c, dict) and c.get("authorAssociation") in ("OWNER", "MEMBER", "COLLABORATOR")
            and (str(c.get("body") or "").lstrip().startswith(ROLES["tester"])
                 or not any(m in str(c.get("body") or "") for m in ROLES.values()))
        ]
        if any(LIVE_EVIDENCE.search(b) for b in bodies):
            continue
        if LIVE not in names:
            if any(ACCEPT_EVIDENCE.search(b) for b in bodies):
                continue
            block(
                f"#{number} is at `{ACCEPT_STAGE}`: its acceptance check is owed, so it does not close yet.\n\n"
                "The supervisor closes it on the tester's `accept-verified:` line, from a tester who did not "
                "write the change. If the check cannot run, the tester says `accept-blocked:` and the issue "
                "goes to the owner. Only the tester's signed comment or the owner's own counts."
            )
        block(
            f"#{number} carries `{LIVE}` and no comment on it says the behavior was "
            "observed live, so it does not close yet.\n\n"
            "Check it in the live environment and post what you saw, with a line "
            f"reading\n\n```\n{LIVE_LINE}\n```\n\n"
            "as its own line. The tester is the role that runs live checks; issues "
            f"sitting at `{LIVE_STAGE}` are its queue.\n\n"
            "If the check cannot be run live (`live-check-blocked:`), move the issue "
            "to `stage:owner` and ask the owner rather than closing around it: an "
            "issue closed with nobody having looked is the state this label exists to "
            "prevent. Only the tester's signed comment or the owner's own counts."
        )


def monitor_live_label_check(command: str) -> None:
    """The monitor does not take `needs:live` off an issue whose live check is owed
    (stage:live) or that waits for the owner (stage:owner): taking it off and closing
    in the same command was how four issues were closed with no live check. Before
    the merge it may still correct a wrong label."""
    repo_match = REPO_FLAG.search(command)
    repo = next(g for g in repo_match.groups()[1:] if g is not None) if repo_match else ""
    numbers: list[str] = []
    seg = ISSUE_EDIT_SEGMENT.search(command)
    if seg:
        found = BARE_NUMBER.search(seg.group(1))
        if found:
            numbers.append(found.group(1))
    numbers += REST_LABELS_TARGET.findall(command) + REST_ISSUE_TARGET.findall(command)
    for number in list(dict.fromkeys(numbers))[:3]:
        names = issue_labels(number, repo) or set()
        stage = names & {LIVE_STAGE, ACCEPT_STAGE, "stage:owner"}
        if stage:
            block(
                f"#{number} is at `{sorted(stage)[0]}`: its live check is owed, and `{LIVE}` comes off only "
                "with the tester's `live-verified:` line or on the owner's word.\n\n"
                "If the tester could not run the check (credentials missing, the live server did not "
                "start, the service unreachable), move the issue to `stage:owner` with one comment "
                "saying what is missing and ask the owner. Evidence in the pull request is not a live "
                "check."
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
SWEEP_HOW = "`python3 .claude/hooks/sweep_locked.py` in your worktree"


def merge_train_on() -> bool:
    """POOL_MERGE_TRAIN: the supervisor checks the whole suite on the merged tree of a batch
    before it merges, so a PR head's affected sweep is enough for Gate 2."""
    return os.environ.get("POOL_MERGE_TRAIN", "").strip().lower() in ("1", "true", "yes", "on")


def load_sweep_record(sha: str) -> dict | None:
    """The provenance record sweep_run.py writes for a full, green sweep started
    from a clean tree at `sha`. None when there is none, or when it cannot be
    read, which for the gate means the same thing."""
    for record_dir in config.record_dirs("sweep"):
        try:
            record = json.loads((record_dir / f"{sha.lower()}.json").read_text())
        except (OSError, ValueError):
            continue
        return record if isinstance(record, dict) else None
    return None


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
    """Pre-merge gates 0-7, in the order a refusal is cheapest to act on.

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

    Gate 7 (POOL_DEVCHECK on): a PR for an issue carrying one of $DEVCHECK_LABELS, or
    one that changes $DEVCHECK_PATHS, names a passing devcheck.py record for its
    change (`- **Live check:** <sha> pass`), or a waiver. See devcheck_gate.

    Unlike the rest of the guard, these fail closed: a PR that cannot be read, a
    changed .py file that cannot be read at the head, or an error inside the gates
    refuses the merge (it is retried later; a merge cannot be undone).
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
            + ["--json", "headRefOid,baseRefOid,headRefName,body,files,closingIssuesReferences"],
            repo,
        )
        if not isinstance(data, dict) or not data.get("headRefOid"):
            # Fail closed here, unlike everywhere else in the guard (gh_json): a merge
            # that skipped its gates because GitHub was slow cannot be taken back,
            # while a merge refused for that reason is retried on the next poll.
            block(
                f"{'PR #' + pr if pr else 'The PR'} could not be read from GitHub (gh pr view failed, timed "
                "out or answered without a head), so its merge gates cannot be checked. Not merged; "
                "try again later."
            )

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

        # Gate 4 for the acceptance check: a product change does not close its issue.
        if closing:
            product = product_paths([str(f.get("path") or "") for f in (files if isinstance(files, list) else [])
                                     if isinstance(f, dict)])
            if product:
                refuse_closing_product_change(", ".join(f"#{n}" for n in closing), product)

        # New files under tests/accept/ come only from an acceptance chore.
        added = [str(f.get("path") or "") for f in (files if isinstance(files, list) else [])
                 if isinstance(f, dict) and str(f.get("changeType") or "").upper() == "ADDED"
                 and str(f.get("path") or "").startswith(ACCEPT_TESTS)]
        added = [a for a in added if a not in guard_changes(body)]  # a guard replaced for a behavior change
        if added and not is_accept_chore(pr_issues(data, body), repo):
            refuse_accept_tests(added)

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
                block(
                    f"`{p}` could not be read at {target} head `{head_sha[:7]}` (not fetched here, "
                    "even after `git fetch origin <head>`), so Gate 1 cannot check it. Not merged; "
                    "try again later."
                )
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
                    record_dir = config.record_dirs("sweep")[0]
                    block(
                        f"{target} has no sweep record for its head `{head_sha[:7]}`.\n\n"
                        f"The sweep writes `{record_dir}/<sha>.json` only for a full, green "
                        "sweep started from a clean tree, so the `green:` line in the body "
                        "was not measured at this commit.\n\n"
                        "Commit first, then run the full sweep at that commit "
                        f"({SWEEP_HOW}; its `git:` line must read `(clean)`, and it prints "
                        "`sweep record: <path>`), paste its `green:` line, and push."
                    )
                if record.get("kind") == "affected" and not merge_train_on():
                    block(
                        f"{target}'s sweep record for `{head_sha[:7]}` is from an affected sweep (only the "
                        "tests the change touches). With the merge train off, nothing checks the whole suite "
                        "before the merge, so Gate 2 needs a full sweep.\n\n"
                        f"Run `python3 .claude/hooks/sweep_locked.py --full --update-pr <N>` at the head."
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

        # Gate 7: a change a user can see was used in the real environment first.
        if devcheck_on():
            changed = [str(f.get("path") or "") for f in (files if isinstance(files, list) else [])
                       if isinstance(f, dict)]
            scope = devcheck_scope(pr_issues(data, body), changed, repo)
            if scope:
                devcheck_gate(body, scope, target, head_sha, base_sha, changed)

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


# Gate 7, the live check. The sweep runs the suite against test doubles, so a change
# can be green there and still not work when the app runs for real: a defect in how
# the app uses a real model or service passed every sweep and the post-merge check
# found it only after it shipped. A developer opens a pull request after using the
# app from the branch; devcheck.py is that step, and its record says it happened.
#
# The required form, in the PR body, one line:
#
#     - **Live check:** <sha> pass
#
# The sha is where devcheck.py ran. It is the head, or an earlier head whose
# recorded change key equals the head's (a rebase that only moved the branch).
# A `Live check:` field and its sha, in the forms devcheck.BODY_LINE rewrites.
DEVCHECK_LINE = re.compile(r"(?im)^[ \t>*-]*(?:\*\*)?Live[ \t]+check(?:\*\*)?:[*`\s]*([0-9a-fA-F]{7,40})\b")


def live_check_line(body: str) -> re.Match | None:
    """The body's last `Live check:` sha, the newest when a stale one was left above it
    (devcheck.py --update-pr leaves one; PR #75 had two and the first was read)."""
    found = list(DEVCHECK_LINE.finditer(body))
    return found[-1] if found else None


DEVCHECK_WAIVER = re.compile(
    r"(?i)\bLive\s+check\b[*`:\s]*waived\s*[-–—:]\s*"
    r"(no\s+visible\s+change|docs\s+only|loop/process|owner\s+ruling)\b"
)
DEVCHECK_HOW = (
    "Commit, then in your worktree run\n\n"
    "    python3 .claude/hooks/devcheck.py --script <your replay of the issue's steps> --update-pr <N>\n\n"
    "It starts the app from your branch in the real environment, runs the project's "
    "smoke and your script against it, and writes `- **Live check:** <sha> pass` into "
    "the body when they pass. See `.issuelooper/project.md` for how the app runs."
)
DEVCHECK_LABELS_DEFAULT = "owner-request,needs:live,ux:accepted"
ISSUE_REF = re.compile(
    r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?|refs?)\s+#(\d+)\b"
)
ISSUE_BRANCH = re.compile(r"^issue-(\d+)$")


def devcheck_on() -> bool:
    return os.environ.get("POOL_DEVCHECK", "0").strip().lower() in (
        "1",
        "on",
        "true",
        "yes",
    )


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name)
    return [
        p.strip() for p in (default if raw is None else raw).split(",") if p.strip()
    ]


def devcheck_scope(
    issues: list[str], paths: list[str], repo: str
) -> tuple[str, str] | None:
    """Why a PR needs a live check: ("label", ...) when an issue it is for carries one of
    $DEVCHECK_LABELS, ("path", ...) when it changes $DEVCHECK_PATHS; None otherwise."""
    wanted = set(_csv_env("DEVCHECK_LABELS", DEVCHECK_LABELS_DEFAULT))
    for n in list(dict.fromkeys(issues))[:5]:
        hit = (issue_labels(n, repo) or set()) & wanted
        if hit:
            return "label", f"#{n} carries `{sorted(hit)[0]}`"
    watched = _csv_env("DEVCHECK_PATHS")
    hit_paths = [p for p in paths if is_protected(p, watched)]
    if hit_paths:
        return "path", f"it changes `{hit_paths[0]}`"
    return None


def devcheck_waiver_ok(body: str, scope: tuple[str, str], target: str) -> bool:
    """True when the body waives the check with a reason that fits the scope. An issue
    that carries a label asks for the check by name, so only the owner waives it."""
    m = DEVCHECK_WAIVER.search(body)
    if not m:
        return False
    if scope[0] == "label" and not re.match(r"(?i)owner", m.group(1)):
        block(
            f"Gate 7 (live check): {target} waives the live check (`{m.group(1)}`), but "
            f"{scope[1]}: the issue itself is about what a user sees, so it is checked "
            "in the real environment before the merge. Only `waived - owner ruling` "
            "skips that, after the owner said so on the issue.\n\n" + DEVCHECK_HOW
        )
    return True


def checklist_gap(rec: dict, paths: list[str]) -> list[tuple[str, str]]:
    """The checklist rows ([accept] in config.toml) the branch's paths need that the
    record neither ran nor skipped with a reason. A record from before the checklist
    (no "rows") is not held to it."""
    if "rows" not in rec:
        return []
    no_skip = config.checklist()["no_skip"]
    done = set(rec.get("rows") or []) | {k for k in (rec.get("skips") or {}) if k not in no_skip}
    return [(rid, text) for rid, _, text in config.required_rows(paths) if rid not in done]


def refuse_checklist_gap(gap: list[tuple[str, str]], sha: str, skips: dict | None = None) -> None:
    banned = [rid for rid, _ in gap if rid in (skips or {})]
    block(
        f"Gate 7 (live check): the live check at `{sha[:12]}` skips checklist rows this change needs: "
        + ", ".join(f"`{rid}` ({text})" for rid, text in gap[:12])
        + ".\n\nThe paths it changes trigger them (`[accept]` in .issuelooper/config.toml). Most "
        "partial implementations the acceptance check found were in these rows: the narrow phone, "
        "the second language, Escape, a reload. Cover each in your script and declare it on a line "
        "of its own, `# accept-rows: <ids>`; a row that cannot apply to this change gets "
        "`# accept-skip: <id> <why>` (reviewers read the reason). Then run devcheck.py again "
        "(`--update-pr <N>`)."
        + (f"\n\nThis project does not take a skip of {', '.join(f'`{r}`' for r in banned)} (`[accept] no_skip`): "
           "the live-check rig can run it. A device the browser needs (a microphone, a camera) has a fake one: "
           "see *Live check* in .issuelooper/project.md." if banned else "")
    )


def devcheck_gate(
    body: str, scope: tuple[str, str], target: str, head_sha: str, base_sha: str, paths: list[str] | None = None
) -> None:
    import devcheck

    if devcheck_waiver_ok(body, scope, target):
        return
    m = live_check_line(body)
    if not m:
        block(
            f"Gate 7 (live check): {target} has no `Live check:` line, and {scope[1]}.\n\n"
            "A change like this is used in the real environment before its pull request, "
            "the way a developer tries a branch before asking for review: the test suite "
            "runs against test doubles and cannot see what the real services do with "
            "it.\n\n" + DEVCHECK_HOW + "\n\nA change no user can see waives it with "
            "`- **Live check:** waived - no visible change` (or `docs only`, "
            "`loop/process`, `owner ruling`)."
        )
    sha = m.group(1).lower()
    rec = devcheck.find_record(sha)
    if rec is None:
        block(
            f"Gate 7 (live check): {target} names a live check at `{sha[:12]}`, and there "
            f"is no record of it in `{devcheck.record_dir()}`.\n\ndevcheck.py writes the "
            "record itself, only for a run that started from a clean commit.\n\n"
            + DEVCHECK_HOW
        )
    if rec.get("status") != "pass":
        failed = [
            f"{s.get('name')} (exit {s.get('rc')}, log {s.get('log')})"
            for s in rec.get("steps") or []
            if s.get("rc") != 0
        ]
        block(
            f"Gate 7 (live check): the live check at `{sha[:12]}` failed: "
            f"{'; '.join(failed) or 'no step ran'}.\n\nRead the logs, fix the change, "
            "commit and check again.\n\n" + DEVCHECK_HOW
        )
    if not any(
        str(s.get("name", "")).startswith("script") for s in rec.get("steps") or []
    ):
        block(
            f"Gate 7 (live check): the live check at `{sha[:12]}` ran no script of the "
            "issue's own steps, only the project's smoke. Replay what the issue (or the "
            "report it quotes) describes, step by step from a fresh start, and pass it "
            "with --script.\n\n" + DEVCHECK_HOW
        )
    gap = checklist_gap(rec, paths or [])
    if gap:
        refuse_checklist_gap(gap, sha, rec.get("skips"))
    rec_sha = str(rec.get("sha") or "").lower()
    if head_sha and rec_sha != head_sha.lower():
        # In the guard's working directory, as merge_base and the other gates' reads.
        root = os.getcwd()
        key = devcheck.branch_key(root, base_sha, head_sha)
        if key is None:
            subprocess.run(
                ["git", "-C", root, "fetch", "-q", "origin", head_sha],
                capture_output=True,
                timeout=30,
            )
            key = devcheck.branch_key(root, base_sha, head_sha)
        if not key or key != rec.get("key"):
            block(
                f"Gate 7 (live check): the live check ran at `{rec_sha[:12]}`, and the head "
                f"`{head_sha[:12]}` changes something else: a rebase or a later commit "
                "changed what this branch does since the check.\n\nCheck the head again; "
                "`python3 .claude/hooks/devcheck.py --rerun "
                f"{rec_sha[:12]} --update-pr <N>` replays the scripts recorded then."
            )


def pr_issues(data: dict, body: str) -> list[str]:
    """The issues a PR is for: GitHub's closing references, its issue-<N> branch, and
    `Refs #N` (a needs:live PR does not close its issue)."""
    found = [
        str(r["number"])
        for r in (data.get("closingIssuesReferences") or [])
        if isinstance(r, dict) and r.get("number")
    ]
    branch = ISSUE_BRANCH.match(str(data.get("headRefName") or ""))
    if branch:
        found.append(branch.group(1))
    found += ISSUE_REF.findall(body)
    return list(dict.fromkeys(found))


def branch_paths(where: str | None = None, head: str = "HEAD", added_only: bool = False) -> list[str]:
    """Files a branch changes (or only adds) since it left origin/main, from the worktree
    `where` (the guard's own directory when None); [] when git cannot say."""
    try:
        done = subprocess.run(
            ["git", *(["-C", where] if where else []), "diff", "--name-only",
             *(["--diff-filter=A"] if added_only else []), f"origin/main...{head}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    return done.stdout.split() if done.returncode == 0 else []


PR_HEAD = re.compile(r"(?:--head|-H)[=\s]+[\"']?([\w./-]+)")


def pr_create_paths(command: str, cwd: str | None, added_only: bool = False) -> list[str]:
    """What the branch of a `gh pr create` changes: from the worktree the command runs
    in, else from `--head`'s branch on origin. The guard runs in its own directory (for
    agy the shared checkout, on main), so `cd <worktree> && gh pr create` was diffed
    there, saw nothing, and four PRs in run 8 opened without a Live check line."""
    where = command_dir(command, cwd, ("gh", "pr", "create"))
    shared = get_shared_repo_dir()
    if where and not (shared and os.path.realpath(where) == os.path.realpath(shared)):
        return branch_paths(where, added_only=added_only)
    m = PR_HEAD.search(command)
    return branch_paths(shared or None, f"origin/{m.group(1)}", added_only) if m else []


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
# $REVIEW_DIR/<sha>.json (config.record_dirs). pass, skipped (no production file)
# and override (the owner's) let the merge go ahead; anything else does not.
# REVIEW_GATE=off (or 0) skips the gate.
REVIEW_ALLOWS = {"pass", "skipped", "override"}  # review_gate.ALLOWS


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
    record = None
    for record_dir in config.record_dirs("review"):
        try:
            record = json.loads((record_dir / f"{head_sha.lower()}.json").read_text())
            break
        except (OSError, ValueError):
            continue
    verdict = record.get("verdict") if isinstance(record, dict) else None
    if verdict in REVIEW_ALLOWS:
        return
    n = pr or "<N>"
    if verdict == "owner":
        block(
            f"{target} head `{head_sha[:7]}` waits for the owner: {str(record.get('reason') or '')[:600]}\n\n"
            "review_gate.py has moved the issue to `stage:owner`. Do not merge; the owner decides "
            "(a `review-override: <sha>` comment of theirs lets this head through)."
        )
    if verdict == "error":
        block(
            f"{target} head `{head_sha[:7]}` could not be reviewed (the review failed: "
            f"{str(record.get('reason') or '')[:300]}). Not merged; run review_gate.py again later."
        )
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


# A worker's conversation resumed to resolve its own PR's conflict (ccpool.py's author
# resolve): it works like a merger on that one PR, under the worker role.
RESOLVE_PR_ENV, RESOLVE_BRANCH_ENV = "RESOLVE_PR", "RESOLVE_BRANCH"
PR_WRITE_SEGMENT = re.compile(r"\bgh\s+pr\s+(?:comment|edit|close|ready|reopen|review)\b([^|;&\n]*)")


def resolve_scope_check(command: str) -> None:
    """The author resolving PR $RESOLVE_PR pushes only its branch, writes only to that PR,
    and opens none: what the step needs and nothing more."""
    pr = os.environ.get(RESOLVE_PR_ENV, "").strip().lstrip("#")
    branch = os.environ.get(RESOLVE_BRANCH_ENV, "").strip()
    if not pr:
        return
    why = (f"This session resumed your conversation to resolve pull request #{pr}'s conflict with main, and "
           "nothing else: rebase, resolve, sweep, push its branch, stop.\n\n")
    if PR_CREATE.search(command):
        block(why + "It does not open a pull request: push the rebased head to the existing branch "
              f"(`git push --force-with-lease origin HEAD:{branch or '<its branch>'}`).")
    for m in PR_WRITE_SEGMENT.finditer(command):
        found = BARE_NUMBER.search(m.group(1))
        if found and found.group(1) != pr:
            block(why + f"It writes only to PR #{pr}, and this names #{found.group(1)}.")
    for words, _ in command_segments(command):
        sub, _, args = git_words(words)
        if sub != "push" or not branch:
            continue
        for spec in [a for a in args if not a.startswith("-")][1:]:
            dst = spec.lstrip("+").partition(":")[2] or spec.lstrip("+")
            if dst.removeprefix("refs/heads/") not in (branch, "HEAD"):
                block(why + f"It pushes only `{branch}`, and this pushes `{dst}`.")


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


def check_pr_create(command: str, cwd: str | None = None) -> None:
    """Pre-creation gates: draft carve-out, Sweep SHA, and Fail-before verification.

    Draft carve-out: If --draft or -d is present, allow without Gate 2/3 checks.
    Gate 2: Must contain `- **Sweep SHA:** <HEAD sha>`. Refuse if missing.
    Gate 3: Must contain `- **Fail-before:** tests/<path>/test_<name>.py <N> -> <M>`
    or one of the 4 valid waivers. Refuse if missing.
    Gate 7: a PR in the live check's scope names a passing devcheck record or a waiver.
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

    # New files under tests/accept/ come only from an acceptance chore.
    added = [p for p in pr_create_paths(command, cwd, added_only=True)
             if p.startswith(ACCEPT_TESTS) and p not in guard_changes(body)]
    if added and not is_accept_chore([n for n in [claimed_issue()] if n] + ISSUE_REF.findall(body), ""):
        refuse_accept_tests(added)

    # Gate 4 for the acceptance check, before the PR exists rather than at its merge.
    closing_refs = CLOSING_REF.findall(body)
    if closing_refs and accept_on():
        product = product_paths(pr_create_paths(command, cwd))
        if product:
            refuse_closing_product_change(", ".join(f"#{n}" for n in dict.fromkeys(closing_refs)), product)

    # Gate 7, as far as it can be told before the PR exists: the issue's labels and
    # the files the branch changes. The merge gate checks the record against the head.
    if devcheck_on():
        m_live = live_check_line(body)
        if m_live and not DEVCHECK_WAIVER.search(body):
            import devcheck

            rec = devcheck.find_record(m_live.group(1))
            if rec is None or rec.get("status") != "pass":
                block(
                    f"Gate 7 (live check): the body names a live check at `{m_live.group(1)[:12]}`, "
                    "and there is no passing record of it.\n\n" + DEVCHECK_HOW
                )
            gap = checklist_gap(rec, pr_create_paths(command, cwd))
            if gap:
                refuse_checklist_gap(gap, m_live.group(1), rec.get("skips"))
        elif not m_live:
            issues = [n for n in [claimed_issue()] if n] + ISSUE_REF.findall(body)
            scope = devcheck_scope(issues, pr_create_paths(command, cwd), "")
            if scope and not devcheck_waiver_ok(body, scope, "This PR"):
                block(
                    f"Gate 7 (live check): the body has no `Live check:` line, and {scope[1]}.\n\n"
                    + DEVCHECK_HOW
                    + "\n\nA change no user can see waives it with "
                    "`- **Live check:** waived - no visible change`."
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
    if role:
        background_check(command)
    if role in ("worker", "merger", "tester"):
        loop_logs_check(command)
    if role in ROLES:
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        git_checkout_check(command, cwd or None)
        shared_file_check(command, cwd or None)
        git_push_check(command, cwd or None)

    if role == "worker":
        worker_git_check(command)
        resolve_scope_check(command)

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
        try:
            check_pr_merge(command)
        except Exception as exc:  # noqa: BLE001
            # The guard fails open on its own bugs (see __main__), but not on a merge:
            # an error inside the gates would merge a PR nobody checked.
            block(f"The merge gates failed with an error ({exc!r:.300}), so nothing was checked. Not merged.")

    if role == "monitor" and LIVE in removed:
        monitor_live_label_check(command)

    if CLOSES.search(command) and LIVE not in removed:
        check_issue_close(command)

    if PR_CREATE.search(command):
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        check_pr_create(command, cwd or None)

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
