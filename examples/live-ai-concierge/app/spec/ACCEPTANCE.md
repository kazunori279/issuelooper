# Acceptance Criteria

The app is accepted when every criterion below holds. Unless a criterion says **live**, it is checked in scripted mode (`CONCIERGE_SCRIPT`, see REQUIREMENTS §9) in headless Chromium at 1280×800, using the `data-testid` hooks in REQUIREMENTS §12.

`spec/fixtures/script.json` is a sample script that exercises every criterion. The UX runs check the app with **another script** (`ux/ux_script.json`). It has the same shape but different places, numbers, texts and languages, so do not hard-code fixture values.

## Core

| ID | Criterion | Req |
| --- | --- | --- |
| A-01 | `uv run concierge --port P` serves the page. `status` reaches `data-state="ready"` within 10 s. No Google credentials are needed in scripted mode. | R-RUN-1/3, R-NF-1 |
| A-02 | At 1280×800 the feed and the cards pane are side by side (feed on the left, narrower). `starters` is visible with at least 5 `starter-chip`s. | R-LAYOUT-1/2/4 |
| A-03 | At 390×844, there is one column, and a card produced by a turn appears inside `feed` after that turn's bubbles. | R-LAYOUT-3 |
| A-04 | `composer-send` is disabled while the input is empty. Typing and pressing Enter adds a `msg-user` with the text and clears the input. | R-CONV-1/2 |
| A-05 | After a scripted turn, a single `msg-assistant` holds all of that turn's `transcript` texts, in order. | R-CONV-2/3, R-SCRIPT-2 |
| A-06 | During an `audio` event, `status` is `speaking`. After `turn_complete`, it returns to `ready`. | R-CONV-5, R-SCRIPT-2 |
| A-07 | A `tool_call` with `delay_ms` ≥ 1000 shows a `progress` note with the tool's progress text (§11) until the card appears. | R-CONV-6 |
| A-08 | A weather `tool_call` renders a `card[data-card-type=weather]` showing: the location, the rounded current temperature, the condition, "Feels like", 12 hourly entries, 5 daily rows that start with "Today" and "Tomorrow" and show high/low, and sunrise and sunset. | R-WX-1 |
| A-09 | The same weather result with `lang: "ja"` shows 今日, 明日, 体感 and the footer labels 風, 湿度, 日の出 and 日の入り. | R-LANG-1 |
| A-10 | A web_search card renders the Markdown (bold becomes bold, lists become list items, links are links), shows one source chip per source, and each chip links to its URL. | R-WEB-1 |
| A-11 | A maps_search card lists every place, numbered 1..N, with name and address. With no `GOOGLE_MAPS_API_KEY` there are no console errors from the app. With a key, the card also shows a `places-map` with one `map-pin` per place, numbered 1..N like the list, and no console errors. | R-MAP-1/2/3 |
| A-12 | Tapping a `starter-chip` sends its prompt (a `msg-user` with the chip's prompt text appears). | R-LAYOUT-4 |
| A-13 | Barge-in on: a message sent during a long `audio` event stops speaking within 1 s, and the rest of the first turn's transcript never appears. Barge-in off: the second turn starts only after the first completes. | R-CONV-4, R-SCRIPT-3 |
| A-14 | `setting-voice` offers exactly the server's five voices. Changing the voice, the language and the interrupt checkbox all survive a reload. Switching the language to Japanese changes the status text to Japanese. | R-SET-1/3/5 |
| A-15 | Setting a location through `location-set` → `location-input` → `location-confirm` updates `location-label`, and the label survives a reload. | R-SET-4 |
| A-16 | `clear-conversation` empties the feed and the cards. | R-SET-6 |
| A-17 | Mute and volume set in the speaker popover survive a reload. | R-CONV-7 |
| A-18 | Several cards in a row are appended in order, and the newest card is scrolled into view. | R-LAYOUT-5 |
| A-19 | **Live** (real Gemini): typed questions about the weather in a city, a web search, and places near a landmark each produce a spoken reply and a card of the right type within 60 s. | R-CONV-2, §4 |

## Extended

| ID | Criterion | Req |
| --- | --- | --- |
| A-20 | A maps_route card shows origin, destination, the mode chip in the turn's language, duration, distance and every step. With a Maps key it also shows a `route-map` with the route drawn on it, and no console errors. | R-ROUTE-1, R-LANG-1 |
| A-21 | A place_info card shows photos, then summary, then video, then sources, in that order. When the result has no video and no photos, those sections are absent (not empty boxes). With a Maps key and a location, a `place-map` with one `map-pin` sits between the photos and the summary. | R-PLACE-1 |
| A-22 | An image_search card shows a 2-column grid with domain captions. A thumbnail opens `lightbox`, and Escape, ✕ and a backdrop tap each close it. | R-IMG-1 |
| A-23 | A youtube_search card embeds a player for the first `video_id` and lists the other titles. | R-YT-1 |
| A-24 | A reload within `SESSION_IDLE_GAP_S` shows the previous bubbles again, and so does a server restart. With `SESSION_IDLE_GAP_S=2`, a reload after 3 s of inactivity shows an empty feed. | R-HIST-1/2/3 |
| A-25 | With `PROFILES=alice,bob`, `setting-profile` is shown. Turns made as alice are not shown after switching to bob, and they come back when switching back to alice. | R-PROF-1 |
| A-26 | A scripted `remember` call adds a `memory-item`. A second call with the same topic in different case replaces it. `forget-history` + `confirm-yes` empties `memory-list`, and the next reload shows no previous bubbles. | R-MEM-1/2, R-SET-7 |
| A-27 | **Live**: questions that call for place_info, image_search, youtube_search and maps_route each produce a card of that type within 60 s. | §4 |
