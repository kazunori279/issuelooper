import json
import struct

import pytest

from concierge.scripted import ScriptEngine


@pytest.fixture
def sample_script(tmp_path):
    data = {
        "turns": [
            {
                "match": "weather in kyoto",
                "events": [
                    {"type": "transcript", "text": "Let me check Kyoto."},
                    {
                        "type": "tool_call",
                        "name": "weather",
                        "args": {"location": "Kyoto", "lang": "en"},
                        "result": {"temp": 20},
                        "delay_ms": 1500,
                    },
                    {"type": "transcript", "text": "It is 20 degrees in Kyoto."},
                    {"type": "audio", "ms": 500},
                    {"type": "turn_complete"},
                ],
            },
            {
                "match": "hello",
                "events": [
                    {"type": "transcript", "text": "Hello there!"},
                    {"type": "turn_complete"},
                ],
            },
        ],
        "fallback": [
            {"type": "transcript", "text": "Sorry, I have no script for that."},
            {"type": "turn_complete"},
        ],
    }
    p = tmp_path / "script.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_script_matching(sample_script):
    engine = ScriptEngine(sample_script)
    turn = engine.match_turn("What is the Weather in Kyoto today?")
    assert turn is not None
    assert turn["events"][0]["text"] == "Let me check Kyoto."


def test_script_matching_order(sample_script):
    engine = ScriptEngine(sample_script)
    turn = engine.match_turn("hello weather in kyoto")
    # First match in the list should win
    assert turn is not None
    assert turn["match"] == "weather in kyoto"


def test_script_matching_none(sample_script):
    engine = ScriptEngine(sample_script)
    turn = engine.match_turn("Unknown question")
    assert turn is None


def test_script_fallback(sample_script):
    engine = ScriptEngine(sample_script)
    events = engine.get_events_for_message("unrecognized question")
    assert len(events) == 2
    assert events[0]["text"] == "Sorry, I have no script for that."
    assert events[1]["type"] == "turn_complete"


def test_script_env_var(sample_script, monkeypatch):
    monkeypatch.setenv("CONCIERGE_SCRIPT", sample_script)
    engine = ScriptEngine()
    turn = engine.match_turn("hello")
    assert turn is not None
    assert turn["events"][0]["text"] == "Hello there!"


def test_script_missing_env(monkeypatch):
    monkeypatch.delenv("CONCIERGE_SCRIPT", raising=False)
    with pytest.raises(ValueError, match="Script path not provided"):
        ScriptEngine()


def test_audio_pcm_generation(sample_script):
    engine = ScriptEngine(sample_script)
    # 24000 samples/sec * 0.1 sec * 2 bytes/sample (16-bit mono) = 4800 bytes
    pcm = engine.generate_audio_pcm(ms=100, sample_rate=24000)
    assert len(pcm) == 4800

    # Verify that the data unpacks to valid signed 16-bit integers
    samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
    assert len(samples) == 2400
    assert any(s != 0 for s in samples)


def test_audio_pcm_sample_rates(sample_script):
    engine = ScriptEngine(sample_script)
    # 16000 samples/sec * 0.5 sec * 2 bytes/sample = 16000 bytes
    pcm = engine.generate_audio_pcm(ms=500, sample_rate=16000)
    assert len(pcm) == 16000


def test_audio_pcm_zero_ms(sample_script):
    engine = ScriptEngine(sample_script)
    pcm = engine.generate_audio_pcm(ms=0, sample_rate=24000)
    assert len(pcm) == 0
