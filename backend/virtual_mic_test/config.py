from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ai_interpreter.config import DeviceSelector


class TestConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WebConfig:
    host: str
    port: int
    open_browser: bool


@dataclass(frozen=True, slots=True)
class TestAudioConfig:
    microphone: DeviceSelector
    virtual_playback: DeviceSelector
    virtual_recording: DeviceSelector
    signal_threshold_dbfs: float
    route_tone_hz: int
    route_tone_seconds: float
    route_record_tail_ms: int
    cloud_max_duration_seconds: int
    output_buffer_ms: int


@dataclass(frozen=True, slots=True)
class TestSettings:
    config_path: Path
    interpreter_config_path: Path
    web: WebConfig
    audio: TestAudioConfig
    output_directory: Path
    max_recordings: int


def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise TestConfigurationError(f"配置项 {key} 必须是对象")
    return value


def _string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TestConfigurationError(f"配置项 {key} 必须是非空字符串")
    return value.strip()


def _bool(parent: dict[str, Any], key: str, default: bool) -> bool:
    value = parent.get(key, default)
    if not isinstance(value, bool):
        raise TestConfigurationError(f"配置项 {key} 必须是布尔值")
    return value


def _number(parent: dict[str, Any], key: str, default: float) -> float:
    value = parent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TestConfigurationError(f"配置项 {key} 必须是数字")
    return float(value)


def _positive_int(parent: dict[str, Any], key: str, default: int) -> int:
    value = parent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TestConfigurationError(f"配置项 {key} 必须是正整数")
    return value


def _selector(raw: dict[str, Any], path: str, *, allow_default: bool) -> DeviceSelector:
    host_api = str(raw.get("host_api", "WASAPI")).strip()
    name = str(raw.get("name", "")).strip()
    use_default = raw.get("use_default", False)
    if host_api.upper() != "WASAPI":
        raise TestConfigurationError(f"{path}.host_api 只允许 WASAPI")
    if not isinstance(use_default, bool):
        raise TestConfigurationError(f"{path}.use_default 必须是布尔值")
    if use_default and not allow_default:
        raise TestConfigurationError(f"{path} 不允许使用默认设备")
    if not use_default and not name:
        raise TestConfigurationError(f"{path}.name 不能为空")
    return DeviceSelector("WASAPI", name, use_default)


def load_test_settings(path: str | Path) -> TestSettings:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TestConfigurationError(f"无法读取测试台配置：{config_path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise TestConfigurationError("schema_version 必须为 1")

    interpreter_value = _string(raw, "interpreter_config")
    interpreter_path = Path(interpreter_value)
    if not interpreter_path.is_absolute():
        interpreter_path = (config_path.parent / interpreter_path).resolve()
    if not interpreter_path.is_file():
        raise TestConfigurationError(f"主程序配置不存在：{interpreter_path}")

    web_raw = _mapping(raw, "web")
    host = _string(web_raw, "host")
    if host not in {"127.0.0.1", "localhost"}:
        raise TestConfigurationError("web.host 只允许 127.0.0.1 或 localhost")
    port = _positive_int(web_raw, "port", 8776)
    if port > 65535:
        raise TestConfigurationError("web.port 必须在 1-65535")

    devices_raw = _mapping(raw, "devices")
    audio_raw = _mapping(raw, "audio")
    output_raw = _mapping(raw, "outputs")
    output_directory = Path(_string(output_raw, "directory"))
    if not output_directory.is_absolute():
        output_directory = (config_path.parent / output_directory).resolve()

    threshold = _number(audio_raw, "signal_threshold_dbfs", -48.0)
    if not -90 <= threshold <= -6:
        raise TestConfigurationError("audio.signal_threshold_dbfs 必须在 -90 到 -6 dBFS")
    tone_seconds = _number(audio_raw, "route_tone_seconds", 1.0)
    if not 0.2 <= tone_seconds <= 5:
        raise TestConfigurationError("audio.route_tone_seconds 必须在 0.2-5 秒")

    return TestSettings(
        config_path=config_path,
        interpreter_config_path=interpreter_path,
        web=WebConfig(host, port, _bool(web_raw, "open_browser", True)),
        audio=TestAudioConfig(
            microphone=_selector(
                _mapping(devices_raw, "microphone"), "devices.microphone", allow_default=True
            ),
            virtual_playback=_selector(
                _mapping(devices_raw, "virtual_playback"),
                "devices.virtual_playback",
                allow_default=False,
            ),
            virtual_recording=_selector(
                _mapping(devices_raw, "virtual_recording"),
                "devices.virtual_recording",
                allow_default=False,
            ),
            signal_threshold_dbfs=threshold,
            route_tone_hz=_positive_int(audio_raw, "route_tone_hz", 1000),
            route_tone_seconds=tone_seconds,
            route_record_tail_ms=_positive_int(audio_raw, "route_record_tail_ms", 800),
            cloud_max_duration_seconds=_positive_int(
                audio_raw, "cloud_max_duration_seconds", 30
            ),
            output_buffer_ms=_positive_int(audio_raw, "output_buffer_ms", 5000),
        ),
        output_directory=output_directory,
        max_recordings=_positive_int(output_raw, "max_recordings", 20),
    )
