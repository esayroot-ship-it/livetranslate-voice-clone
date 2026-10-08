from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import SecretStr

from ai_interpreter.web_server import (
    ConfigUpdate,
    _public_config,
    _update_config,
    create_web_app,
    run_web_server,
)


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "settings.local.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "app": {"mode": "local_subtitle", "language_preset": "local_zh_remote_en"},
                "aliyun": {
                    "api_key": "existing-secret",
                    "region": "cn-beijing",
                    "workspace_id": "ws-test",
                    "model": "qwen3.5-livetranslate-flash-realtime",
                },
                "audio": {
                    "microphone": {"name": "", "use_default": True},
                    "remote_playback": {"name": "", "use_default": True},
                    "virtual_output": {"name": "CABLE Input"},
                    "input_sample_rate_hz": 16000,
                    "output_sample_rate_hz": 24000,
                    "chunk_ms": 100,
                    "silence_dbfs": -48,
                    "silence_report_after_ms": 3000,
                },
                "vad": {"threshold": 0.2, "silence_duration_ms": 500},
                "queues": {"input_chunks": 20},
                "subtitle": {
                    "show_source": True,
                    "show_translation": True,
                    "font_size_px": 28,
                    "max_segments": 50,
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _update(api_key: SecretStr | None) -> ConfigUpdate:
    return ConfigUpdate(
        api_key=api_key,
        region="cn-beijing",
        workspace_id="ws-test",
        language_preset="local_zh_remote_en",
        microphone_name="",
        microphone_use_default=True,
        remote_playback_name="",
        remote_playback_use_default=True,
        virtual_output_name="CABLE Input",
        input_chunk_ms=100,
        silence_dbfs=-48,
        silence_report_after_ms=3000,
        vad_threshold=0.2,
        vad_silence_ms=500,
        input_queue_chunks=20,
        subtitle_show_source=True,
        subtitle_show_translation=True,
        subtitle_font_size_px=28,
        subtitle_max_segments=50,
    )


def test_public_config_never_returns_api_key(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    result = _public_config(_config_file(tmp_path))
    assert result["api_key_configured"] is True
    assert result["api_key_source"] == "file"
    assert "api_key" not in result
    assert "existing-secret" not in repr(result)


def test_blank_key_preserves_existing_file_secret(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    path = _config_file(tmp_path)
    _update_config(path, _update(None))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["aliyun"]["api_key"] == "existing-secret"


def test_new_key_is_written_but_not_returned(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    path = _config_file(tmp_path)
    result = _update_config(path, _update(SecretStr("replacement-secret")))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["aliyun"]["api_key"] == "replacement-secret"
    assert "replacement-secret" not in repr(result)


def test_web_update_persists_full_duplex_voice_and_output_switch(tmp_path: Path) -> None:
    path = _config_file(tmp_path)
    update = _update(None)
    update.mode = "full_duplex"
    update.voice_id = "qwen-translate-vc-test"
    update.virtual_output_enabled = False
    result = _update_config(path, update)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["app"]["mode"] == "full_duplex"
    assert raw["voice"]["voice_id"] == "qwen-translate-vc-test"
    assert raw["audio"]["virtual_output"]["enabled"] is False
    assert result["virtual_output"]["enabled"] is False


def test_start_reuses_existing_interpreter_instead_of_binding_again(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    path = _config_file(tmp_path)
    monkeypatch.setattr("ai_interpreter.web_server._port_is_listening", lambda *_: True)
    monkeypatch.setattr(
        "ai_interpreter.web_server._existing_interpreter_is_healthy", lambda *_: True
    )
    assert run_web_server(path, no_browser=True) == 0


def test_microphone_test_routes_are_exposed(tmp_path: Path) -> None:
    app = create_web_app(_config_file(tmp_path))
    paths = {route.path for route in app.routes}
    assert "/api/microphone-test/start" in paths
    assert "/api/microphone-test/stop" in paths
    assert "/api/microphone-test/status" in paths
    assert "/api/microphone-test/audio/{name}" in paths
