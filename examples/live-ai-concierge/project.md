# Project notes for the issue loop

## The app

The Live AI Concierge: FastAPI and vanilla JS in `src/concierge/` (`main.py`,
`scripted.py`, `static/`), run with `uv`. The spec is `spec/REQUIREMENTS.md` and
`spec/ACCEPTANCE.md`; `spec/ui/` shows the target look. Read `AGENTS.md`: `uv add`,
never pip; the `data-testid` hooks of REQUIREMENTS §12; never hard-code values
from `spec/fixtures/script.json` (the app is evaluated with another script).

## Worktree setup

`uv sync --frozen -q`

## Tests

- Anything on the page: `tests/e2e/test_issue_<N>.py` (pytest-playwright; the
  fixtures `server_url` and `page` in `tests/e2e/conftest.py` start the app in
  scripted mode on a free port). Everything else: `tests/unit/test_issue_<N>.py`.
- Never append to `test_page.py`, `test_shell.py` or `test_app.py`.
- One file: `uv run pytest -q <file>`. Lint: `uv run ruff check src tests`.

## Run the app on a port

Port 8000 is the owner's: never run `make run` or `make scripted`.

- Scripted (no credentials):
  `GOOGLE_MAPS_API_KEY= SESSIONS_DB=/tmp/<name>.db CONCIERGE_SCRIPT=spec/fixtures/script.json setsid uv run concierge --port <port> > /tmp/<name>-server.log 2>&1 < /dev/null &`
- Ready when `curl -sf localhost:<port>/` answers.

## Look at a change on screen

A short Playwright script (`uv run python <script>`), screenshots at 1280×800
and 390×844, `timezone_id="Asia/Tokyo"`.

## Spec check

The spec is `spec/REQUIREMENTS.md` (requirements numbered `R-XXX-N`) and
`spec/ACCEPTANCE.md`. Worth checking: phone layouts, Japanese, settings, error
and empty states, and the cards' maps.

## UX run journeys

- Japanese: switch the UI language and ask in Japanese.
- A long conversation: scroll up while a reply is still arriving.
- Live journey (run twice): ask for something with a long spoken answer; about
  2 seconds into the reply send a short new question (a barge-in); then one more
  ordinary question. The app must stop the first answer, answer both, and never
  show "Not delivered", a stuck status or a dead input.

## Live mode

Acceptance that needs the real Gemini Live model (live answers, real tool
calls). Start the app without `CONCIERGE_SCRIPT`, with credentials through a
symlink into the tree or worktree (`ln -sf "$SHARED_REPO_PATH/.env" .env`;
a worker removes it before committing):
`SESSIONS_DB=/tmp/<name>-live.db setsid uv run concierge --port <port> > /tmp/<name>-live.log 2>&1 < /dev/null &`.
A worker checking a `needs:live` change drives it in English and Japanese when
the issue concerns language.

## Files many changes touch

`src/concierge/static/*`, `src/concierge/main.py`.

## Secrets

`.env` holds real credentials. `GET /api/config` answers with the browser Maps
key: never print it.
