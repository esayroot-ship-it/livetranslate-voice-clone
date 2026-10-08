from __future__ import annotations

import numpy as np

from ai_interpreter.audio import MicrophoneProcessor


def _pcm(value: int, samples: int = 160) -> bytes:
    return np.full(samples, value, dtype="<i2").tobytes()


def test_microphone_processor_applies_digital_gain() -> None:
    processor = MicrophoneProcessor(
        gain_db=6.0,
        gate_enabled=False,
        gate_dbfs=-52.0,
        gate_hold_ms=300,
        chunk_ms=100,
    )
    processed, _level, gated = processor.process(_pcm(1000))
    samples = np.frombuffer(processed, dtype="<i2")
    assert gated is False
    assert 1980 <= int(samples[0]) <= 2010


def test_microphone_noise_gate_preserves_hold_then_mutes_silence() -> None:
    processor = MicrophoneProcessor(
        gain_db=0.0,
        gate_enabled=True,
        gate_dbfs=-40.0,
        gate_hold_ms=200,
        chunk_ms=100,
    )
    muted, _level, gated = processor.process(_pcm(100))
    assert gated is True
    assert not any(muted)

    speech, _level, gated = processor.process(_pcm(10000))
    assert gated is False
    assert any(speech)
    assert processor.process(_pcm(100))[2] is False
    assert processor.process(_pcm(100))[2] is False
    muted, _level, gated = processor.process(_pcm(100))
    assert gated is True
    assert not any(muted)
