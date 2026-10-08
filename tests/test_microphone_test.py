from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

from ai_interpreter.microphone_test import CloneWaveSink, MicrophoneTestManager
from ai_interpreter.models import DeviceDescriptor, PushState, StatusEvent


def test_clone_wave_sink_writes_24khz_mono_pcm(tmp_path: Path) -> None:
    path = tmp_path / "clone.wav"
    pcm = (b"\x01\x00\xff\xff") * 120
    sink = CloneWaveSink(path)

    assert sink.write(pcm) == 0
    sink.close()

    with wave.open(str(path), "rb") as recording:
        assert recording.getframerate() == 24000
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.readframes(recording.getnframes()) == pcm


def test_recording_resolver_rejects_traversal_and_non_test_files(tmp_path: Path) -> None:
    config = tmp_path / "config" / "settings.local.yaml"
    config.parent.mkdir()
    manager = MicrophoneTestManager(config, lambda _event: None)
    manager.output_directory.mkdir(parents=True)
    valid = manager.output_directory / "microphone-test-20260816-000000-000.wav"
    valid.write_bytes(b"RIFF")
    (manager.output_directory / "other.wav").write_bytes(b"RIFF")

    assert manager.resolve_recording(valid.name) == valid
    assert manager.resolve_recording("other.wav") is None
    assert manager.resolve_recording("../settings.local.yaml") is None


def test_manager_starts_capture_only_after_cloud_streaming(monkeypatch, tmp_path: Path) -> None:
    events = []
    config = tmp_path / "config" / "settings.local.yaml"
    config.parent.mkdir()
    microphone = DeviceDescriptor(7, "Physical Mic", "Windows WASAPI", 2, 0, 48000)
    settings = SimpleNamespace(
        local_session=SimpleNamespace(modalities=("text", "audio"), voice_id="voice-test"),
        audio=SimpleNamespace(
            microphone=object(),
            output_sample_rate_hz=24000,
        ),
        queues=SimpleNamespace(input_chunks=20),
    )

    class FakeDevices:
        def resolve_microphone(self, _selector):
            return microphone

    class FakeCapture:
        def __init__(self, **_kwargs):
            self.ident = None
            self.started = False

        def start(self):
            self.started = True
            self.ident = 1

        def stop(self):
            pass

        def is_alive(self):
            return False

    class FakeCloud:
        def __init__(self, _session):
            self.started = False
            self.error = None

        def start(self):
            self.started = True

        def is_alive(self):
            return False

    monkeypatch.setattr("ai_interpreter.microphone_test.load_settings", lambda _path: settings)
    monkeypatch.setattr("ai_interpreter.microphone_test.DeviceManager", FakeDevices)
    monkeypatch.setattr("ai_interpreter.microphone_test.CaptureWorker", FakeCapture)
    monkeypatch.setattr(
        "ai_interpreter.microphone_test.LiveTranslateSession", lambda **_kw: object()
    )
    monkeypatch.setattr("ai_interpreter.microphone_test.SingleSessionThread", FakeCloud)

    manager = MicrophoneTestManager(config, events.append)
    result = manager.start(5)
    assert result["running"] is True
    assert manager._capture is not None
    assert manager._capture.started is False

    manager._on_status(
        StatusEvent(component="cloud_push", state=PushState.STREAMING.value, channel="local")
    )
    assert manager._capture.started is True

    stopped = manager.stop()
    assert stopped["running"] is False
    assert stopped["recording_url"] is None
    assert any(event.get("state") == "no_audio" for event in events)
