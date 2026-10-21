# Live AI Concierge seed backlog

`examples/seed_issues.py` files this backlog: each `## #N title` heading starts an
issue, the `meta:` line holds its labels and the issues it is blocked by, and the
rest up to the next heading is the body. The numbers are the GitHub issue numbers in
a fresh repository.

54 issues, numbered in the order of their value to the app's main purpose, each one a
small, testable step:

- **P0 (#1-#14), the minimum viable product:** a voice concierge you can talk to
  through the real Gemini Live API, by voice or text, that answers with the core
  cards and survives interruptions and dropped connections. Live mode is built in
  stages: typed text through ADK (#1), the model's voice (#2), the microphone (#3)
  and voice input (#4); then the core cards and their live tools (#5-#9); barge-in,
  reconnect and escaping (#10-#12); startup checks and recovery for live mode
  (#13-#14).
- **P1 (#15-#31), the next iteration:** the settings people need, the other cards,
  live tools for them, and visible bugs.
- **P2 (#32-#54), the rest:** history, profiles, memory, minor settings and
  localization, scripted-mode fixes, docs and chores.

The 11 `needs:live` issues need the real model to verify: they merge with
`Refs #N` and close after a tester's live check.

---

## #1 Live mode: answer typed messages through ADK
meta: type:feature-request P0 needs:live | blocked_by:

Today the app only works in scripted mode. Without `CONCIERGE_SCRIPT`, the server should open a Gemini Live session through ADK for each page connection (R-RUN-2, R-RUN-3): `LIVE_MODEL` on Vertex AI with the project and location from `.env`. A typed message goes to the model, and the model's reply, from its output transcription, streams into one assistant bubble through the same WebSocket messages scripted mode uses. When the model ends its turn, the pill returns to `ready`. A typed "hello" gets an assistant bubble within 60 s, and a follow-up message gets another one.

This is the first step of live mode: text in, transcript out. Playing the model's voice, voice input, startup checks and recovery are separate issues.

## #2 Live mode: play the model's voice
meta: type:feature-request P0 needs:live | blocked_by: 1

Relay the model's audio (24 kHz 16-bit mono PCM) to the page as binary frames, the way scripted mode sends audio, so live replies are spoken (R-CONV-2, R-CONV-3). The pill shows `speaking` while the audio plays and returns to `ready` after the turn. Partial transcripts keep updating the same assistant bubble rather than adding new ones.

## #3 Stream the microphone in voice mode
meta: type:feature-request P0 | blocked_by:

Tapping the mic only flips the status today. It should capture the microphone and stream 16 kHz mono 16-bit PCM over the WebSocket as binary frames while voice mode is on, show an active state on `composer-mic` and `listening` on the pill, and stop on a second tap (R-CONV-3). The scripted server already accepts and discards the audio (R-SCRIPT-4).

## #4 Live mode: talk by voice
meta: type:feature-request P0 needs:live | blocked_by: 1, 3

In live mode, forward the microphone frames from #3 to the model as realtime 16 kHz audio (R-CONV-3). What the user says is transcribed into a user bubble (input transcription), and the model answers as it does for typed messages. Saying "What is two plus two?" with the mic on produces a user bubble with those words and an assistant reply within 60 s.

## #5 Web answer card
meta: type:feature-request P0 | blocked_by:

Render `web_search` results as a web answer card (R-WEB-1, A-10): the answer as Markdown (headings, bold, lists, links) and one source chip per source, each an `<a data-testid="source-chip">` linking to its URL. Today the result is shown as raw JSON.

## #6 Places card
meta: type:feature-request P0 | blocked_by:

Render `maps_search` results as a places card (R-MAP-1/2/3, A-11): a numbered list 1..N, each `place-item` showing name, address and rating when known. When `GOOGLE_MAPS_API_KEY` is set, add a Google map (`places-map`) with one numbered `map-pin` per place, matching the list. With no key there is no map and no console errors.

## #7 Live mode: weather tool
meta: type:feature-request P0 needs:live | blocked_by: 1

Give the live agent the `weather` tool. It returns the §10 `weather` object, so the UI renders the same card as in scripted mode, and the model speaks a short summary. Weather data comes from Open-Meteo (no API key), with Open-Meteo's geocoding for place names (R-WX-3). It must work with the credentials in `.env` and no other API keys. Asking "What's the weather in Kyoto right now?" produces a weather card and an assistant reply within 60 s (A-19).

## #8 Live mode: web search tool
meta: type:feature-request P0 needs:live | blocked_by: 1, 5

Give the live agent the `web_search` tool. It returns the §10 `web_search` object, so the UI renders the same card as in scripted mode, and the model speaks a short summary. Answer with Gemini (`TOOL_MODEL`) and Google Search grounding, and return the grounding sources (R-WEB-1). It must work with the credentials in `.env` and no other API keys. Asking "Search the web for the latest news about the James Webb telescope." produces a web answer card and an assistant reply within 60 s (A-19).

## #9 Live mode: places tool
meta: type:feature-request P0 needs:live | blocked_by: 1, 6

Give the live agent the `maps_search` tool. It returns the §10 `maps_search` object, so the UI renders the same card as in scripted mode, and the model speaks a short summary. Use a source that needs no Maps key, for example Gemini with Google Maps grounding: `GOOGLE_MAPS_API_KEY` is a browser key for the page's map only (R-MAP-2/3). Give every place its `lat`/`lng` so the map can pin it. It must work with the credentials in `.env` and no other API keys. Asking "Find ramen shops near Shibuya Station." produces a places card and an assistant reply within 60 s (A-19).

## #10 Barge-in during scripted replay
meta: type:feature-request P0 | blocked_by:

A message sent while the assistant is speaking stops playback within 1 s, cancels the rest of the current turn (its remaining transcripts never appear), and the new turn proceeds (R-CONV-4, R-SCRIPT-3, A-13). Today the second message waits for the first turn to finish.

## #11 The app stops reconnecting after the server comes back late
meta: type:bug P0 | blocked_by:

If the server is down for more than a few seconds, the status pill stays on the error state forever, even after the server is back. R-CONV-5 says the app reconnects automatically with backoff. It should keep trying (backoff capped at about 10 s) and reach `ready` once the server is up.

## #12 Typed HTML in a message is rendered instead of shown as text
meta: type:bug P0 | blocked_by:

Typing `<b>hello</b>` and pressing Enter shows a bold "hello" in my bubble. Typing `<img src=x onerror=alert(1)>` runs the handler. A user bubble must show exactly the characters I typed.

## #13 Live mode: check the configuration at startup
meta: type:feature-request P0 | blocked_by: 1

R-NF-1: in live mode (no `CONCIERGE_SCRIPT`), the server checks its configuration before it serves anything: `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` are set and not empty, and Application Default Credentials can be found. If something is missing, it exits with a non-zero code and a one-line message naming what is missing, without a traceback. Scripted mode needs none of this and must still start without Google configuration.

## #14 Live mode: recover when the Live session fails
meta: type:feature-request P0 needs:live | blocked_by: 1, 11

If the Live session fails or the API closes it (for example a wrong `LIVE_MODEL`, or a session time limit), the page must not hang on `thinking`. The server closes the session cleanly without crashing, tells the page (the pill shows `error` and the page reconnects with backoff, R-CONV-5) and opens a new Live session on the next connection. After the server itself restarts, the page reconnects and the live conversation works again.

## #15 Settings sheet
meta: type:feature-request P1 | blocked_by:

The settings button opens a `settings-sheet` over the page and it can be closed again (§5). This is the container for the settings issues that follow; changes there apply immediately and are saved to `localStorage`.

## #16 Voice setting served by the server
meta: type:feature-request P1 | blocked_by: 15

Add `setting-voice` (R-SET-3, A-14). The server provides the five voices (Kore, Puck, Charon, Aoede, Fenrir) and the default through an endpoint; the page reads the list from it. The choice survives a reload, and changing it reconnects while keeping the conversation on screen.

## #17 UI language setting
meta: type:feature-request P1 | blocked_by: 15

Add `setting-language` (English / Japanese) to the settings sheet (R-SET-1). It changes the UI chrome, including the status text, and survives a reload. It does not change the language the model answers in.

## #18 Location setting
meta: type:feature-request P1 | blocked_by: 15

Add the location setting (R-SET-4, A-15): `location-label` shows the label or "Not set"; `location-set` → `location-input` → `location-confirm` sets it. Store `{label, lat, lng}`, keep it across reloads, and send it to the server on (re)connect.

## #19 Speaker popover with mute and volume
meta: type:feature-request P1 | blocked_by:

The speaker button opens a popover with `volume-mute` (`aria-pressed="true"` while muted) and `volume-slider` (`<input type="range">`, 0–100) (R-CONV-7, A-17). Both apply to playback and survive a reload.

## #20 Route card
meta: type:feature-request P1 | blocked_by:

Render `maps_route` results as a route card (R-ROUTE-1, A-20): origin, destination, a travel-mode chip, total duration and distance, and every step as a `route-step`. When `GOOGLE_MAPS_API_KEY` is set, add a Google map (`route-map`) with the route drawn on it (the result's `polyline`, or a route the page asks Maps for).

## #21 Place card
meta: type:feature-request P1 | blocked_by:

Render `place_info` results as one place card (R-PLACE-1, A-21): up to 3 photos, then a Google map with the place's pin (`place-map`, when the result has a location and `GOOGLE_MAPS_API_KEY` is set), then the summary (Markdown), then the embedded video if any, then source chips. Sections with no data are omitted, not shown empty.

## #22 Image grid card
meta: type:feature-request P1 | blocked_by:

Render `image_search` results as a 2-column grid of thumbnails, each captioned with its source domain (R-IMG-1).

## #23 Video card
meta: type:feature-request P1 | blocked_by:

Render `youtube_search` results as a video card (R-YT-1, A-23): an embedded player for the first `video_id`, and the other results as links showing their real titles.

## #24 Live mode: route tool
meta: type:feature-request P1 needs:live | blocked_by: 1, 20

Give the live agent the `maps_route` tool. It returns the §10 `maps_route` object, so the UI renders the same card as in scripted mode (with a `polyline` when the source has one, so the map can draw the route), and the model speaks a short summary. It must work with the credentials in `.env` and no other API keys. Asking "How do I get from Tokyo Station to Asakusa by train?" produces a route card and an assistant reply within 60 s (A-27).

## #25 Live mode: place tool
meta: type:feature-request P1 needs:live | blocked_by: 1, 21

Give the live agent the `place_info` tool. It returns the §10 `place_info` object, so the UI renders the same card as in scripted mode, and the model speaks a short summary. Gather the summary, photos, location (`lat`/`lng`, for the card's map) and optional video concurrently (R-PLACE-1). It must work with the credentials in `.env` and no other API keys. Asking "Tell me about Kinkaku-ji." produces a place card and an assistant reply within 60 s (A-27).

## #26 Live mode: image search tool
meta: type:feature-request P1 needs:live | blocked_by: 1, 22

Give the live agent the `image_search` tool. It returns the §10 `image_search` object, so the UI renders the same card as in scripted mode, and the model speaks a short summary. It must work with the credentials in `.env` and no other API keys. Asking "Show me pictures of Mount Fuji." produces a image grid card and an assistant reply within 60 s (A-27).

## #27 Live mode: video search tool
meta: type:feature-request P1 needs:live | blocked_by: 1, 23

Give the live agent the `youtube_search` tool. It returns the §10 `youtube_search` object, so the UI renders the same card as in scripted mode, and the model speaks a short summary. Video titles must be real titles (R-YT-1). It must work with the credentials in `.env` and no other API keys. Asking "Find a YouTube video that teaches how to make sushi." produces a video card and an assistant reply within 60 s (A-27).

## #28 Status does not show "thinking" while a tool runs after speech
meta: type:bug P1 | blocked_by:

In a turn where the assistant speaks first and then calls a slow tool, the pill stays on `speaking` (or jumps to `ready`) for the whole tool call. R-CONV-5: `thinking` means waiting for the model or a tool.

## #29 Weather footer stays in English on Japanese cards
meta: type:bug P1 | blocked_by:

With `lang: "ja"`, the weather card still says Wind, Humidity, Sunrise and Sunset. §11 requires 風, 湿度, 日の出 and 日の入り.

## #30 A message of only spaces can be sent
meta: type:bug P1 | blocked_by:

When the input holds only spaces, the send button is correctly disabled, but pressing Enter still adds an empty bubble to the feed and sends it to the agent. Whitespace-only input should count as empty (R-CONV-1).

## #31 The clock in the top bar stops updating
meta: type:bug P1 | blocked_by:

The date and time are correct when the page loads, but after a couple of minutes the clock is behind. R-LAYOUT-1 says it updates every minute.

## #32 Resume the conversation after a reload
meta: type:feature-request P2 | blocked_by:

The server owns conversation identity (R-HIST-1). Reloading within `SESSION_IDLE_GAP_S` (default 1800) shows the previous turns again: user and assistant bubbles, and cards.

## #33 Keep conversations across a server restart
meta: type:feature-request P2 | blocked_by: 32

Store conversations in SQLite at `SESSIONS_DB` (default `sessions.db`) so a reload after a server restart still shows the previous turns (R-HIST-3, A-24).

## #34 Start a new conversation after the idle gap
meta: type:feature-request P2 | blocked_by: 32

After more than `SESSION_IDLE_GAP_S` of inactivity, the next connection starts a new conversation with an empty feed (R-HIST-2, A-24). In live mode a short recap of the previous conversation is given to the model; in scripted mode only the rollover happens.

## #35 Remember facts and show them in settings
meta: type:feature-request P2 | blocked_by: 15

Apply `remember(topic, fact)` calls, including scripted ones, to a per-profile memory store (R-MEM-1/2, A-26). Settings shows "What I remember" (`memory-list`, one `memory-item` per fact, "topic: fact"), updated as facts arrive. A fact whose topic matches an existing one case-insensitively replaces it.

## #36 Forget my history
meta: type:feature-request P2 | blocked_by: 32, 35

Add `forget-history` to settings (R-SET-7, A-26). After `confirm-yes` it deletes the current profile's stored conversations and remembered facts: `memory-list` empties and the next reload shows no previous bubbles.

## #37 Profiles
meta: type:feature-request P2 | blocked_by: 15, 32

With `PROFILES=alice,bob`, settings shows `setting-profile` (R-PROF-1, A-25). Each profile has its own conversations; switching clears the screen, reconnects and shows that profile's conversation. With zero or one profile the picker is hidden.

## #38 Clear conversation
meta: type:feature-request P2 | blocked_by: 15

Add `clear-conversation` to settings (R-SET-6, A-16). It empties the feed and the cards on screen and brings the starter chips back.

## #39 Barge-in setting
meta: type:feature-request P2 | blocked_by: 10, 15

Add the "Allow barge-in while speaking" checkbox, `setting-interrupt`, on by default (R-SET-5). When it is off, a message sent during a turn is queued and its turn starts only after the current one completes. The setting survives a reload.

## #40 Microphone device setting
meta: type:feature-request P2 | blocked_by: 15

Add `setting-mic` (R-SET-2): the input devices, with "System default" first. The choice is restored after a reload.

## #41 Scroll-to-bottom button in the feed
meta: type:feature-request P2 | blocked_by:

The feed auto-scrolls to the latest message, but if I have scrolled up to read, new messages should not yank me down. Show a `scroll-bottom` button instead, which scrolls to the end and then hides (R-LAYOUT-5).

## #42 Query chips on the web answer card
meta: type:feature-request P2 | blocked_by: 5

Show the query or queries used (`queries`, or `query` when `queries` is missing) as chips on the web answer card (R-WEB-1).

## #43 Localize the route mode chip
meta: type:feature-request P2 | blocked_by: 20

The route card's mode chip uses the turn's language (R-LANG-1, §11): Drive / Walk / Transit / Bicycle in English, 車 / 徒歩 / 電車 / 自転車 in Japanese, English for anything else.

## #44 Image lightbox
meta: type:feature-request P2 | blocked_by: 22

Tapping a thumbnail on the image card opens an in-app `lightbox` with the larger image and its caption. ✕ (`lightbox-close`), Escape and a tap on the backdrop each close it (R-IMG-1, A-22).

## #45 Localize the top-bar clock
meta: type:feature-request P2 | blocked_by: 17

When the UI language is Japanese, the date and time in the top bar use Japanese formatting (for example 10月8日(木) 13:05).

## #46 "Partly cloudy" is not translated on Japanese weather cards
meta: type:bug P2 | blocked_by:

A Japanese weather card (`lang: "ja"`) with condition `partly_cloudy` shows the English word. §11 gives 晴れ時々曇り. The other conditions are fine.

## #47 Status pill says "Error" while reconnecting
meta: type:bug P2 | blocked_by:

When the connection drops, the pill reads "Error" (エラー in Japanese). §11 gives "Reconnecting" / 再接続中 as the text for that state. Keep `data-state="error"`.

## #48 Two cards from one turn appear in reverse order on a phone
meta: type:bug P2 | blocked_by:

In the narrow layout, when one turn produces two cards, the second card is placed above the first. Cards should appear in the order they arrived, after that turn's bubbles (R-LAYOUT-3).

## #49 `delay_ms` is ignored on audio and turn_complete events
meta: type:bug P2 | blocked_by:

R-SCRIPT-1 says any event may carry `delay_ms` (default 100) as a wait before it takes effect. The server honors it only for `transcript` and `tool_call`; `audio` and `turn_complete` happen at once.

## #50 A bad CONCIERGE_SCRIPT path starts a broken server
meta: type:bug P2 | blocked_by:

`CONCIERGE_SCRIPT=/no/such/file uv run concierge` starts and serves the page, and then every message fails. R-NF-1: the server should exit at startup with a clear message naming the problem.

## #51 Script turns whose `match` has capital letters never fire
meta: type:bug P2 | blocked_by:

A scripted turn with `"match": "Weather in Paris"` is never selected, even when I type exactly "Weather in Paris"; the fallback plays instead. R-SCRIPT-1 says matching is a case-insensitive substring match on both sides.

## #52 Document configuration in the README
meta: type:documentation P2 | blocked_by:

The README does not list the environment variables. Add a configuration section naming every variable in R-RUN-2 (and `CONCIERGE_SCRIPT`) with one line on what each does, and show how to run in scripted mode.

## #53 Document the WebSocket protocol
meta: type:documentation P2 | blocked_by:

Write `docs/protocol.md` describing the `/ws` protocol between page and server: every JSON message type in each direction with its fields, and the binary audio frames (format and direction).

## #54 Check formatting in `make verify`
meta: type:chore P2 | blocked_by:

Add `ruff format --check src tests` to `make verify` and format the code so it passes.
