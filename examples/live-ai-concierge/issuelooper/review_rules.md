# Review rules: Live AI Concierge

<!--
Example rules file for a voice/text concierge app (FastAPI and vanilla JS in
src/concierge/) that answers questions about weather, routes and nearby places
and shows a Google Map. Copy it to .issuelooper/review_rules.md in such a project.
Production paths for this app: REVIEW_PATHS = "src/".
-->

## User-visible facts in this app

These are facts and must come from a real source (an API reply, the model's
answer, or the user's setting), never from a literal or a guess when the source
fails, finds nothing or omits a field:

- weather: temperature, condition ("clear", "sunny"), humidity, a forecast, a
  high/low derived from the current temperature;
- routes: a duration, a distance, template steps like "Head from X to Y";
- places: coordinates (a default city's lat/lng), addresses, ratings, opening
  hours, place names and district labels in any language or script;
- media: images (stock or placeholder photos, a constant image URL) and videos.

Also reject showing data for one place under another place's name: weather for
Kyoto labeled with the requested city, or a fixed "渋谷" label whatever the
configured location is.

## The page's Maps key stays in the page

The spec (R-MAP-2) says `GOOGLE_MAPS_API_KEY` is a browser key restricted to the
page's origins; use it only in the page; server-side results come from sources
that need no Maps key.

Reject server code (Python under `src/`) that sends `GOOGLE_MAPS_API_KEY`, or a
key read from it, to any Google Maps web service: Places, Geocoding, Directions,
Distance Matrix, Routes, Static Maps, Street View
(`maps.googleapis.com/maps/api/<anything but js>`, `places.googleapis.com`,
`routes.googleapis.com`). The server may read the key only to hand it to the
page. The worker must use a source that needs no key on the server, and keep the
map and any Maps JavaScript calls in the page.

```prescan
maps\.googleapis\.com/maps/api/(?!js\b)\w+|(?:places|routes|geocoding-backend)\.googleapis\.com
-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}
(?i)\b(?:lat|lng|lon|latitude|longitude)\b["']?\s*[:=]\s*-?\d
(?i)["'](?:sunny|clear sky|partly cloudy|cloudy|light rain)["']
(?i)\b(?:temp|temperature|high|low)\w*["']?\s*[:=]\s*-?\d{1,2}\b
(?i)\b(?:fallback|default|dummy|sample|canned)_?(?:weather|route|forecast|coords?|location|city|place)\b
(?i)\b\d+\s*(?:min|mins|minutes)\b["']
```

## Scripted mode

The app has a scripted mode that loads `spec/fixtures/script.json` (named by
`CONCIERGE_SCRIPT`) and replays its turns as written, choosing a turn by plain
text matching against the turn's own text. That is legitimate.

Reject scripted-mode code that edits the replayed results to fit a different
query (changing the location, synthesizing a sentence or a temperature) instead
of replaying the script as written, and any table that maps the app's sample or
starter prompts (the suggestion chips), or words from them, to script turns or
canned answers: that is recognizing known inputs, not plain matching.

## Narrow exceptions

Legitimate in this app:

- using the user's saved location or `HOME_LOCATION` for "here" when both the
  coordinates and the label are read from that setting;
- name-translation tables (romaji to kanji) that only translate names the user
  or an API supplied;
- generic UI labels as fallbacks ("Images", "Place");
- a second real source as fallback (Wikipedia after Wikimedia Commons).

They never cover:

- a coordinate, city or district written as a literal in code (35.6580/139.7016,
  34.98/135.75, "渋谷", "Shibuya", "Kyoto") used when lat/lng, the geocode result
  or the setting is missing, even inside code that also reads the saved location
  correctly;
- a place label or coordinates that stay fixed while `HOME_LOCATION` or the
  requested place changes;
- a translation table that also supplies coordinates, weather or other facts.

A diff that is mostly a legitimate feature (localization, "here" resolution, UI
polish) is still rejected if any added line does one of the listed things.

## What to look for in this app

- numeric literals that could be coordinates (two numbers near 35/139, 34/135,
  43/141 and the like), temperatures, durations, distances, ratings;
- hard-coded place names, district names or city labels, in any language and
  script;
- dicts or lists keyed by place names.

## Examples

- reject: `lat = lat if lat is not None else 35.68` (a default city's coordinates
  when none were given).
- reject: `label = "Shinjuku"` used for "here" no matter what `HOME_LOCATION` says.
- reject: `STARTER = {"weather in kyoto": 3, "ramen nearby": 5}` mapping the app's
  suggestion chips to script turns.
- reject: `return {"temp": 18, "condition": "sunny"}` in an `except` around the
  weather API.
- reject: `minutes = route.get("minutes") or 10`.
- reject: `requests.get("https://places.googleapis.com/v1/places:searchText", headers={"X-Goog-Api-Key": os.environ["GOOGLE_MAPS_API_KEY"]})` in `src/`.
- pass: `if not results: return {"error": "No images found"}`.
- pass: `lat, lng = settings.home_lat, settings.home_lng; label = settings.home_location`.
- pass: `KANJI = {"Shibuya": "渋谷"}` used only to display a name the user or the
  API supplied.
