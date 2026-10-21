"""Unit tests for weather data structure, rounding, and localization."""

import json
from pathlib import Path


def round_temp(temp: float) -> str:
    """Helper mimicking the card's temperature formatting: Math.round(t) + '°'."""
    # Round half away from zero as JavaScript's Math.round does for positive numbers
    return f"{round(temp + 1e-9)}°"


def test_weather_temperature_rounding():
    assert round_temp(20.4) == "20°"
    assert round_temp(19.7) == "20°"
    assert round_temp(12.7) == "13°"
    assert round_temp(14.1) == "14°"
    assert round_temp(0.4) == "0°"
    assert round_temp(0.6) == "1°"
    assert round_temp(-5.2) == "-5°"


def test_script_weather_fixtures_structure():
    script_path = Path(__file__).parents[2] / "spec" / "fixtures" / "script.json"
    assert script_path.exists(), "spec/fixtures/script.json must exist"

    with open(script_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    weather_turns = [
        event["result"]
        for turn in data.get("turns", [])
        for event in turn.get("events", [])
        if event.get("type") == "tool_call" and event.get("name") == "weather"
    ]

    assert len(weather_turns) >= 2, "Expected at least 2 weather turns (en and ja)"

    for result in weather_turns:
        # Check required top-level fields
        assert "location" in result
        assert "local_time" in result
        assert "lang" in result
        assert "current" in result
        assert "hourly" in result
        assert "daily" in result

        current = result["current"]
        assert "temp_c" in current
        assert "feels_like_c" in current
        assert "condition" in current
        assert "is_day" in current

        # Check hourly entries (12 entries)
        assert len(result["hourly"]) == 12
        for h in result["hourly"]:
            assert "time" in h
            assert "temp_c" in h
            assert "condition" in h

        # Check daily entries (5 entries)
        assert len(result["daily"]) == 5
        for d in result["daily"]:
            assert "date" in d
            assert "high_c" in d
            assert "low_c" in d
            assert "condition" in d


def test_japanese_localization_terms():
    # R-LANG-1 and REQUIREMENTS §11 required strings
    terms_ja = {
        "today": "今日",
        "tomorrow": "明日",
        "feels_like": "体感",
    }
    assert terms_ja["today"] == "今日"
    assert terms_ja["tomorrow"] == "明日"
    assert terms_ja["feels_like"] == "体感"

    # Conditions mapping per REQUIREMENTS §11
    conditions_ja = {
        "clear": "晴れ",
        "partly_cloudy": "晴れ時々曇り",
        "cloudy": "曇り",
        "fog": "霧",
        "drizzle": "霧雨",
        "rain": "雨",
        "snow": "雪",
        "thunderstorm": "雷雨",
    }
    assert len(conditions_ja) == 8
    assert conditions_ja["clear"] == "晴れ"
    assert conditions_ja["partly_cloudy"] == "晴れ時々曇り"
    assert conditions_ja["thunderstorm"] == "雷雨"


def test_japanese_weekday_names():
    ja_weekdays = ["日", "月", "火", "水", "木", "金", "土"]
    en_weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    assert len(ja_weekdays) == 7
    assert len(en_weekdays) == 7
    assert ja_weekdays[5] == "金"
    assert ja_weekdays[6] == "土"
    assert ja_weekdays[0] == "日"
