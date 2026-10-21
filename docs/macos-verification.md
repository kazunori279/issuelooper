# macOS and Claude Code validation

Validation for [issue #1](https://github.com/kazunori279/issuelooper/issues/1),
performed on 2026-10-21 with macOS 26.7 (arm64), Python 3.13.7 and Claude Code
2.1.286 using its configured Vertex provider and default model.

## Fixes found during validation

- The installer compared a logical checkout path with Git's physical path.
  `pwd -P` lets an install through `/tmp` work when Git reports `/private/tmp`.
- The supervisor depended on the external `setsid` command to detach. On macOS
  it stayed in the launcher's session and disappeared after the launcher exited.
  It now uses Python's `start_new_session=True` on both platforms.
- Long macOS temporary paths exceeded the Unix socket address limit. The broker,
  owner client and supervisor now share a canonical, short socket address in a
  private per-user directory when necessary. The usual `broker.sock` remains a
  discoverable symlink. Short addresses continue to live in the run directory.
- Server instructions and the concierge deployment example now use `nohup`
  and `lsof` listener selection instead of Linux-only `setsid` and `fuser` syntax.
- Both samples' lockfiles referenced a private package proxy. They were regenerated
  against public PyPI without changing any package versions.
- Tests now compare physical paths and isolate the missing-CLI check from a
  developer's installed Claude Code. Regression tests cover symlinked installs,
  long socket paths, socket aliases and detached supervisor sessions.

## Live checks

- A real Claude print-mode invocation emitted init, assistant and result events.
  `--resume` continued the same session and recalled the preceding response.
- The headless broker accepted multiple messages and received results. Restarting
  the loop resumed the monitor's conversation.
- The reviewer returned parsable JSON with `--max-turns 1`. With `--verbose`, its
  response was a list of events, which `read_claude` also parsed successfully.
- A real Bash tool call triggered the installed PreToolUse hook. An unsigned
  `gh issue comment` was denied. The test used a fake `gh` executable; it never ran
  and no comment was sent by this check.
- The pool rendered real tool calls/results, created worktrees under `/tmp`, ran
  sweeps from their `/private/tmp` paths, and read the resulting sweep records.

## End-to-end result

A private [validation repository](https://github.com/kazunori279/issuelooper-mac-e2e-20261021)
used the hello-world app, `[agents] cli = "claude"`, `[monitor] host = "broker"`
and `[pool] max_sessions = 3`, with review enabled and live/UX/spec testing disabled.
The monitor triaged two issues and made the second depend on the first.

The first attempt exposed the supervisor detachment and private package proxy
problems above. After fixing the framework and lockfile, the loop restarted at
05:33:04 UTC. It recovered the previous worker's commit, reproduced the failing
tests, swept each PR head, passed the real Claude reviewer and automatically merged:

| Issue | PR | Sweep | Merged (UTC) |
|---|---|---|---|
| Repeated whitespace counts | [#3](https://github.com/kazunori279/issuelooper-mac-e2e-20261021/pull/3) | 8 passed | 05:34:15 |
| Initial UTF-8 byte order mark | [#4](https://github.com/kazunori279/issuelooper-mac-e2e-20261021/pull/4) | 15 passed | 05:35:39 |

No worker, branch, PR or issue needed manual intervention during this final run.
Both issues closed, the queue became empty and the validation loop was stopped.
The final merge commit is `b776b247d54f1dc6a6b55097b7a4bde6e095cda2`; a fresh
`make verify` on that merged main passed all 15 tests.

## Automated checks

All 14 framework test files passed on macOS after the fixes. Run the suite from
the repository root:

```bash
for t in template/.claude/hooks/*_test.py; do python3 "$t" || exit; done
```

Both sample environments installed with `uv sync --frozen`. `make verify` passed
for hello-world (2 tests) and concierge (lint, 21 unit tests, 23 browser tests).

## Limits of the evidence

Forced stall recovery, wrap-up timing and Claude quota recovery are covered by
fake-CLI tests; a real provider quota or stalled Claude process was not induced.
Per-role CLI selection is covered by tests. An `agy` worker override alongside
the Claude monitor was smoke-tested with agy 1.4.3, but its result included a
provider quota error; a complete mixed-CLI pool run remains unverified. The macOS
live run used the headless broker, not a real herdr workspace.
