# Project notes for the issue loop

Every agent session reads this file after the `issuelooper-shared` skill. It holds
what the skills cannot know about this repository. Replace each section's
placeholder with the facts for your project and delete the sections that do not
apply. Keep it short: commands and paths, not prose.

## The app

<!-- One paragraph: what the app is, the main directories, where the spec lives. -->

## Worktree setup

<!-- What a session runs right after `git worktree add`, from the worktree root.
     Example: `uv sync --frozen -q` -->

## Tests

<!-- Where a new issue's test goes and how to run one file. The merge gates
     expect pytest files (`tests/<suite>/test_issue_<N>.py`).
     Example:
     - Anything a user sees in the browser: `tests/e2e/test_issue_<N>.py`
       (fixtures in `tests/e2e/conftest.py`). Everything else:
       `tests/unit/test_issue_<N>.py`.
     - One file: `uv run pytest -q <file>`
     - Lint: `uv run ruff check src tests` -->

## Run the app on a port

<!-- How a session starts its own copy of the app on a port it was given, in the
     background, with any database or scratch files under /tmp. Say which mode
     needs no credentials. Never name a port the owner uses.
     Example:
     `APP_DB=/tmp/<name>.db setsid uv run myapp --port <port> > /tmp/<name>-server.log 2>&1 < /dev/null &`
     Ready when `curl -sf localhost:<port>/` answers. -->

## Look at a change on screen

<!-- Optional. How to drive the app and take screenshots (for example a short
     Playwright script, `uv run python <script>`), and the viewports to check. -->

## Spec check

<!-- Optional, when `[spec] enabled = true`. Where the spec and its acceptance
     criteria are, how requirements are numbered, and the areas most worth
     checking. -->

## UX run journeys

<!-- Optional, when `[ux] enabled = true`. Journeys the tester must cover beyond
     the standard ones (first visit on desktop and phone, every language, a long
     session, interruptions, shipped owner requests), and a live journey to run
     twice when live checks are on. -->

## Live mode

<!-- Only when `[live] enabled = true`. What acceptance needs the live
     environment (a real model, a real device, a third-party API), how a tester or
     a worker starts the app against it on a port of its own, and where the
     credentials come from. -->

## Files many changes touch

<!-- Optional. Shared files where parallel workers should keep hunks small and
     never reformat. -->

## Secrets

<!-- Where credentials live (for example `.env`, never committed), and any
     endpoint whose response contains one (also list it under
     `[guard] secret_urls` in config.toml). -->
