# Live AI Concierge

A web app in which a person talks to an AI concierge by voice or by typing. The
concierge answers out loud, and when it uses a tool (weather, web search, places,
routes, place details, images, videos) the result appears as a card next to the
conversation. Python with Google ADK and the Gemini Live API on the server, plain
JavaScript in the page.

- `spec/`: the requirements, the acceptance criteria, screenshots of the target look
  and a sample script for scripted mode.
- `ux/`: the UX checks: user journeys that drive the app in a browser, the script
  they run on, and the rubric findings are judged by.
- `make verify` runs the checks, `make scripted` runs the app without credentials,
  `make run` against the real model (copy `.env.example` to `.env` first).
