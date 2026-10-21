# Live AI Concierge: Requirements

Build a web app in which a person talks to an AI concierge by voice or by typing. The concierge answers out loud, and when it uses a tool (weather, search, maps and so on) the result appears as a rich **card** next to the conversation. Screenshots of the target look are in `spec/ui/`.

The backend is Python with **Google ADK** (`google-adk==2.8.0`) and the **Gemini Live API** for bidirectional streaming audio. The frontend is served by the same server; the frontend technology is your choice.

Requirement IDs (`R-…`) are referenced by `spec/ACCEPTANCE.md`.

## 1. Running the app

- **R-RUN-1** `uv run concierge --port <port>` starts the server, and `http://localhost:<port>/` serves the app.
- **R-RUN-2** Configuration comes from environment variables, also read from `.env` (see `.env.example`). Vertex AI with Application Default Credentials is the supported auth path.
  - `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` (the Live model is served from `us-central1`)
  - `LIVE_MODEL`, default `gemini-3.8-live`
  - `TOOL_MODEL` for grounded tool calls, default `gemini-2.5-flash-lite`
  - `GOOGLE_MAPS_API_KEY` (optional; see R-MAP-3)
  - `HOME_LOCATION` (optional fallback location)
  - `PROFILES`, `SESSION_IDLE_GAP_S`, `SESSIONS_DB` (§7)
- **R-RUN-3** **Scripted mode.** When `CONCIERGE_SCRIPT=<path>` is set, the server must not contact Gemini. It replays the script described in §9 instead. Every automated end-to-end test and every UX run uses this mode, so it must drive exactly the same UI code paths as live mode.

## 2. Layout

- **R-LAYOUT-1** Dark theme. A top bar holds:
  - the app name with an icon
  - the current date and time, updated every minute
  - a speaker button
  - a settings button
  - a connection status pill
- **R-LAYOUT-2** Below the bar, two panes side by side. The left pane (about one third of the width) is the conversation **feed** with the **composer** at its bottom. The right pane holds the **cards**.
- **R-LAYOUT-3** When the viewport is narrower than 720 px there is a single column. Cards are shown inline in the feed, right after the turn that produced them, and the page reads from top to bottom.
- **R-LAYOUT-4** While no card has been shown, the right pane shows **starter chips**: one-tap prompts, each with an icon and a label. There is at least one chip for each of Weather, Web Search, Find Places, Routes and Video. Tapping a chip sends its prompt as a typed message.
- **R-LAYOUT-5** The newest card is scrolled into view. Older cards stay above it, newest at the bottom. The feed auto-scrolls to the latest message. If the user has scrolled up, a "scroll to bottom" button appears instead of the feed jumping, also when the user sends a message while scrolled up.

## 3. Conversation

- **R-CONV-1** The composer has a microphone toggle, a text input with the placeholder "Type instead…", and a send button. The send button is disabled while the input is empty. Pressing Enter sends.
- **R-CONV-2** A typed message appears in the feed immediately as a user bubble, aligned right. It is sent to the agent, and the agent's reply is **spoken** and also shown as an assistant bubble, aligned left. A typed question gets a spoken answer.
- **R-CONV-3** Voice mode. Tapping the mic streams 16 kHz mono PCM to the server. The model's audio comes back as 24 kHz PCM and is played. While voice mode is on, the mic button shows an active state. What the user says is transcribed into a user bubble, and what the model says is transcribed into an assistant bubble. Transcripts stream in as they arrive: partial text updates the same bubble rather than creating new ones.
- **R-CONV-4** Barge-in. When "Allow barge-in while speaking" is on in settings, and the user starts speaking or sends a message while the assistant is speaking, playback stops at once. The assistant bubble keeps the text spoken so far, and the new turn proceeds. When the setting is off, the assistant finishes speaking first.
- **R-CONV-5** The status pill shows one of these states, each with a distinct color:
  - `connecting`
  - `ready`
  - `listening` (mic on)
  - `thinking` (waiting for the model or a tool)
  - `speaking` (assistant audio playing)
  - `error` (connection lost; the app reconnects automatically with backoff)
- **R-CONV-6** While a tool is running, the feed shows a small progress note naming the tool, for example "Checking the weather…". It is removed or replaced when the card arrives.
- **R-CONV-7** The speaker button opens a popover with a mute toggle and a volume slider (0–100). Volume and mute persist across reloads.

## 4. Tools and cards

The agent has the tools below. Each tool returns a result object (§10). The model speaks a short summary, and the UI renders a card from the same object. Every card has a header with an icon and a title. Unless stated otherwise, the title is the query or the place.

| Tool | Card |
| --- | --- |
| `weather(location, lang)` | **R-WX** Weather card |
| `web_search(query, lang)` | **R-WEB** Web answer card |
| `maps_search(query, lang)` | **R-MAP** Places card |
| `maps_route(origin, destination, mode, lang)` | **R-ROUTE** Route card (Extended) |
| `place_info(place, question, lang)` | **R-PLACE** Place card (Extended) |
| `image_search(query, lang)` | **R-IMG** Image grid card (Extended) |
| `youtube_search(query, lang)` | **R-YT** Video card (Extended) |
| `remember(topic, fact)` | No card; see R-MEM |

- **R-WX-1** The weather card shows:
  - the location name and local time
  - the current temperature (large, in °C), the condition word with an icon, and "Feels like"
  - an hourly strip for the next 12 hours (time, icon, temperature)
  - a 5-day list (day label, where the first two rows are "Today" and "Tomorrow", then weekday names; icon; high / low)
  - a footer with wind, humidity, sunrise and sunset
- **R-WX-2** The card background is an animated sky that matches the condition: at least clear, cloudy, rain, snow and thunderstorm, with day and night variants for clear. The animation pauses when the card is scrolled out of view and is disabled under `prefers-reduced-motion`.
- **R-WX-3** In live mode, weather data comes from Open-Meteo (no API key). For the current location, use the stored coordinates (R-SET-4) rather than geocoding a name.
- **R-WEB-1** The web answer card shows the answer rendered as Markdown (headings, bold, lists, links), the query or queries used as chips, and a row of **source chips** (site name or domain) linking to the sources. In live mode, use Gemini with Google Search grounding.
- **R-MAP-1** The places card shows the places as a **numbered list**. Each entry shows the name, address and rating when known.
- **R-MAP-2** When `GOOGLE_MAPS_API_KEY` is set, the card also shows a Google map (Maps JavaScript API) with one pin per place, at the place's `lat`/`lng`. Each pin shows the same number as the place's entry in the list, and the map is framed so every pin is visible. The key is a browser key restricted to the page's origins: use it only in the page. Server-side results still come from sources that need no Maps key.
- **R-MAP-3** Without a key, the card shows the list only. This must not cause an error.
- **R-ROUTE-1** The route card shows origin and destination, a travel-mode chip (drive, walk, transit or bicycle), total duration and distance, and the steps as a list. With a Maps key it also shows a Google map above the steps with the route drawn on it, from origin to destination, framed to fit the route: the result's `polyline` when it has one, otherwise a route the page asks the Maps JavaScript API for.
- **R-PLACE-1** One `place_info` call gathers a summary, photos, location and an optional video concurrently, and renders them as **one** card in this order:
  1. a row of up to 3 photos
  2. a Google map with one pin at the place's `location`, when the result has a location and a Maps key is set
  3. the summary (Markdown)
  4. the video, if any (an embedded player)
  5. source chips
  A section with no data is omitted, not shown empty.
- **R-IMG-1** The image card shows a 2-column grid of thumbnails, each captioned with its source domain. Tapping a thumbnail opens an in-app lightbox with the larger image and its caption. Close it with ✕, Escape, or a tap on the backdrop.
- **R-YT-1** The video card embeds a player for the first result and lists the other results as links with titles. The titles must be real video titles, not "youtube.com".
- **R-LANG-1** Every tool takes a BCP-47 `lang`, and the agent passes the language of the turn. Words a card writes itself (for example "Today", "Tomorrow", "Feels like", travel modes, the progress note) are shown in that language. English and Japanese are required; English is the fallback for anything else. See the table in §11.

## 5. Settings sheet

The settings button opens a sheet over the page. Changes apply immediately and are saved to `localStorage`.

- **R-SET-1** **Language** of the UI chrome (English or Japanese). This does not change the language the model answers in.
- **R-SET-2** **Microphone**: a list of input devices, with "System default" first. The choice is restored after a reload.
- **R-SET-3** **Voice**: one of `Kore`, `Puck`, `Charon`, `Aoede`, `Fenrir`. The server provides the list and the default through an endpoint, so the list is not hard-coded in two places. Changing the voice reconnects the session. The conversation is kept.
- **R-SET-4** **Location**: shows the current location label, or "Not set". "Set location…" lets the user enter an address. When a Maps key is present there may also be a map picker. The app stores `{label, lat, lng}` and sends it to the server on (re)connect, and the agent resolves "here" and "nearby" to it. If nothing is set, `HOME_LOCATION` is used.
- **R-SET-5** **Interruption**: the "Allow barge-in while speaking" checkbox (R-CONV-4). It is on by default.
- **R-SET-6** **Clear conversation** clears the feed and the cards on screen only.
- **R-SET-7** **Forget my history** deletes all stored conversations and remembered facts of the current profile (R-HIST, R-MEM) after a confirmation.

## 6. Extended: conversation history (R-HIST)

- **R-HIST-1** The server owns conversation identity. Reloading the page within `SESSION_IDLE_GAP_S` (default 1800) of the last activity resumes the same conversation: the feed shows the previous turns again and the model remembers them.
- **R-HIST-2** After a longer gap, the next connection starts a **new** conversation with an empty feed. A 2–3 sentence recap of the previous conversation is generated and given to the model as context, so a follow-up like "book it for the same time" still makes sense. In scripted mode the recap is not generated, but the rollover must still happen.
- **R-HIST-3** Conversations are stored in SQLite at `SESSIONS_DB` (default `sessions.db`) and survive a server restart.

## 7. Extended: profiles and memory

- **R-PROF-1** `PROFILES=alice,bob` makes a "Who" picker appear in settings. Each profile has its own conversations and memory. Switching profiles clears the screen and reconnects. With zero or one profile the picker is hidden and a single default profile is used.
- **R-MEM-1** The agent calls `remember(topic, fact)` when the user states a lasting fact or preference. A fact with the same topic (case-insensitive) **replaces** the old one. Facts are given to the model in every new conversation of that profile.
- **R-MEM-2** Settings shows a "What I remember" list of the current profile's facts (topic: fact). It updates when a fact is added or replaced. Forget my history empties it.

## 8. Non-functional

- **R-NF-1** The server sanity-checks configuration at startup and exits with a clear message when something required is missing or invalid. In scripted mode no Google credentials are needed.
- **R-NF-2** The voice path must never block on a tool. While a slow tool runs, the user can keep talking or typing.
- **R-NF-3** No secrets are sent to the browser except the Maps JS key, which is meant to be public.

## 9. Scripted mode contract (`CONCIERGE_SCRIPT`)

The script is a JSON file:

```json
{
  "turns": [
    {
      "match": "weather in kyoto",
      "events": [
        {"type": "transcript", "text": "Let me check the weather in Kyoto."},
        {"type": "tool_call", "name": "weather", "args": {"location": "Kyoto", "lang": "en"},
         "result": {"…": "a weather result object, §10"}},
        {"type": "transcript", "text": "It is 20 degrees and clear in Kyoto."},
        {"type": "audio", "ms": 1500},
        {"type": "turn_complete"}
      ]
    }
  ],
  "fallback": [{"type": "transcript", "text": "Sorry, I have no script for that."}, {"type": "turn_complete"}]
}
```

- **R-SCRIPT-1** For each user message, typed or chip, the server picks the first turn whose `match` occurs in the message as a case-insensitive substring. If none matches, it uses `fallback`. It then replays the events in order. Any event may carry `"delay_ms"` (default 100), which is a wait before the event takes effect. For `tool_call`, the progress note appears as soon as the event starts and the card appears after `delay_ms`, which is how a slow tool is simulated.
- **R-SCRIPT-2** Event semantics:
  - `transcript` appends text to the current assistant bubble, exactly like a live output transcription.
  - `tool_call` runs the same UI path as a live tool call: progress note (R-CONV-6), then a card rendered from `result`. The real tool is **not** executed. `remember` calls are applied to the memory store.
  - `audio` makes the server send `ms` milliseconds of 24 kHz PCM (a quiet tone is fine) through the real playback path, so the status shows `speaking` while it plays.
  - `turn_complete` ends the turn.
- **R-SCRIPT-3** Barge-in applies to replay. A new user message during replay cancels the rest of the current turn when barge-in is allowed, and is queued until the turn ends when it is not.
- **R-SCRIPT-4** Microphone audio is accepted and discarded in scripted mode.

## 10. Tool result objects

The UI must render cards from these shapes. Extra fields are allowed, and missing optional fields (marked `?`) must not break a card.

```text
weather:   {location, local_time, lang, current: {temp_c, feels_like_c, condition, is_day, wind_kmh?, humidity?},
            hourly: [{time:"HH:MM", temp_c, condition}] (12), daily: [{date:"YYYY-MM-DD", high_c, low_c, condition}] (5),
            sunrise?:"HH:MM", sunset?:"HH:MM"}
            condition ∈ clear|partly_cloudy|cloudy|fog|drizzle|rain|snow|thunderstorm
web_search:{query, lang, queries?: [str], answer_markdown, sources: [{title, url, domain}]}
maps_search:{query, lang, places: [{name, address, lat, lng, rating?, place_id?}]}
maps_route:{origin, destination, mode: drive|walk|transit|bicycle, lang, duration_min, distance_km,
            steps: [{instruction, duration_min?}], polyline?}
            polyline: the route's path as a Google encoded polyline string
place_info:{place, lang, summary_markdown, photos?: [{url, domain}], location?: {lat, lng, address},
            video?: {video_id, title}, sources?: [{title, url, domain}]}
image_search:{query, lang, images: [{url, thumbnail_url?, domain, title?}]}
youtube_search:{query, lang, videos: [{video_id, title, channel?}]}
```

## 11. Required UI strings

| Key | en | ja |
| --- | --- | --- |
| today / tomorrow | Today / Tomorrow | 今日 / 明日 |
| feels like | Feels like | 体感 |
| weather footer | Wind / Humidity / Sunrise / Sunset | 風 / 湿度 / 日の出 / 日の入り |
| modes | Drive / Walk / Transit / Bicycle | 車 / 徒歩 / 電車 / 自転車 |
| progress | Checking the weather… / Searching the web… / Searching maps… / Finding a route… / Looking up the place… / Searching images… / Searching videos… | 天気を確認しています… / ウェブを検索しています… / 地図を検索しています… / ルートを検索しています… / 場所を調べています… / 画像を検索しています… / 動画を検索しています… |
| status | Connecting / Ready / Listening / Thinking / Speaking / Reconnecting | 接続中 / 準備完了 / 聞き取り中 / 考え中 / 話し中 / 再接続中 |
| conditions | Clear / Partly cloudy / Cloudy / Fog / Drizzle / Rain / Snow / Thunderstorm | 晴れ / 晴れ時々曇り / 曇り / 霧 / 霧雨 / 雨 / 雪 / 雷雨 |

## 12. Test hooks

Automated tests locate elements by these `data-testid` attributes. They are required.

| testid | Element |
| --- | --- |
| `status` | Status pill; also set `data-state` to the R-CONV-5 state name |
| `feed` | Conversation feed |
| `msg-user`, `msg-assistant` | Each user and assistant bubble |
| `progress` | Tool progress note |
| `composer-input`, `composer-send`, `composer-mic` | Composer controls |
| `cards` | Right-pane card container (narrow layout: cards are in `feed`) |
| `card` | Every card, with `data-card-type` set to the tool name |
| `starters`, `starter-chip` | Starter area and chips; each chip carries its prompt in `data-prompt` |
| `wx-hour`, `wx-day` | Each hourly entry and each daily row of the weather card |
| `source-chip` | Each source chip (an `<a>` with the source URL) |
| `place-item` | Each numbered entry of the places card |
| `route-step` | Each step of the route card |
| `places-map`, `route-map`, `place-map` | The map of the places, route and place card (only with a Maps key) |
| `map-pin` | Each pin on a card's map; its visible text is the pin's number on the places card |
| `settings-button`, `settings-sheet` | Settings |
| `setting-language`, `setting-mic`, `setting-voice`, `setting-interrupt`, `setting-profile` | Settings controls (`setting-profile` exists only with 2 or more profiles) |
| `location-label`, `location-set`, `location-input`, `location-confirm` | Location setting |
| `clear-conversation`, `forget-history`, `confirm-yes` | Clear and Forget actions |
| `memory-list`, `memory-item` | What I remember |
| `volume-button`, `volume-mute`, `volume-slider` | Speaker popover; `volume-mute` has `aria-pressed="true"` while muted, and `volume-slider` is an `<input type="range">` |
| `lightbox`, `lightbox-close` | Image lightbox |
| `scroll-bottom` | Scroll-to-bottom button |

Inside cards, tests read visible text, so render values as text. Weather temperatures are rounded integers, for example `20°`.
