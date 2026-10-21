import re

from playwright.sync_api import Page, expect


def test_top_bar_clock_and_status(page: Page, server_url: str):
    page.goto(server_url)
    # Status reaches ready
    status = page.locator('[data-testid="status"]')
    expect(status).to_have_attribute("data-state", "ready", timeout=10000)
    expect(status.locator(".status-text")).to_have_text("Ready")

    # Clock updates and shows formatted date and time (e.g. "Wed, Oct 7 09:17")
    clock = page.locator("#clock")
    expect(clock).to_be_visible()
    clock_text = clock.inner_text().strip()
    pattern = r"^[A-Z][a-z]{2},\s[A-Z][a-z]{2}\s\d{1,2}\s\d{2}:\d{2}$"
    assert re.match(pattern, clock_text), f"Clock text '{clock_text}' does not match expected format"


def test_desktop_layout_and_starter_chips(page: Page, server_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server_url)
    page.wait_for_selector('[data-testid="status"][data-state="ready"]')

    feed = page.locator('[data-testid="feed"]')
    cards = page.locator('[data-testid="cards"]')
    expect(feed).to_be_visible()
    expect(cards).to_be_visible()

    # Feed on left, narrower than cards
    feed_box = feed.bounding_box()
    cards_box = cards.bounding_box()
    assert feed_box is not None and cards_box is not None
    assert feed_box["x"] < cards_box["x"]
    assert feed_box["width"] < cards_box["width"]

    # Starters container visible with >= 5 chips
    starters = page.locator('[data-testid="starters"]')
    expect(starters).to_be_visible()
    chips = page.locator('[data-testid="starter-chip"]')
    assert chips.count() >= 5

    # Check required categories in chips
    prompts = [chips.nth(i).get_attribute("data-prompt") or "" for i in range(chips.count())]
    prompts_text = " ".join(prompts).lower()
    assert "weather" in prompts_text
    assert "search" in prompts_text or "telescope" in prompts_text
    assert "ramen" in prompts_text or "place" in prompts_text
    assert "route" in prompts_text or "asakusa" in prompts_text
    assert "video" in prompts_text or "sushi" in prompts_text


def test_composer_empty_state_and_send(page: Page, server_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server_url)
    page.wait_for_selector('[data-testid="status"][data-state="ready"]')

    send_btn = page.locator('[data-testid="composer-send"]')
    input_box = page.locator('[data-testid="composer-input"]')

    # Send button disabled initially
    expect(send_btn).to_be_disabled()

    # Whitespace only keeps send disabled
    input_box.fill("   ")
    expect(send_btn).to_be_disabled()

    # Typing text enables send
    input_box.fill("hello")
    expect(send_btn).to_be_enabled()

    # Enter key sends text
    input_box.press("Enter")

    # msg-user bubble added
    user_bubble = page.locator('[data-testid="msg-user"]').first
    expect(user_bubble).to_be_visible()
    expect(user_bubble).to_have_text("hello")

    # Input cleared and send button disabled
    expect(input_box).to_have_value("")
    expect(send_btn).to_be_disabled()


def test_starter_chip_click_dispatches_prompt(page: Page, server_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server_url)
    page.wait_for_selector('[data-testid="status"][data-state="ready"]')

    first_chip = page.locator('[data-testid="starter-chip"]').first
    expected_prompt = first_chip.get_attribute("data-prompt")
    assert expected_prompt

    first_chip.click()

    user_bubble = page.locator('[data-testid="msg-user"]').first
    expect(user_bubble).to_be_visible()
    expect(user_bubble).to_have_text(expected_prompt)


def test_transcript_streaming_accumulation_and_progress(page: Page, server_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server_url)
    page.wait_for_selector('[data-testid="status"][data-state="ready"]')

    input_box = page.locator('[data-testid="composer-input"]')
    # Send "weather in kyoto" which has 1.5s tool delay in script.json
    input_box.fill("What is the weather in kyoto?")
    page.locator('[data-testid="composer-send"]').click()

    # Progress note appears while tool is running
    progress = page.locator('[data-testid="progress"]')
    expect(progress).to_be_visible(timeout=3000)
    expect(progress).to_contain_text("Checking the weather…")

    # Card appears when tool finishes
    card = page.locator('[data-testid="card"][data-card-type="weather"]')
    expect(card).to_be_visible(timeout=5000)

    # Progress note is removed/replaced once card arrives
    expect(progress).not_to_be_visible()

    # Exactly one assistant bubble holding all transcript chunks
    assistant_bubbles = page.locator('[data-testid="msg-assistant"]')
    expect(assistant_bubbles).to_have_count(1)
    text = assistant_bubbles.first.inner_text()
    assert "Let me check Kyoto." in text
    assert "It is 20 degrees in Kyoto." in text


def test_narrow_viewport_inline_card(page: Page, server_url: str):
    # Narrow viewport: 390x844 (R-LAYOUT-3, A-03)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(server_url)
    page.wait_for_selector('[data-testid="status"][data-state="ready"]')

    # Right pane is hidden on narrow view
    cards_pane = page.locator("#cards-pane")
    expect(cards_pane).not_to_be_visible()

    input_box = page.locator('[data-testid="composer-input"]')
    input_box.fill("weather in kyoto")
    input_box.press("Enter")

    # Card appears inside #feed after turn's bubbles
    feed_card = page.locator('[data-testid="feed"] [data-testid="card"]')
    expect(feed_card).to_be_visible(timeout=5000)
    expect(feed_card).to_have_attribute("data-card-type", "weather")


def test_composer_status_transitions_and_speaking(page: Page, server_url: str):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(server_url)
    page.wait_for_selector('[data-testid="status"][data-state="ready"]')

    status = page.locator('[data-testid="status"]')

    input_box = page.locator('[data-testid="composer-input"]')
    input_box.fill("weather in kyoto")
    page.locator('[data-testid="composer-send"]').click()

    # Status transitions to thinking
    expect(status).to_have_attribute("data-state", "thinking")

    # During audio event in script.json, status is speaking (A-06)
    expect(status).to_have_attribute("data-state", "speaking", timeout=5000)

    # After turn_complete, returns to ready
    expect(status).to_have_attribute("data-state", "ready", timeout=5000)
