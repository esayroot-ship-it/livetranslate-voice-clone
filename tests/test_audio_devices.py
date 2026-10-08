from __future__ import annotations

import numpy as np
import pytest

from ai_interpreter.audio import (
    DeviceManager,
    DeviceResolutionError,
    StreamingOutputConverter,
    _refresh_input_device,
    _refresh_output_device,
)
from ai_interpreter.models import CaptureIssue, DeviceDescriptor


def _device(index: int, host_api: str, *, inputs: int = 2, outputs: int = 0) -> DeviceDescriptor:
    return DeviceDescriptor(
        index=index,
        name="Shared Device",
        host_api=host_api,
        max_input_channels=inputs,
        max_output_channels=outputs,
        default_sample_rate=48000,
    )


def test_manual_device_resolution_ignores_non_wasapi_duplicates() -> None:
    selected = DeviceManager._unique_match(
        [
            _device(1, "MME"),
            _device(2, "Windows DirectSound"),
            _device(3, "Windows WASAPI"),
        ],
        "Shared Device",
        "input",
    )
    assert selected.index == 3


def test_manual_device_resolution_rejects_non_wasapi_only() -> None:
    with pytest.raises(DeviceResolutionError) as raised:
        DeviceManager._unique_match([_device(1, "MME")], "Shared Device", "input")
    assert raised.value.issue == CaptureIssue.DEVICE_NOT_FOUND


def test_refresh_loopback_uses_index_from_active_pyaudio_instance() -> None:
    class FakePyAudio:
        def get_loopback_device_info_generator(self):  # type: ignore[no-untyped-def]
            yield {
                "index": 41,
                "name": "Speaker [Loopback]",
                "hostApi": 0,
                "maxInputChannels": 2,
                "maxOutputChannels": 0,
                "defaultSampleRate": 48000,
                "isLoopbackDevice": True,
            }

        def get_host_api_info_by_index(self, _index: int) -> dict[str, str]:
            return {"name": "Windows WASAPI"}

    stale = DeviceDescriptor(
        index=17,
        name="Speaker [Loopback]",
        host_api="Windows WASAPI",
        max_input_channels=2,
        max_output_channels=0,
        default_sample_rate=48000,
        is_loopback=True,
    )
    refreshed = _refresh_input_device(FakePyAudio(), stale)
    assert refreshed.index == 41


def test_refresh_output_uses_index_from_active_pyaudio_instance() -> None:
    class FakePyAudio:
        def get_device_count(self) -> int:
            return 1

        def get_device_info_by_index(self, _index: int) -> dict[str, object]:
            return {
                "index": 52,
                "name": "CABLE Input",
                "hostApi": 0,
                "maxInputChannels": 0,
                "maxOutputChannels": 2,
                "defaultSampleRate": 48000,
            }

        def get_host_api_info_by_index(self, _index: int) -> dict[str, str]:
            return {"name": "Windows WASAPI"}

    stale = DeviceDescriptor(
        index=12,
        name="CABLE Input",
        host_api="Windows WASAPI",
        max_input_channels=0,
        max_output_channels=2,
        default_sample_rate=48000,
    )
    assert _refresh_output_device(FakePyAudio(), stale).index == 52


def test_output_converter_resamples_24khz_mono_to_48khz_stereo() -> None:
    source = np.arange(2400, dtype="<i2").tobytes()
    converter = StreamingOutputConverter(24000, 48000, 2)

    converted = np.frombuffer(converter.process_pcm16(source), dtype="<i2").reshape(-1, 2)

    assert 4500 <= len(converted) <= 5000
    assert np.array_equal(converted[:, 0], converted[:, 1])


def test_output_converter_matches_44100hz_cable_native_chunk_size() -> None:
    source = np.zeros(2400, dtype="<i2").tobytes()
    converter = StreamingOutputConverter(24000, 44100, 2)

    converted = np.frombuffer(converter.process_pcm16(source), dtype="<i2").reshape(-1, 2)

    assert len(converted) == 4410
