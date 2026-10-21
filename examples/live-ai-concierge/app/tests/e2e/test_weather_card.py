"""End-to-end tests for the Weather Card component rendering and localization."""

import json
from pathlib import Path


def load_fixture_turn(match_text: str):
    script_path = Path(__file__).parents[2] / "spec" / "fixtures" / "script.json"
    with open(script_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for turn in data.get("turns", []):
        if turn.get("match") == match_text:
            for event in turn.get("events", []):
                if event.get("type") == "tool_call" and event.get("name") == "weather":
                    return event["result"]
    raise ValueError(f"Weather fixture matching '{match_text}' not found")


def test_weather_card_english(page, server_url):
    page.goto(server_url)
    en_fixture = load_fixture_turn("weather in kyoto")

    # Call renderWeatherCard in page context
    result = page.evaluate(
        """async (fixture) => {
            const module = await import('/static/js/cards/weather.js');
            const container = document.getElementById('cards-pane');
            const card = module.renderWeatherCard(fixture, container);
            return !!card;
        }""",
        en_fixture,
    )
    assert result is True, "renderWeatherCard should return a truthy card element"

    card = page.locator('.card.weather-card[data-testid="card"][data-card-type="weather"]')
    assert card.count() == 1, "Weather card must have data-testid='card' and data-card-type='weather'"

    # Check header
    card_text = card.inner_text()
    assert "Kyoto" in card_text
    assert "18:15" in card_text

    # Check current temperature and condition
    assert "20°" in card_text
    assert "Clear" in card_text
    assert "Feels like 19°" in card_text

    # Check 12 hourly entries
    hourly_items = card.locator('[data-testid="wx-hour"]')
    assert hourly_items.count() == 12
    # Verify first hourly item contains time and rounded temp
    first_hour = hourly_items.first.inner_text()
    assert "18:00" in first_hour
    assert "20°" in first_hour

    # Check 5 daily rows
    daily_items = card.locator('[data-testid="wx-day"]')
    assert daily_items.count() == 5
    assert "Today" in daily_items.nth(0).inner_text()
    assert "Tomorrow" in daily_items.nth(1).inner_text()
    # Check high and low temps for row 0 (high: 23.4 -> 23°, low: 16.4 -> 16°)
    today_text = daily_items.nth(0).inner_text()
    assert "23°" in today_text
    assert "16°" in today_text

    # Check footer metrics
    assert "Wind 6 km/h" in card_text
    assert "Humidity 60%" in card_text
    assert "Sunrise 05:56" in card_text
    assert "Sunset 17:33" in card_text


def test_weather_card_japanese_localization(page, server_url):
    page.goto(server_url)
    ja_fixture = load_fixture_turn("京都の天気")

    page.evaluate(
        """async (fixture) => {
            const module = await import('/static/js/cards/weather.js');
            const container = document.getElementById('cards-pane');
            module.renderWeatherCard(fixture, container);
        }""",
        ja_fixture,
    )

    card = page.locator('.card.weather-card[data-testid="card"][data-card-type="weather"]')
    card_text = card.inner_text()

    # Required strings in A-09 and R-LANG-1
    assert "京都" in card_text
    assert "晴れ" in card_text
    assert "体感 19°" in card_text

    daily_items = card.locator('[data-testid="wx-day"]')
    assert daily_items.count() == 5
    assert "今日" in daily_items.nth(0).inner_text()
    assert "明日" in daily_items.nth(1).inner_text()
    # Rows 2..4 should have Japanese weekday names (金, 土, 日)
    assert "金" in daily_items.nth(2).inner_text()
    assert "土" in daily_items.nth(3).inner_text()
    assert "日" in daily_items.nth(4).inner_text()


def test_weather_card_sky_conditions(page, server_url):
    page.goto(server_url)

    conditions_to_test = [
        {"condition": "clear", "is_day": True, "expected_class": "sky-clear-day"},
        {"condition": "clear", "is_day": False, "expected_class": "sky-clear-night"},
        {"condition": "cloudy", "is_day": True, "expected_class": "sky-cloudy"},
        {"condition": "rain", "is_day": True, "expected_class": "sky-rain"},
        {"condition": "snow", "is_day": True, "expected_class": "sky-snow"},
        {"condition": "thunderstorm", "is_day": True, "expected_class": "sky-thunderstorm"},
    ]

    for item in conditions_to_test:
        class_found = page.evaluate(
            """async (item) => {
                const module = await import('/static/js/cards/weather.js');
                const dummyData = {
                    location: "TestCity",
                    local_time: "12:00",
                    lang: "en",
                    current: {
                        temp_c: 15,
                        feels_like_c: 14,
                        condition: item.condition,
                        is_day: item.is_day
                    },
                    hourly: [],
                    daily: []
                };
                const card = module.renderWeatherCard(dummyData);
                const bg = card.querySelector('.weather-sky-bg');
                return bg ? bg.className : "";
            }""",
            item,
        )
        assert item["expected_class"] in class_found, f"Expected {item['expected_class']} in {class_found}"


def test_weather_card_reduced_motion(page, server_url):
    page.emulate_media(reduced_motion="reduce")
    page.goto(server_url)

    is_reduced = page.evaluate(
        """async () => {
            const module = await import('/static/js/cards/weather.js');
            const dummyData = {
                location: "TestCity",
                local_time: "12:00",
                lang: "en",
                current: { temp_c: 15, feels_like_c: 14, condition: "clear", is_day: true },
                hourly: [],
                daily: []
            };
            const card = module.renderWeatherCard(dummyData);
            return card.classList.contains('reduced-motion');
        }"""
    )
    assert is_reduced is True, "Weather card must have 'reduced-motion' class when reduced motion is preferred"

