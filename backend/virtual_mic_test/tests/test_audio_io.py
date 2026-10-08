from __future__ import annotations

import wave
from pathlib import Path

from virtual_mic_test.audio_io import WaveTeeSink


class FakeBuffer:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> int:
        self.data.extend(data)
        return 0

    def clear(self) -> int:
        size = len(self.data)
        self.data.clear()
        return size


def test_wave_tee_saves_cloud_audio_and_forwards_pcm(tmp_path: Path) -> None:
    path = tmp_path / "cloud.wav"
    buffer = FakeBuffer()
    events = []
    sink = WaveTeeSink(buffer, path, threshold_dbfs=-48, on_event=events.append)
    pcm = (10000).to_bytes(2, "little", signed=True) * 2400
    assert sink.write(pcm) == 0
    sink.close()
    assert bytes(buffer.data) == pcm
    assert events[-1]["state"] == "first_audio"
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == 24000
        assert wav.getnchannels() == 1
        assert wav.getnframes() == 2400
