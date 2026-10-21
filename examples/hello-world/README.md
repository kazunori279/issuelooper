# Sample: hello world (textstats)

A first run of issuelooper in 15-30 minutes. The app is `textstats`, a small Python
command-line tool that counts words and characters, with a deliberate bug. Six
issues ask the loop to fix two bugs and add four features; you watch it work through
them in parallel (15-30 minutes), then talk to the monitor and file an issue of
your own (about 15 more).

## Before you start

The [requirements](../../README.md#requirements): `gh` signed in, Python 3.11+,
`agy` signed in, herdr, and [uv](https://docs.astral.sh/uv/). The agents run on
`gemini-4-argon-high`; to use another model, edit `model` in
`issuelooper/config.toml` before you create the repository.

## 1. Create the repository (2 minutes)

```bash
git clone https://github.com/kazunori279/issuelooper
issuelooper/examples/create.sh hello-world <you>/textstats-demo
```

This makes `./textstats-demo`, a private GitHub repository with issuelooper
installed and these issues at `stage:triage`:

| # | Issue | Priority |
|---|---|---|
| 1 | Word count is wrong with several spaces or newlines between words | P0 |
| 2 | Show the number of lines | P1 |
| 3 | Add --top N: the most frequent words | P2 |
| 4 | Add --json to print the counts as JSON (blocked by #2) | P2 |
| 5 | A UTF-8 byte order mark is counted as a character | P1 |
| 6 | Count sentences | P2 |

## 2. Start the loop

```bash
cd textstats-demo
herdr --session textstats-demo
```

In the herdr pane:

```bash
.claude/hooks/issuelooper start
```

A tab named `issuelooper` opens with the monitor (left), an aide (right) and the
status view (bottom left), and the dashboard is at <http://127.0.0.1:8780/>.

## 3. Watch it work (about 20 minutes)

- The supervisor starts four workers at once (`[pool] max_sessions = 4`), one per
  issue, in priority order. #4 waits until #2 has merged.
- Each worker writes a failing test first (`tests/unit/test_issue_<N>.py`), fixes the
  code, runs `make verify`, and opens a pull request that records the test counts
  before and after.
- The reviewer, a second model call, reads each diff against
  `.issuelooper/review_rules.md` (counts must come from the input; standard library
  only).
- The supervisor merges a pull request when the gates pass. All six issues touch
  `cli.py`, so later pull requests conflict with earlier merges: the supervisor
  rebases the simple ones itself and starts a merger session for the rest. When a
  rebase turns a test red (an earlier issue's test that expects the old output, say),
  the merger hands the pull request back and a new worker finishes it.
- The monitor gets a `Pool update:` message after each change and keeps the queue
  in order.

Follow it on the dashboard, in the status pane, or on GitHub (issues move from
`stage:triage` to `stage:doing` to `stage:pr`, then close).

## 4. Take part (about 15 minutes)

- **Ask the monitor**, in its pane: "What has merged so far, and what is still
  open?"
- **File an issue through the aide**, in its pane: "File an issue: add
  `--min-length N` to `--top`, so words shorter than N letters are skipped." The
  aide files it at `stage:triage` with a priority, so a worker picks it up within
  seconds; it ships in about 15 minutes.
- **Try the result**: `git pull && uv run textstats README.md --top 3`.

## 5. Stop

```bash
.claude/hooks/issuelooper stop --all
```

`start` picks up where it left off. To remove the sample:
`gh repo delete <you>/textstats-demo` and delete the directory.
