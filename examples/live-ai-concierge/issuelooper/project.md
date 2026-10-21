# Project notes for the issue loop

## The app

The Live AI Concierge: FastAPI and vanilla JS in `src/concierge/` (`main.py`,
`scripted.py`, `static/`), run with `uv`. The spec is `spec/REQUIREMENTS.md` and
`spec/ACCEPTANCE.md`; `spec/ui/` shows the target look. Read `AGENTS.md`: `uv add`,
never pip; the `data-testid` hooks of REQUIREMENTS §12; never hard-code values
from `spec/fixtures/script.json` (the UX runs use another script, `ux/ux_script.json`).

`spec/` (the product decision) and `ux/` (the UX checks) are the owner's: merge
gate 6 refuses a pull request that changes anything under them. A test that needs other scripted turns starts the app with its own
script under `tests/` (`CONCIERGE_SCRIPT=tests/...`); a spec that looks wrong is a
comment on the issue for the owner.

## Worktree setup

`uv sync --frozen -q`

## Tests

- Anything on the page: `tests/e2e/test_issue_<N>.py` (pytest-playwright; the
  fixtures `server_url` and `page` in `tests/e2e/conftest.py` start the app in
  scripted mode on a free port). Everything else: `tests/unit/test_issue_<N>.py`.
- Never append to `test_page.py`, `test_shell.py` or `test_app.py`.
- One file: `uv run pytest -q <file>`. Lint: `uv run ruff check src tests`.

## Run the app on a port

Ports 8000 and 8001 are the owner's (`.issuelooper/deploy.sh` serves main there):
never run `make run` or `make scripted`.

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

The UX checks are in `ux/`; start every UX run from them rather than writing
journeys from scratch:

1. **Scripted journeys.** From the extracted tree (no server of your own needed;
   it starts the app itself with `ux/ux_script.json`, about 4 minutes):
   `uv run python ux/journeys.py --port "$TST_PORT" --out "$SHARED_REPO_PATH/.ux/<sha12>"`.
   Eight journeys: a first-time visitor on a desktop, a phone user, a heavy user
   (pictures, video, interrupting a reply, reload, scrolling up while replies
   arrive), settings and memory, a Japanese speaker, a saved location on a phone,
   two profiles, and a server restart. `steps.json` gives each step's status pill,
   page errors and, when a control could not be used, why. Make the contact sheets
   with `--captions <out>/captions.json`.
2. **Live journey**, twice, each into its own directory (`live-1/`, `live-2/`),
   after `ln -sf "$SHARED_REPO_PATH/.env" .env` in the tree:
   `uv run python ux/journeys.py --live --out "$SHARED_REPO_PATH/.ux/<sha12>/live-1"`.
   It asks three questions, interrupts a long story (barge-in), asks about "near
   here" after setting a location, and asks twice in Japanese. The app must stop
   the first answer, answer every question, and never show "Not delivered", a
   stuck status or a dead input. A step that fails in one of the two runs is a bug.
3. **Your own probes.** Then up to six actions of your own, in a short Playwright
   script, on whatever looked wrong or untested in the journeys, and every
   `owner-request` that shipped, with a screenshot after each.

Judge every screenshot by `ux/rubric.md` (what counts as friction, and what does
not: scripted mode's canned reply and placeholder data). Its severities set the
priority: blocker P0, major P1, minor P2. A step that could not be done because a
feature is missing is a finding only when no open issue covers that feature.

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
