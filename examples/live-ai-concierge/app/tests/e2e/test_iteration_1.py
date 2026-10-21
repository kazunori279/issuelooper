"""End-to-End Acceptance Verification for Criteria A-01 through A-09.

Validates:
- A-01: Server startup, ready state within 10s without Google credentials in scripted mode.
- A-02: 1280x800 desktop layout (side-by-side, feed left and narrower) & >= 5 starter chips.
- A-03: 390x844 mobile layout (single column, cards inside feed after turn's bubbles).
- A-04: Composer input/send button states, Enter sends msg-user and clears input.
- A-05: Scripted turn accumulation in a single msg-assistant bubble in order.
- A-06: Status transitions to speaking during audio and returns to ready after turn_complete.
- A-07: Slow tool_call (delay_ms >= 1000) shows progress note until card appears.
- A-08: Weather card components (location, rounded temp, condition, feels like, 12 hourly, 5 daily with Today/Tomorrow, sunrise/sunset).
- A-09: Weather card Japanese localization (今日, 明日, 体感).
"""

import json
import os
import pathlib
import socket
import subprocess
import time
import urllib.request
from typing import Any

from playwright.sync_api import Page, expect

REPO = pathlib.Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_script_path() -> pathlib.Path:
    script_env = os.environ.get("CONCIERGE_SCRIPT")
    if script_env and pathlib.Path(script_env).is_file():
        return pathlib.Path(script_env)
    return REPO / "spec" / "fixtures" / "script.json"


def load_script() -> dict[str, Any]:
    with open(get_script_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def find_turn_by_tool(name: str, lang: str | None = None) -> tuple[str, dict[str, Any]]:
    """Returns (match_text, tool_event) for a tool with optional lang filter."""
    script = load_script()
    for turn in script.get("turns", []):
        for event in turn.get("events", []):
            if event.get("type") == "tool_call" and event.get("name") == name:
                args = event.get("args", {})
                event_lang = args.get("lang") or event.get("result", {}).get("lang")
                if lang is None:
                    return turn.get("match", ""), event
                if lang == "ja" and event_lang and event_lang.startswith("ja"):
                    return turn.get("match", ""), event
                if lang != "ja" and (not event_lang or not event_lang.startswith("ja")):
                    return turn.get("match", ""), event
    raise ValueError(f"No turn with tool '{name}' and lang='{lang}' found in script")


def find_turn_with_delayed_tool(min_delay_ms: int = 1000) -> tuple[str, str, int, dict[str, Any]]:
    """Returns (match_text, tool_name, delay_ms, event) for a tool call with delay >= min_delay_ms."""
    script = load_script()
    for turn in script.get("turns", []):
        for event in turn.get("events", []):
            if event.get("type") == "tool_call" and event.get("delay_ms", 0) >= min_delay_ms:
                return turn.get("match", ""), event.get("name", ""), event.get("delay_ms", 0), event
    raise ValueError(f"No tool call with delay_ms >= {min_delay_ms} found in script")


def find_turn_with_multi_transcript() -> tuple[str, list[str]]:
    """Returns (match_text, transcript_texts) for a turn with multiple transcript events."""
    script = load_script()
    for turn in script.get("turns", []):
        texts = [ev.get("text", "") for ev in turn.get("events", []) if ev.get("type") == "transcript"]
        if len(texts) > 1:
            return turn.get("match", ""), texts
    for turn in script.get("turns", []):
        texts = [ev.get("text", "") for ev in turn.get("events", []) if ev.get("type") == "transcript"]
        if texts:
            return turn.get("match", ""), texts
    raise ValueError("No turn with transcript found in script")


def find_turn_with_audio() -> tuple[str, int]:
    """Returns (match_text, audio_ms) for a turn with an audio event."""
    script = load_script()
    for turn in script.get("turns", []):
        for event in turn.get("events", []):
            if event.get("type") == "audio" and event.get("ms", 0) > 0:
                return turn.get("match", ""), event.get("ms", 0)
    raise ValueError("No turn with audio found in script")


# A-01: Server startup, ready state within 10s without Google credentials in scripted mode
def test_a01_serve_page_and_status_ready_within_10s_without_credentials(page: Page, tmp_path):
    """A-01: `uv run concierge --port P` serves the page.
    `status` reaches `data-state="ready"` within 10 s without Google credentials in scripted mode.
    """
    port = _free_port()
    clean_env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("GOOGLE_") and not k.startswith("GEMINI_") and "CREDENTIALS" not in k
    }
    clean_env["CONCIERGE_SCRIPT"] = str(get_script_path())
    clean_env["SESSIONS_DB"] = str(tmp_path / "sessions.db")

    proc = subprocess.Popen(
        ["uv", "run", "concierge", "--port", str(port)],
        cwd=REPO,
        env=clean_env,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        start_time = time.time()
        connected = False
        while time.time() - start_time < 10:
            try:
                urllib.request.urlopen(url, timeout=1)
                connected = True
                break
            except OSError:
                time.sleep(0.2)
        assert connected, "Server failed to start and respond within 10s"

        page.goto(url)
        status = page.locator('[data-testid="status"]')
        expect(status).to_have_attribute("data-state", "ready", timeout=10000)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# A-02: 1280x800 desktop layout & starter chips
def test_a02_desktop_layout_side_by_side_and_starters(page: Page, server_url: str):
    """A-02: At 1280×800 the feed and the cards pane are side by side (feed on the left, narrower).
    `starters` is visible with at least 5 `starter-chip`s.
    """
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server_url)
    expect(page.locator('[data-testid="status"]')).to_have_attribute("data-state", "ready", timeout=10000)

    feed = page.locator('[data-testid="feed"]')
    cards = page.locator('[data-testid="cards"]')
    expect(feed).to_be_visible()
    expect(cards).to_be_visible()

    feed_box = feed.bounding_box()
    cards_box = cards.bounding_box()
    assert feed_box is not None and cards_box is not None
    # Feed on the left
    assert feed_box["x"] < cards_box["x"], f"Expected feed x ({feed_box['x']}) < cards x ({cards_box['x']})"
    # Feed narrower than cards
    assert feed_box["width"] < cards_box["width"], (
        f"Expected feed width ({feed_box['width']}) < cards width ({cards_box['width']})"
    )

    # Starters container visible with >= 5 chips
    starters = page.locator('[data-testid="starters"]')
    expect(starters).to_be_visible()

    chips = starters.locator('[data-testid="starter-chip"]')
    chip_count = chips.count()
    assert chip_count >= 5, f"Expected at least 5 starter chips, got {chip_count}"

    for i in range(chip_count):
        prompt = chips.nth(i).get_attribute("data-prompt")
        assert prompt and prompt.strip(), f"Starter chip {i} must have non-empty data-prompt"


# A-03: 390x844 mobile layout & card after bubbles
def test_a03_narrow_viewport_single_column_and_card_after_bubbles(page: Page, server_url: str):
    """A-03: At 390×844, there is one column, and a card produced by a turn appears
    inside `feed` after that turn's bubbles.
    """
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(server_url)
    expect(page.locator('[data-testid="status"]')).to_have_attribute("data-state", "ready", timeout=10000)

    # Cards pane is hidden on narrow view
    cards_pane = page.locator("#cards-pane")
    expect(cards_pane).not_to_be_visible()

    match_text, tool_event = find_turn_by_tool("weather")
    card_type = tool_event.get("name", "weather")

    input_box = page.locator('[data-testid="composer-input"]')
    input_box.fill(match_text)
    input_box.press("Enter")

    feed_card = page.locator(f'[data-testid="feed"] [data-testid="card"][data-card-type="{card_type}"]')
    expect(feed_card).to_be_visible(timeout=10000)

    expect(page.locator('[data-testid="status"]')).to_have_attribute("data-state", "ready", timeout=10000)

    user_bubble = page.locator('[data-testid="feed"] [data-testid="msg-user"]').last
    expect(user_bubble).to_be_visible()

    dom_indices = page.evaluate("""() => {
        const feed = document.querySelector('[data-testid="feed"]');
        const children = Array.from(feed.children);
        const userIdx = children.findIndex(el => el.getAttribute('data-testid') === 'msg-user');
        const asstIdx = children.findIndex(el => el.getAttribute('data-testid') === 'msg-assistant');
        const cardIdx = children.findIndex(el => el.getAttribute('data-testid') === 'card');
        return { userIdx, asstIdx, cardIdx };
    }""")

    user_idx = dom_indices["userIdx"]
    asst_idx = dom_indices["asstIdx"]
    card_idx = dom_indices["cardIdx"]

    assert card_idx != -1, "Card not found in feed"
    assert user_idx != -1, "User bubble not found in feed"
    assert user_idx < card_idx, f"Card (idx {card_idx}) must appear after msg-user (idx {user_idx})"

    if asst_idx != -1:
        assert asst_idx < card_idx, f"Card (idx {card_idx}) must appear after msg-assistant (idx {asst_idx})"

    user_box = user_bubble.bounding_box()
    card_box = feed_card.bounding_box()
    assert user_box is not None and card_box is not None
    assert card_box["y"] > user_box["y"], "Card must be vertically positioned below user message"


# A-04: Composer input and send button behavior
def test_a04_composer_send_disabled_empty_and_submits_message(page: Page, server_url: str):
    """A-04: `composer-send` is disabled while the input is empty.
    Typing and pressing Enter adds a `msg-user` with the text and clears the input.
    """
    page.goto(server_url)
    expect(page.locator('[data-testid="status"]')).to_have_attribute("data-state", "ready", timeout=10000)

    send_btn = page.locator('[data-testid="composer-send"]')
    input_box = page.locator('[data-testid="composer-input"]')

    # Initially disabled
    expect(send_btn).to_be_disabled()

    # Whitespace only keeps disabled
    input_box.fill("     ")
    expect(send_btn).to_be_disabled()

    input_box.fill("")
    expect(send_btn).to_be_disabled()

    # Typing text enables send
    test_text = "Testing user message submission"
    input_box.fill(test_text)
    expect(send_btn).to_be_enabled()

    # Press Enter
    input_box.press("Enter")

    # msg-user appears with text
    user_bubble = page.locator('[data-testid="msg-user"]').first
    expect(user_bubble).to_be_visible()
    expect(user_bubble).to_have_text(test_text)

    # Input cleared and send button disabled
    expect(input_box).to_have_value("")
    expect(send_btn).to_be_disabled()


# A-05: Single assistant bubble holding transcripts in order
def test_a05_single_assistant_bubble_holds_all_transcripts_in_order(page: Page, server_url: str):
    """A-05: After a scripted turn, a single `msg-assistant` holds all of that turn's `transcript` texts, in order."""
    page.goto(server_url)
    expect(page.locator('[data-testid="status"]')).to_have_attribute("data-state", "ready", timeout=10000)

    match_text, transcript_texts = find_turn_with_multi_transcript()

    input_box = page.locator('[data-testid="composer-input"]')
    input_box.fill(match_text)
    input_box.press("Enter")

    # Wait for turn completion
    expect(page.locator('[data-testid="status"]')).to_have_attribute("data-state", "ready", timeout=15000)

    # Exactly one msg-assistant bubble
    assistant_bubbles = page.locator('[data-testid="msg-assistant"]')
    expect(assistant_bubbles).to_have_count(1)

    bubble_text = assistant_bubbles.first.inner_text()
    last_idx = -1
    for chunk in transcript_texts:
        cleaned_chunk = chunk.strip()
        if not cleaned_chunk:
            continue
        idx = bubble_text.find(cleaned_chunk)
        assert idx != -1, f"Expected transcript chunk '{cleaned_chunk}' in assistant bubble '{bubble_text}'"
        assert idx >= last_idx, f"Chunk '{cleaned_chunk}' out of order in assistant bubble '{bubble_text}'"
        last_idx = idx


# A-06: Status speaking during audio and ready after turn complete
def test_a06_status_speaking_during_audio_and_ready_after_turn_complete(page: Page, server_url: str):
    """A-06: During an `audio` event, `status` is `speaking` (`data-state="speaking"`).
    After `turn_complete`, it returns to `ready`.
    """
    page.goto(server_url)
    status = page.locator('[data-testid="status"]')
    expect(status).to_have_attribute("data-state", "ready", timeout=10000)

    match_text, _ = find_turn_with_audio()

    input_box = page.locator('[data-testid="composer-input"]')
    input_box.fill(match_text)
    input_box.press("Enter")

    # Status reaches speaking during audio playback
    expect(status).to_have_attribute("data-state", "speaking", timeout=8000)

    # Status returns to ready after completion
    expect(status).to_have_attribute("data-state", "ready", timeout=15000)


# A-07: Delayed tool progress note until card arrives
def test_a07_delayed_tool_shows_progress_note_until_card_appears(page: Page, server_url: str):
    """A-07: A `tool_call` with `delay_ms >= 1000` shows a `progress` note with the tool's
    progress text (§11) until the card appears.
    """
    page.goto(server_url)
    expect(page.locator('[data-testid="status"]')).to_have_attribute("data-state", "ready", timeout=10000)

    match_text, tool_name, delay_ms, event = find_turn_with_delayed_tool(min_delay_ms=1000)
    args = event.get("args", {})
    lang = args.get("lang", "en")
    is_ja = lang.startswith("ja")

    progress_map_en = {
        "weather": "Checking the weather…",
        "web_search": "Searching the web…",
        "maps_search": "Searching maps…",
        "maps_route": "Finding a route…",
        "place_info": "Looking up the place…",
        "image_search": "Searching images…",
        "youtube_search": "Searching videos…",
    }
    progress_map_ja = {
        "weather": "天気を確認しています…",
        "web_search": "ウェブを検索しています…",
        "maps_search": "地図を検索しています…",
        "maps_route": "ルートを検索しています…",
        "place_info": "場所を調べています…",
        "image_search": "画像を検索しています…",
        "youtube_search": "動画を検索しています…",
    }
    expected_text = (progress_map_ja if is_ja else progress_map_en).get(tool_name, "Checking")

    input_box = page.locator('[data-testid="composer-input"]')
    input_box.fill(match_text)
    input_box.press("Enter")

    # Progress note visible during execution
    progress = page.locator('[data-testid="progress"]')
    expect(progress).to_be_visible(timeout=3000)
    expect(progress).to_contain_text(expected_text)

    # Card arrives
    card = page.locator(f'[data-testid="card"][data-card-type="{tool_name}"]')
    expect(card).to_be_visible(timeout=delay_ms + 5000)

    # Progress note is gone
    expect(progress).not_to_be_visible()


# A-08: Weather card layout, temperatures, hourly, daily, sunrise/sunset
def test_a08_weather_card_components_and_metrics(page: Page, server_url: str):
    """A-08: A weather `tool_call` renders a `card[data-card-type=weather]` showing:
    the location, the rounded current temperature (e.g. `20°`), the condition,
    "Feels like", 12 hourly entries (`wx-hour`), 5 daily rows (`wx-day`) that start
    with "Today" and "Tomorrow" and show high / low, and sunrise and sunset.
    """
    page.goto(server_url)
    expect(page.locator('[data-testid="status"]')).to_have_attribute("data-state", "ready", timeout=10000)

    match_text, event = find_turn_by_tool("weather", lang="en")
    result = event.get("result", {})
    expected_location = result.get("location", "")
    current = result.get("current", {})
    temp_c = current.get("temp_c")
    expected_temp_rounded = f"{round(temp_c)}°" if temp_c is not None else ""
    expected_condition = current.get("condition", "")
    feels_like_c = current.get("feels_like_c")
    expected_feels_rounded = f"{round(feels_like_c)}°" if feels_like_c is not None else ""
    sunrise = result.get("sunrise")
    sunset = result.get("sunset")

    input_box = page.locator('[data-testid="composer-input"]')
    input_box.fill(match_text)
    input_box.press("Enter")

    # Wait for weather card
    card = page.locator('[data-testid="card"][data-card-type="weather"]')
    expect(card).to_be_visible(timeout=10000)
    card_text = card.inner_text()

    # 1. Location
    assert expected_location in card_text, f"Location '{expected_location}' not in card: {card_text}"

    # 2. Rounded current temperature
    assert expected_temp_rounded in card_text, f"Rounded temp '{expected_temp_rounded}' not in card: {card_text}"

    # 3. Condition
    condition_map = {
        "clear": "Clear",
        "partly_cloudy": "Partly cloudy",
        "cloudy": "Cloudy",
        "fog": "Fog",
        "drizzle": "Drizzle",
        "rain": "Rain",
        "snow": "Snow",
        "thunderstorm": "Thunderstorm",
    }
    cond_text = condition_map.get(expected_condition, expected_condition)
    assert cond_text.lower() in card_text.lower(), f"Condition '{cond_text}' not in card: {card_text}"

    # 4. Feels like
    assert "Feels like" in card_text, f"'Feels like' not found in card: {card_text}"
    if expected_feels_rounded:
        assert expected_feels_rounded in card_text, f"Feels like temp '{expected_feels_rounded}' not in card"

    # 5. 12 hourly entries
    hourly_items = card.locator('[data-testid="wx-hour"]')
    assert hourly_items.count() == 12, f"Expected 12 hourly items, got {hourly_items.count()}"

    # 6. 5 daily rows starting with Today and Tomorrow and showing high / low
    daily_items = card.locator('[data-testid="wx-day"]')
    assert daily_items.count() == 5, f"Expected 5 daily rows, got {daily_items.count()}"

    row0_text = daily_items.nth(0).inner_text()
    row1_text = daily_items.nth(1).inner_text()
    assert "Today" in row0_text, f"Row 0 should start with 'Today', got '{row0_text}'"
    assert "Tomorrow" in row1_text, f"Row 1 should start with 'Tomorrow', got '{row1_text}'"

    for i in range(5):
        day_text = daily_items.nth(i).inner_text()
        assert "°" in day_text, f"Daily row {i} should show temperatures with °, got '{day_text}'"

    # 7. Sunrise and sunset
    if sunrise:
        assert f"Sunrise {sunrise}" in card_text or sunrise in card_text, f"Sunrise '{sunrise}' not in card"
    if sunset:
        assert f"Sunset {sunset}" in card_text or sunset in card_text, f"Sunset '{sunset}' not in card"


# A-09: Weather card Japanese localization
def test_a09_japanese_weather_card_localization(page: Page, server_url: str):
    """A-09: The same weather result with `lang: "ja"` shows 今日, 明日 and 体感."""
    page.goto(server_url)
    expect(page.locator('[data-testid="status"]')).to_have_attribute("data-state", "ready", timeout=10000)

    match_text, event = find_turn_by_tool("weather", lang="ja")
    result = event.get("result", {})
    expected_location = result.get("location", "")
    feels_like_c = result.get("current", {}).get("feels_like_c")
    expected_feels_rounded = f"{round(feels_like_c)}°" if feels_like_c is not None else ""

    input_box = page.locator('[data-testid="composer-input"]')
    input_box.fill(match_text)
    input_box.press("Enter")

    # Wait for weather card
    card = page.locator('[data-testid="card"][data-card-type="weather"]')
    expect(card).to_be_visible(timeout=10000)
    card_text = card.inner_text()

    # Required strings in A-09: 今日, 明日, 体感
    assert "今日" in card_text, f"'今日' not found in Japanese weather card: {card_text}"
    assert "明日" in card_text, f"'明日' not found in Japanese weather card: {card_text}"
    assert "体感" in card_text, f"'体感' not found in Japanese weather card: {card_text}"

    # Specifically verify daily rows
    daily_items = card.locator('[data-testid="wx-day"]')
    assert daily_items.count() == 5, f"Expected 5 daily rows, got {daily_items.count()}"
    assert "今日" in daily_items.nth(0).inner_text(), (
        f"Row 0 must contain '今日', got '{daily_items.nth(0).inner_text()}'"
    )
    assert "明日" in daily_items.nth(1).inner_text(), (
        f"Row 1 must contain '明日', got '{daily_items.nth(1).inner_text()}'"
    )

    if expected_location:
        assert expected_location in card_text, f"Japanese location '{expected_location}' not in card"

    if expected_feels_rounded:
        assert f"体感 {expected_feels_rounded}" in card_text or expected_feels_rounded in card_text
