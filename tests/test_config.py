from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_interpreter.config import ConfigurationError, load_settings


def _write_config(
    tmp_path: Path,
    *,
    preset: str = "local_zh_remote_en",
    mode: str = "local_subtitle",
    api_key: str = "",
) -> Path:
    term_dir = tmp_path / "terminology"
    term_dir.mkdir()
    (term_dir / "zh_to_en.json").write_text(
        json.dumps({"人工智能": "Artificial Intelligence"}, ensure_ascii=False), encoding="utf-8"
    )
    (term_dir / "en_to_zh.json").write_text(
        json.dumps({"Artificial Intelligence": "人工智能"}, ensure_ascii=False), encoding="utf-8"
    )
    raw = {
        "schema_version": 1,
        "app": {"mode": mode, "language_preset": preset},
        "aliyun": {
            "api_key": api_key,
            "region": "cn-beijing",
            "workspace_id": "ws-test123",
            "model": "qwen3.5-livetranslate-flash-realtime",
        },
        "web": {"host": "127.0.0.1", "port": 8765, "open_browser": False},
        "subtitle": {
            "show_source": True,
            "show_translation": True,
            "font_size_px": 28,
            "max_segments": 50,
        },
        "voice": {"voice_id": "qwen-translate-vc-test", "clone_frequency": "never"},
        "audio": {
            "microphone": {"host_api": "WASAPI", "name": "", "use_default": True},
            "remote_playback": {"host_api": "WASAPI", "name": "", "use_default": True},
            "virtual_output": {
                "host_api": "WASAPI",
                "name": "CABLE Input",
                "use_default": False,
            },
            "input_sample_rate_hz": 16000,
            "output_sample_rate_hz": 24000,
            "chunk_ms": 100,
            "silence_dbfs": -48,
            "silence_report_after_ms": 3000,
        },
        "vad": {"type": "server_vad", "threshold": 0.2, "silence_duration_ms": 500},
        "queues": {"input_chunks": 20, "output_buffer_ms": 3000},
        "reconnect": {"delays_seconds": [1, 2, 4, 8, 15]},
        "terminology": {
            "zh_to_en": "terminology/zh_to_en.json",
            "en_to_zh": "terminology/en_to_zh.json",
            "max_file_bytes": 262144,
        },
        "logging": {
            "level": "INFO",
            "directory": "logs",
            "max_files": 5,
            "max_bytes_per_file": 10000,
        },
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_load_settings_builds_two_language_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret-for-test")
    settings = load_settings(_write_config(tmp_path))
    assert settings.local_session.source_language == "zh"
    assert settings.local_session.target_language == "en"
    assert settings.mode == "local_subtitle"
    assert settings.local_session.modalities == ("text",)
    assert settings.local_session.voice_id is None
    assert settings.remote_session.source_language == "en"
    assert settings.remote_session.target_language == "zh"
    assert settings.remote_session.modalities == ("text",)
    assert "Authorization" not in settings.aliyun.websocket_url


def test_reverse_preset_uses_reverse_terminology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret-for-test")
    settings = load_settings(_write_config(tmp_path, preset="local_en_remote_zh"))
    assert settings.local_session.terminology == {"Artificial Intelligence": "人工智能"}
    assert settings.remote_session.terminology == {"人工智能": "Artificial Intelligence"}


def test_missing_secret_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="DASHSCOPE_API_KEY"):
        load_settings(_write_config(tmp_path))


def test_api_key_can_be_loaded_from_local_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    settings = load_settings(_write_config(tmp_path, api_key="file-secret"))
    assert settings.aliyun.api_key == "file-secret"
    assert "file-secret" not in repr(settings.aliyun)


def test_environment_api_key_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "environment-secret")
    settings = load_settings(_write_config(tmp_path, api_key="file-secret"))
    assert settings.aliyun.api_key == "environment-secret"


def test_unknown_top_level_key_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret-for-test")
    path = _write_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["api_key"] = "must-not-be-accepted"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="未知顶层"):
        load_settings(path)


def test_placeholder_values_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret-for-test")
    path = _write_config(tmp_path, mode="full_duplex")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["voice"]["voice_id"] = "请替换voice"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="voice_id"):
        load_settings(path)


def test_full_duplex_can_receive_clone_audio_without_virtual_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret-for-test")
    path = _write_config(tmp_path, mode="full_duplex")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["audio"]["virtual_output"]["enabled"] = False
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    settings = load_settings(path)
    assert settings.audio.virtual_output_enabled is False
    assert settings.local_session.modalities == ("text", "audio")
