# Sample: Live AI Concierge

The app issuelooper was developed and benchmarked on, from its first slice to the
full spec, in about six hours. A person talks to an AI concierge by voice or by
typing; it answers out loud, and when it uses a tool (weather, web search, places,
routes, place details, images, videos) the result appears as a card next to the
conversation. Python with Google ADK and the Gemini Live API, plain JavaScript in
the page.

You start from a working first slice (the layout, scripted mode, the weather card)
and 54 seed issues. The loop builds the rest: live mode on the real model, the other
cards, settings, history, profiles and memory. Its testers check live issues against
the real model, walk the app as users would every 25 minutes, and check it against
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

In the benchmark this sample comes from, a harness ran a UX tester outside the
loop: scripted user journeys, a few actions of its own, and a harsh review of every
screenshot, filed as proposals for a developer to accept. Here the loop's own tester
does that job in its UX runs, with the harness's material in the app repository:

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
  [uv](https://docs.astral.sh/uv/). Linux.
- A Google Cloud project with the Vertex AI API enabled, and Application Default
  Credentials on this machine (`gcloud auth application-default login`). Live mode
  uses `gemini-3.8-live` in `us-central1` (`LIVE_MODEL` in `.env`).
- Optional: a Google Maps JavaScript API key restricted to `localhost`, for the maps
  in the places, route and place cards. Without one the cards show lists.
- Ports 8000 and 8001 free: `deploy.sh` serves the app there for you.
- Cost: in the benchmark, every role on Gemini 4 Argon cost about $570 of model
  usage at Vertex list prices over 12 hours with 16 sessions, so plan on roughly
  half that for six hours.

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
herdr --session concierge
```

In the herdr pane:

```bash
.claude/hooks/issuelooper start
```

The monitor, an aide and the status view open in a tab named `issuelooper`; the
dashboard is at <http://127.0.0.1:8780/>. The supervisor starts workers on the P0
issues at once.

## 3. What happens

- **First hour or two:** the minimum viable product. Live mode is built in stages
  (typed text through ADK, the model's voice, the microphone, voice input), then the
  web answer and places cards and their live tools, barge-in, reconnects, startup
  checks. Each `needs:live` issue merges with `Refs #N` and closes after a tester's
  live check against the real model.
- **Then:** settings, the other cards and their live tools, history, profiles,
  memory, localization, docs. From the first merges on, UX runs file what gets in a
  user's way, and they become a growing share of the work.
- **When the workers run out of issues:** extra UX runs and spec checks, one
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

In the benchmark, a developer checked in every 15 minutes and filed about 50
requests over 12 hours; every role on Gemini 4 Argon held a UX score of 80 or more
(of 100, from a harsh audit of the deployed app) from 4 h 39 min to the end.

## 5. Stop

```bash
.claude/hooks/issuelooper stop --all
```

This stops the supervisor, its sessions, the dashboard and the panes; the servers
`deploy.sh` started keep running (`fuser -k 8000/tcp 8001/tcp` stops them). `start`
picks up where it left off.
