# Project notes for the issue loop

## The app

`textstats`: a command-line tool that counts words, lines and characters in
text. Code in `src/textstats/` (`core.py` holds the counting, `cli.py` the
command line). The issues are the spec; `README.md` says how to run it.
Standard library only: never add a runtime dependency.

## Worktree setup

`uv sync --frozen -q`

## Tests

- New tests: `tests/unit/test_issue_<N>.py`. Test the command line through
  `textstats.cli.main([...])` with `capsys`, and the counting through `core.py`.
- One file: `uv run pytest -q <file>`.
- The whole suite: `make verify`.

## Run the app on a port

Not applicable: it is a command-line tool. Run it with
`uv run textstats <file>` or pipe text into `uv run textstats`.

## Files many changes touch

`src/textstats/cli.py` and `src/textstats/core.py`: keep hunks small, add new
functions rather than rewriting existing ones, and never reformat.
