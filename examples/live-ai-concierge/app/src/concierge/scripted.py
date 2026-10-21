import json
import math
import os
import pathlib
import struct
from typing import Any


class ScriptEngine:
    """Replay engine for scripted mode (CONCIERGE_SCRIPT)."""

    def __init__(self, script_path: str | pathlib.Path | None = None) -> None:
        if script_path is None:
            script_path = os.environ.get("CONCIERGE_SCRIPT")
        if not script_path:
            raise ValueError("Script path not provided and CONCIERGE_SCRIPT is not set")

        path = pathlib.Path(script_path)
        with open(path, "r", encoding="utf-8") as f:
            self.script: dict[str, Any] = json.load(f)

        self.turns: list[dict[str, Any]] = self.script.get("turns", [])
        self.fallback: list[dict[str, Any]] = self.script.get("fallback", [])

    def match_turn(self, text: str) -> dict[str, Any] | None:
        lowered = text.lower()
        for turn in self.turns:
            match_str = turn.get("match", "")
            if match_str in lowered:
                return turn
        return None

    def get_events_for_message(self, text: str) -> list[dict[str, Any]]:
        matched = self.match_turn(text)
        if matched is not None:
            return list(matched.get("events", []))
        return list(self.fallback)

    @staticmethod
    def pcm_sample_count(ms: int, sample_rate: int = 24000) -> int:
        """Calculates sample count for ms milliseconds at sample_rate."""
        return int(sample_rate * ms / 1000)

    @staticmethod
    def pcm_byte_length(ms: int, sample_rate: int = 24000, bytes_per_sample: int = 2) -> int:
        """Calculates byte length for 16-bit mono PCM of ms duration."""
        return ScriptEngine.pcm_sample_count(ms, sample_rate) * bytes_per_sample

    @staticmethod
    def pcm_duration_ms(byte_count: int, sample_rate: int = 24000, bytes_per_sample: int = 2) -> float:
        """Calculates duration in ms from byte count for 16-bit mono PCM."""
        return (byte_count / (sample_rate * bytes_per_sample)) * 1000.0

    @staticmethod
    def generate_audio_pcm(ms: int, sample_rate: int = 24000) -> bytes:
        """Generates 16-bit mono signed PCM audio samples for ms milliseconds."""
        sample_count = ScriptEngine.pcm_sample_count(ms, sample_rate)
        freq = 440.0
        amplitude = 2000
        pcm = bytearray()
        for i in range(sample_count):
            val = int(amplitude * math.sin(2 * math.pi * freq * i / sample_rate))
            val = max(-32768, min(32767, val))
            pcm.extend(struct.pack("<h", val))
        return bytes(pcm)
