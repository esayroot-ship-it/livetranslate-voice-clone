from __future__ import annotations

from pathlib import Path

import yaml

from virtual_mic_test.config import load_test_settings


def test_config_resolves_main_config_and_output_directory(tmp_path: Path) -> None:
    main = tmp_path / "main.yaml"
    main.write_text("schema_version: 1\n", encoding="utf-8")
    config_dir = tmp_path / "station"
    config_dir.mkdir()
    path = config_dir / "settings.yaml"
    raw = {
        "schema_version": 1,
        "interpreter_config": "../main.yaml",
        "web": {"host": "127.0.0.1", "port": 8776, "open_browser": False},
        "devices": {
            "microphone": {"host_api": "WASAPI", "name": "", "use_default": True},
            "virtual_playback": {
                "host_api": "WASAPI",
                "name": "CABLE Input",
                "use_default": False,
            },
            "virtual_recording": {
                "host_api": "WASAPI",
                "name": "CABLE Output",
                "use_default": False,
            },
        },
        "audio": {
            "signal_threshold_dbfs": -48,
            "route_tone_hz": 1000,
            "route_tone_seconds": 1.0,
            "route_record_tail_ms": 800,
            "cloud_max_duration_seconds": 30,
            "output_buffer_ms": 5000,
        },
        "outputs": {"directory": "outputs", "max_recordings": 20},
    }
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    settings = load_test_settings(path)
    assert settings.interpreter_config_path == main
    assert settings.output_directory == config_dir / "outputs"
    assert settings.audio.virtual_playback.name == "CABLE Input"
