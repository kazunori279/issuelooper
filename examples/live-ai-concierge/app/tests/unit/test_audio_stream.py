import struct

import pytest

from concierge.scripted import ScriptEngine


def test_audio_duration_to_samples():
    """Verify duration-to-sample calculations at 24 kHz."""
    # 24 kHz = 24,000 samples per second
    assert ScriptEngine.pcm_sample_count(0, sample_rate=24000) == 0
    assert ScriptEngine.pcm_sample_count(10, sample_rate=24000) == 240
    assert ScriptEngine.pcm_sample_count(100, sample_rate=24000) == 2400
    assert ScriptEngine.pcm_sample_count(500, sample_rate=24000) == 12000
    assert ScriptEngine.pcm_sample_count(1000, sample_rate=24000) == 24000
    assert ScriptEngine.pcm_sample_count(1500, sample_rate=24000) == 36000
    assert ScriptEngine.pcm_sample_count(6000, sample_rate=24000) == 144000


def test_audio_byte_length_calculations():
    """Verify PCM frame byte length (16-bit mono = 2 bytes per sample)."""
    # 24000 samples/s * 0.5s * 2 bytes = 24000 bytes
    assert ScriptEngine.pcm_byte_length(500, sample_rate=24000) == 24000
    assert ScriptEngine.pcm_byte_length(1000, sample_rate=24000) == 48000
    assert ScriptEngine.pcm_byte_length(0, sample_rate=24000) == 0


def test_pcm_duration_from_bytes():
    """Verify converting byte count back to duration in milliseconds."""
    sample_rate = 24000
    for ms in [10, 50, 100, 250, 500, 1000, 1500]:
        byte_len = ScriptEngine.pcm_byte_length(ms, sample_rate=sample_rate)
        calc_ms = ScriptEngine.pcm_duration_ms(byte_len, sample_rate=sample_rate)
        assert calc_ms == pytest.approx(ms, rel=1e-5)


def test_pcm_frame_sizing_and_structure():
    """Verify that generated PCM data matches 16-bit signed linear PCM frame sizing."""
    sample_rate = 24000
    pcm = ScriptEngine.generate_audio_pcm(ms=250, sample_rate=sample_rate)

    # Frame size for 16-bit mono is 2 bytes
    assert len(pcm) % 2 == 0
    num_samples = len(pcm) // 2
    assert num_samples == ScriptEngine.pcm_sample_count(250, sample_rate=sample_rate)

    # All samples must unpack into valid signed 16-bit integers
    samples = struct.unpack(f"<{num_samples}h", pcm)
    assert len(samples) == num_samples
    for sample in samples:
        assert -32768 <= sample <= 32767

    # Ensure audio is non-silent and waveform varies
    assert any(s != 0 for s in samples)
    assert max(samples) > 0
    assert min(samples) < 0


def test_audio_chunking_stream():
    """Verify chunking a PCM stream into discrete network frames."""
    total_ms = 1000
    chunk_ms = 100
    sample_rate = 24000

    full_pcm = ScriptEngine.generate_audio_pcm(ms=total_ms, sample_rate=sample_rate)
    chunk_size = ScriptEngine.pcm_byte_length(chunk_ms, sample_rate=sample_rate)

    chunks = [full_pcm[i : i + chunk_size] for i in range(0, len(full_pcm), chunk_size)]
    assert len(chunks) == total_ms // chunk_ms

    reconstructed = b"".join(chunks)
    assert reconstructed == full_pcm
    for chunk in chunks:
        assert len(chunk) == chunk_size
        assert len(chunk) % 2 == 0
