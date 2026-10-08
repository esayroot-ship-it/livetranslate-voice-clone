from __future__ import annotations

import wave
from pathlib import Path

import yaml

from bailian_voice_clone.translate_test import (
    _session_update,
    _write_output,
    load_translation_test_settings,
)


def _config(tmp_path: Path) -> Path:
    audio = tmp_path / "input.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24_000)
        stream.writeframes(b"\x00\x00" * 24_000 * 4)
    raw = {
        "schema_version": 1,
        "aliyun": {
            "api_key": "secret",
            "workspace_id": "ws-test",
            "region": "cn-beijing",
            "request_timeout_seconds": 30,
        },
        "service": {
            "enrollment_model": "qwen-voice-enrollment",
            "target_model": "qwen3.5-livetranslate-flash-realtime",
            "preferred_name": "test_voice",
        },
        "audio": {
            "file": str(audio),
            "url": "",
            "mime_type": "auto",
            "text": "测试",
            "language": "zh",
            "max_encoded_bytes": 10_000_000,
            "validate_wav_metadata": True,
        },
        "listing": {"page_index": 0, "page_size": 10},
        "output": {"format": "json", "show_request_id": True},
        "recording": {"prompt": "测试"},
        "translation_test": {
            "voice_id": "qwen-translate-vc-test-123",
            "input_file": str(audio),
            "output_file": str(tmp_path / "output.wav"),
            "source_language": "zh",
            "target_language": "en",
            "chunk_ms": 100,
            "realtime_pacing": True,
            "configure_timeout_seconds": 15,
            "finish_timeout_seconds": 60,
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_translation_test_uses_fixed_livetranslate_voice(tmp_path: Path) -> None:
    settings = load_translation_test_settings(_config(tmp_path))
    session = _session_update(settings)["session"]
    assert session["translation"] == {"language": "en"}
    assert session["voice"] == "qwen-translate-vc-test-123"
    assert session["voice_clone_options"] == {"frequency": "never"}
    assert session["sample_rate"] == 16_000


def test_output_pcm_is_saved_as_24k_mono_wav(tmp_path: Path) -> None:
    output = tmp_path / "output.wav"
    duration = _write_output(output, b"\x00\x00" * 24_000)
    with wave.open(str(output), "rb") as stream:
        assert stream.getframerate() == 24_000
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
    assert duration == 1.0
