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
  fixtures `server_url` and `page` in `tests/conftest.py` start the app in
  scripted mode on a free port). Everything else: `tests/unit/test_issue_<N>.py`.
- Never append to `test_page.py`, `test_shell.py` or `test_app.py`.
- The server keeps conversations (REQUIREMENTS §6), and every test on `server_url` shares
  them. A test that needs no earlier conversation (an empty feed after a reload, a first
  visit, "Clear conversation" then reload) uses `fresh_server_url`: an app of its own with
  an empty sessions database: that is the per-test user (the app has no other way to give a
  test its own conversations). Acceptance guards (tests/accept/) do the same, never an
  endpoint only tests call or a patched `page.goto`/`page.reload` to reset state.
- One file: `uv run pytest -q <file>`. Lint: `uv run ruff check src tests`.

## Run the app on a port

Ports 8000 and 8001 are the owner's (`.issuelooper/deploy.sh` serves main there):
never run `make run` or `make scripted`.

- Scripted (no credentials):
  `GOOGLE_MAPS_API_KEY= SESSIONS_DB=$ISSUELOOPER_TMP/<name>.db CONCIERGE_SCRIPT=spec/fixtures/script.json nohup uv run concierge --port <port> > $ISSUELOOPER_TMP/<name>-server.log 2>&1 < /dev/null &`
- Ready when `curl -sf localhost:<port>/` answers.

## Look at a change on screen

A short Playwright script (`uv run python <script>`), screenshots at 1280×800
and 390×844, `timezone_id="Asia/Tokyo"`.
Launch Chromium with `env={**os.environ, "PULSE_SERVER": "unix:/nonexistent"}`:
through a real sound server, playback can stall and the status stays `speaking`.

## Live check

`[devcheck]` is on: before the PR, `python3 .claude/hooks/devcheck.py --script <path>`
starts the app from your worktree in live mode (the real Gemini Live model, with
the shared checkout's `.env` linked in) on `$DEVCHECK_URL` and runs your script with
`uv run python`. Write it like the screenshot script above, against
`os.environ["DEVCHECK_URL"]`, screenshots into `os.environ["DEVCHECK_OUT"]`, and
make it exit non-zero when a step does not show what it should (a reply that
never comes, a stuck status, a card for the wrong place). Live answers vary:
assert what must hold (the right city, a card of the right kind, a reply in the
asked language), not exact wording. Drive the issue's steps in each language the
issue concerns, and the steps around them that depend on the change (after
setting a location, ask for something "near here").

Voice works in a live check: Chromium plays a recording as the microphone. Launch it
the way `ux/journeys.py --live` does, with `--use-fake-ui-for-media-stream`,
`--use-fake-device-for-media-stream` and
`--use-file-for-fake-audio-capture=<absolute path to ux/barge_in.wav>%noloop`, and the
`microphone` permission in the browser context; then tap the mic (`composer-mic`): the
model hears "Sorry, stop. What time is it in Tokyo?", once. That covers the
`live:voice-and-typed` row, and a missing microphone is not a reason to skip it (the
config's `[accept] no_skip` refuses that skip).

## Spec check

The spec is `spec/REQUIREMENTS.md` (requirements numbered `R-XXX-N`) and
`spec/ACCEPTANCE.md` (cases `A-NN`). Cite only IDs that `grep -n` finds there, in a
UX run too (R-UI-3, R-CARD-5 and R-MAPS-2 do not exist). Worth checking: phone layouts, Japanese, settings, error
and empty states, and the cards' maps.

## UX run journeys

The UX checks are in `ux/`, and `[ux] journeys` in config.toml names
`ux/journeys.py`: start every UX run from them, and never write a journeys file of
your own:

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
   after `ln -sf "$SHARED_REPO_PATH/.env" "$T/.env"` (the tree's path, written out):
   `uv run python ux/journeys.py --live --out "$SHARED_REPO_PATH/.ux/<sha12>/live-1"`.
   This is the live journey; every UX run runs it twice. It asks three questions,
   interrupts a long story by text and then by voice (the fake microphone says
   "Sorry, stop. What time is it in Tokyo?"), reloads in the middle of a turn and
   asks again, sets the location to Otaru and asks "near here" in English, asks
   for several cards in one turn, and asks in Japanese, "near here" included
   (この近く). A step fails when it could not be done, or when its screenshot shows
   an answer that ignores the saved location, a reply under another question, a
   first answer that did not stop, a status stuck on thinking, "Not delivered" or a
   dead input. A step that fails in either run is a bug and caps the score at 70.
3. **Your own probes.** Then up to six actions of your own, in a short Playwright
   script at `$ISSUELOOPER_TMP/tst-$TST_PORT-probes.py` (scratch, not a journeys file), on
   whatever looked wrong or untested in the journeys, and every
   `owner-request` that shipped, with a screenshot after each.

Judge every screenshot by `ux/rubric.md` (what counts as friction, and what does
not: scripted mode's canned reply and placeholder data). Its severities set the
priority: blocker P0, major P1, minor P2. A step that could not be done because a
feature is missing is a finding only when no open issue covers that feature.

## Live mode

Acceptance that needs the real Gemini Live model (live answers, real tool
calls). Start the app without `CONCIERGE_SCRIPT`, with credentials through a
symlink into the tree or worktree, naming its path in full:
`ln -sf "$SHARED_REPO_PATH/.env" "$WT/.env"`, and before committing
`rm -f "$WT/.env"`. Never a bare `rm -f .env` or `ln -sf ... .env`: a command with no
`cd` (or after a `cd` that failed) runs in the shared checkout, and there it removes
the owner's only copy of the credentials; the guard refuses it. Start the app:
`SESSIONS_DB=$ISSUELOOPER_TMP/<name>-live.db nohup uv run concierge --port <port> > $ISSUELOOPER_TMP/<name>-live.log 2>&1 < /dev/null &`.
A worker checking a `needs:live` change drives it in English and Japanese when
the issue concerns language.

## Files many changes touch

`src/concierge/static/*`, `src/concierge/main.py`.

## Areas and explorers

config.toml names five areas (live, settings, cards, shell, tools); each issue is
worked in its area's conversation, and `main.py` (live) and `app.js`, `index.html`,
`style.css` (shell) are hot files, one session at a time. An issue you file that
lies in one area carries `area:<name>`. Explorers each take one focus from
`[explore] focuses` and judge it like a UX run, by `ux/rubric.md`.

## Secrets

`.env` holds real credentials. `GET /api/config` answers with the browser Maps
key: never print it.
