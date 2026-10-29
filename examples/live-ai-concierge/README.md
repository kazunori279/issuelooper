# Sample: Live AI Concierge

The app issuelooper was developed on, from its first slice to the
full spec, in about six hours. A person talks to an AI concierge by voice or by
typing; it answers out loud, and when it uses a tool (weather, web search, places,
routes, place details, images, videos) the result appears as a card next to the
conversation. Python with Google ADK and the Gemini Live API, plain JavaScript in
the page.

You start from a working first slice (the layout, scripted mode, the weather card)
and 54 seed issues. The loop builds the rest: live mode on the real model, the other
cards, settings, history, profiles and memory. Its testers check live issues against
the real model, walk the app as users would, one run after another, and check it against
the spec when the workers run out of issues, and what they file becomes the next
work.

## What is in the sample

| Path | What it is |
|---|---|
| `app/` | The starting app. `spec/` holds the requirements, acceptance criteria, screenshots of the target look and a sample script; `ux/` holds the UX checks (below). |
| `backlog.md` | The 54 seed issues: P0 #1-#14 (the minimum viable product), P1 #15-#31, P2 #32-#54, with their dependencies. 11 are `needs:live`. |
| `issuelooper/` | `config.toml` (every role on `gemini-4-argon-high`, 16 sessions, live checks, UX runs and spec checks on), `project.md`, `review_rules.md`, `monitor_kickoff.md`, and `deploy.sh`, which serves `main` for you to try. |
| `after_create.sh` | Installs the environment and the browser, runs `make verify`, writes `.env`. |

### The UX checks

The loop's own tester walks the app in its UX runs: scripted user journeys, a few
actions of its own, and a harsh review of every screenshot. Its material is in the app
repository:

- `ux/journeys.py`: the journeys as a Playwright script. Eight scripted journeys (a
  first-time visitor on a desktop, a phone user, a heavy user, settings and memory,
  a Japanese speaker, a saved location on a phone, two profiles, a server restart)
  and, with `--live`, a live session on the real model with a barge-in. It starts
  the app itself, takes a screenshot after every step and records the status pill,
  page errors and any control it could not use. About 4 minutes.
- `ux/ux_script.json`: the script the journeys run on, with other places, numbers and
  languages than `spec/fixtures/script.json`, so code that hard-codes the sample
  script shows up as wrong.
- `ux/rubric.md`: what counts as friction, how severe it is (blocker P0, major P1,
  minor P2), and what does not count.

`.issuelooper/project.md` tells the tester to run the journeys, then probe up to six
things of its own, and to judge by the rubric. Findings go straight into the queue
as `UX: ...` issues; no one has to accept them first.

## Before you start

- The [requirements](../../README.md#requirements): `gh` signed in, Python 3.11+,
  `agy` signed in with access to `gemini-4-argon-high`, herdr, and
  [uv](https://docs.astral.sh/uv/). Linux or macOS.
- A Google Cloud project with the Vertex AI API enabled, and Application Default
  Credentials on this machine (`gcloud auth application-default login`). Live mode
  uses `gemini-3.8-live` in `us-central1` (`LIVE_MODEL` in `.env`).
- Optional: a Google Maps JavaScript API key whose website restriction allows
  `http://localhost:*/*` (any port: the testers run the app on ports of their own),
  for the maps in the places, route and place cards. Without one the cards show lists.
- Ports 8000 and 8001 free: `deploy.sh` serves the app there for you.
- Cost: 16 agent sessions for six hours use a lot of model tokens; check your model's
  prices and quota before a long run.
- Model quota: 16 sessions on one model reach its tokens-per-minute quota now and
  then (52 times in our six-hour run); the supervisor waits each one out, but a
  higher quota makes the run faster.

## 1. Create the repository (10 minutes)

```bash
git clone https://github.com/kazunori279/issuelooper
issuelooper/examples/create.sh live-ai-concierge <you>/concierge
```

Then fill in `concierge/.env` (never committed): `GOOGLE_CLOUD_PROJECT`, and
`GOOGLE_MAPS_API_KEY` if you have one. The testers and live-mode workers link it
into their trees; without it, the `needs:live` issues cannot be verified.

## 2. Start the loop

```bash
cd concierge
herdr --session concierge     # the session name create.sh printed last (your repository's name)
```

In the herdr pane:

```bash
.claude/hooks/issuelooper start
```

The monitor, an aide and the status view open in a tab named `issuelooper`; the
dashboard is at <http://127.0.0.1:8780/>. The supervisor starts workers on the P0
issues at once.

## 3. What happens

Times and scores depend on the model and on the settings. In run 14 of this sample (six
hours, every role on `gemini-3.8-flash-high`, acceptance in `"gate"` mode with author
acceptance, an owner who only answered questions), most seed issues closed, and closes
trailed merges by about an hour, because each product change closed only after its
acceptance check.

This config runs `[accept] mode = "after"`: each pull request says `Fixes #N` and its merge
closes the issue, and a tester who did not write the change then checks
the seeds, owner requests, P0/P1, `needs:live` issues and a sample of the rest on the
deployed build, reopening a failure. The dashboard shows that failure rate.

- **The first hour:** 16 workers take the issues by priority. Live mode is built in stages
  (typed text through ADK in #1, then the model's voice, the microphone, voice input, and
  the live tools for the cards), and the other live issues wait for #1.
- **UX runs from the start:** each run walks the deployed app and files at most five
  findings. Features whose seed issues are still open count as missing scope, not as
  defects, so the early scores rate what is built.
- **When the workers run out of issues:** explorers, extra UX runs and spec checks, one
  requirement at a time, make the next work.

Every merge is deployed: <http://localhost:8000/> runs `main` in scripted mode (try
the starter chips), and <http://localhost:8001/> in live mode when `.env` is set.

## 4. Your part

You are the owner. You do not have to watch, but the loop goes further with you:

- Answer what waits at `stage:owner` (`gh issue list --label stage:owner`, or the
  dashboard's **Needs you**) with a comment on the issue.
- Try the app on :8000 and :8001 now and then, and file what bothers you as issues
  (or ask the aide to). Add `owner-request` to keep one at P1 or above.
- Ask the monitor what shipped and what is stuck, and re-rank work in its pane.

The loop goes further when the owner checks in often and files what they want as
requests.

## 5. Stop

```bash
.claude/hooks/issuelooper stop --all
```

This stops the supervisor, its sessions, the dashboard and the panes; the servers
`deploy.sh` started keep running (`lsof -tiTCP:8000 -tiTCP:8001 -sTCP:LISTEN | xargs kill` stops them). `start`
picks up where it left off.
