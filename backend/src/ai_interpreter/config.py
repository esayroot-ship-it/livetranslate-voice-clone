from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .models import Channel


class ConfigurationError(ValueError):
    """配置无效且不允许静默回退。"""


@dataclass(frozen=True, slots=True)
class DeviceSelector:
    host_api: str
    name: str
    use_default: bool


@dataclass(frozen=True, slots=True)
class AliyunSettings:
    api_key: str = field(repr=False)
    region: Literal["cn-beijing", "ap-southeast-1"]
    workspace_id: str
    model: str
    connect_timeout_seconds: float
    configure_timeout_seconds: float
    finish_timeout_seconds: float
    max_message_bytes: int

    @property
    def websocket_url(self) -> str:
        suffix = {
            "cn-beijing": "cn-beijing.maas.aliyuncs.com",
            "ap-southeast-1": "ap-southeast-1.maas.aliyuncs.com",
        }[self.region]
        return (
            f"wss://{self.workspace_id}.{suffix}/api-ws/v1/realtime"
            f"?model={self.model}"
        )


@dataclass(frozen=True, slots=True)
class AudioSettings:
    microphone: DeviceSelector
    remote_playback: DeviceSelector
    virtual_output: DeviceSelector
    input_sample_rate_hz: int
    output_sample_rate_hz: int
    chunk_ms: int
    silence_dbfs: float
    silence_report_after_ms: int
    virtual_output_enabled: bool = True
    microphone_gain_db: float = 0.0
    microphone_noise_gate_enabled: bool = False
    microphone_noise_gate_dbfs: float = -52.0
    microphone_noise_gate_hold_ms: int = 300
    target_language_guard_enabled: bool = True


@dataclass(frozen=True, slots=True)
class VadSettings:
    type: str
    threshold: float
    silence_duration_ms: int


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    voice_id: str
    clone_frequency: str


@dataclass(frozen=True, slots=True)
class QueueSettings:
    input_chunks: int
    output_buffer_ms: int


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str
    directory: Path
    max_files: int
    max_bytes_per_file: int


@dataclass(frozen=True, slots=True)
class WebSettings:
    host: str
    port: int
    open_browser: bool


@dataclass(frozen=True, slots=True)
class SubtitleSettings:
    show_source: bool
    show_translation: bool
    font_size_px: int
    max_segments: int
    source_mode: str = "remote"
    show_channel_labels: bool = True
    compact_background: bool = True
    window_width_percent: int = 72
    horizontal_padding_px: int = 16
    vertical_padding_px: int = 8
    source_font_size_px: int = 16
    source_font_color: str = "#c7d2cc"
    channel_label_color: str = "#ddef7e"


@dataclass(frozen=True, slots=True)
class SessionSettings:
    channel: Channel
    source_language: str | None
    target_language: str
    modalities: tuple[str, ...]
    voice_id: str | None
    terminology: dict[str, str]
    transcription_model: str | None = "qwen3-asr-flash-realtime"
    input_audio_format: str = "pcm"
    output_audio_format: str = "pcm"
    enable_voice_clone: bool = False
    voice_clone_frequency: str = "never"
    same_language_skip_text: bool = False
    same_language_skip_audio: bool = False


@dataclass(frozen=True, slots=True)
class AppSettings:
    config_path: Path
    mode: Literal["local_subtitle", "microphone_interpretation", "full_duplex"]
    language_preset: str
    aliyun: AliyunSettings
    voice: VoiceSettings
    audio: AudioSettings
    vad: VadSettings
    queues: QueueSettings
    reconnect_delays_seconds: tuple[float, ...]
    logging: LoggingSettings
    web: WebSettings
    subtitle: SubtitleSettings
    remote_session: SessionSettings
    local_session: SessionSettings
    microphone_push_to_talk: bool = False


_ALLOWED_TOP_LEVEL = {
    "schema_version",
    "app",
    "aliyun",
    "web",
    "subtitle",
    "voice",
    "audio",
    "vad",
    "queues",
    "reconnect",
    "terminology",
    "logging",
}
_PLACEHOLDER_MARKERS = ("请替换", "replace", "your-", "<", ">")


def _required_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"配置项 {key} 必须是对象")
    return value


def _required_string(parent: dict[str, Any], key: str, *, label: str | None = None) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"配置项 {label or key} 必须是非空字符串")
    return value.strip()


def _optional_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key, "")
    if not isinstance(value, str):
        raise ConfigurationError(f"配置项 {key} 必须是字符串")
    return value.strip()


def _number(parent: dict[str, Any], key: str, default: float) -> float:
    value = parent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"配置项 {key} 必须是数字")
    return float(value)


def _positive_int(parent: dict[str, Any], key: str, default: int) -> int:
    value = parent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"配置项 {key} 必须是正整数")
    return value


def _boolean(parent: dict[str, Any], key: str, default: bool) -> bool:
    value = parent.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"配置项 {key} 必须是布尔值")
    return value


def _device_selector(raw: dict[str, Any], path: str) -> DeviceSelector:
    host_api = str(raw.get("host_api", "WASAPI")).strip()
    name = str(raw.get("name", "")).strip()
    use_default = raw.get("use_default", False)
    if host_api.upper() != "WASAPI":
        raise ConfigurationError(f"{path}.host_api V1 只支持 WASAPI")
    if not isinstance(use_default, bool):
        raise ConfigurationError(f"{path}.use_default 必须是布尔值")
    if not use_default and not name:
        raise ConfigurationError(f"{path}.name 不能为空")
    return DeviceSelector(host_api="WASAPI", name=name, use_default=use_default)


def _contains_placeholder(value: str) -> bool:
    lower = value.lower()
    return any(marker in lower for marker in _PLACEHOLDER_MARKERS)


def _load_terminology(path: Path, max_bytes: int) -> dict[str, str]:
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise ConfigurationError(f"术语文件不存在：{path}") from exc
    if size > max_bytes:
        raise ConfigurationError(f"术语文件超过 {max_bytes} bytes：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"术语文件不是有效 UTF-8 JSON：{path}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"术语文件顶层必须是对象：{path}")
    result: dict[str, str] = {}
    for source, target in raw.items():
        if not isinstance(source, str) or not source.strip():
            raise ConfigurationError(f"术语源词必须是非空字符串：{path}")
        if not isinstance(target, str) or not target.strip():
            raise ConfigurationError(f"术语目标词必须是非空字符串：{path}")
        result[source.strip()] = target.strip()
    return result


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"JSON 存在重复键：{key}")
        result[key] = value
    return result


def load_settings(path: str | Path, *, require_runtime_secrets: bool = True) -> AppSettings:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"无法读取配置：{config_path}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("settings.yaml 顶层必须是对象")
    unknown = set(raw) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise ConfigurationError(f"存在未知顶层配置项：{', '.join(sorted(unknown))}")
    if raw.get("schema_version") != 1:
        raise ConfigurationError("schema_version 必须为 1")

    app_raw = _required_mapping(raw, "app")
    aliyun_raw = _required_mapping(raw, "aliyun")
    web_raw = _required_mapping(raw, "web")
    subtitle_raw = _required_mapping(raw, "subtitle")
    voice_raw = _required_mapping(raw, "voice")
    audio_raw = _required_mapping(raw, "audio")
    vad_raw = _required_mapping(raw, "vad")
    queues_raw = _required_mapping(raw, "queues")
    reconnect_raw = _required_mapping(raw, "reconnect")
    terminology_raw = _required_mapping(raw, "terminology")
    logging_raw = _required_mapping(raw, "logging")

    mode = _required_string(app_raw, "mode")
    if mode not in {"local_subtitle", "full_duplex"}:
        raise ConfigurationError("app.mode 只允许 local_subtitle 或 full_duplex")
    preset = _required_string(app_raw, "language_preset")
    if preset not in {"local_zh_remote_en", "local_en_remote_zh"}:
        raise ConfigurationError("app.language_preset 只允许两种中英文预设")

    region = _required_string(aliyun_raw, "region")
    if region not in {"cn-beijing", "ap-southeast-1"}:
        raise ConfigurationError("aliyun.region 不受支持")
    workspace_id = _required_string(aliyun_raw, "workspace_id")
    model = _required_string(aliyun_raw, "model")
    if _contains_placeholder(workspace_id):
        raise ConfigurationError("请先填写真实 aliyun.workspace_id")
    if model != "qwen3.5-livetranslate-flash-realtime":
        raise ConfigurationError("V1 模型必须为 qwen3.5-livetranslate-flash-realtime")
    configured_api_key = _optional_string(aliyun_raw, "api_key")
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip() or configured_api_key
    aliyun = AliyunSettings(
        api_key=api_key,
        region=region,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        model=model,
        connect_timeout_seconds=_number(aliyun_raw, "connect_timeout_seconds", 10),
        configure_timeout_seconds=_number(aliyun_raw, "configure_timeout_seconds", 10),
        finish_timeout_seconds=_number(aliyun_raw, "finish_timeout_seconds", 15),
        max_message_bytes=_positive_int(aliyun_raw, "max_message_bytes", 2_097_152),
    )

    voice_id = _optional_string(voice_raw, "voice_id")
    frequency = _required_string(voice_raw, "clone_frequency")
    if mode == "full_duplex" and (not voice_id or _contains_placeholder(voice_id)):
        raise ConfigurationError("请先填写真实 voice.voice_id")
    if frequency not in {"never", "once", "always"}:
        raise ConfigurationError("voice.clone_frequency 仅允许 never/once/always")
    voice = VoiceSettings(voice_id=voice_id, clone_frequency=frequency)

    virtual_output_raw = _required_mapping(audio_raw, "virtual_output")
    audio = AudioSettings(
        microphone=_device_selector(_required_mapping(audio_raw, "microphone"), "audio.microphone"),
        remote_playback=_device_selector(
            _required_mapping(audio_raw, "remote_playback"), "audio.remote_playback"
        ),
        virtual_output=_device_selector(virtual_output_raw, "audio.virtual_output"),
        input_sample_rate_hz=_positive_int(audio_raw, "input_sample_rate_hz", 16000),
        output_sample_rate_hz=_positive_int(audio_raw, "output_sample_rate_hz", 24000),
        chunk_ms=_positive_int(audio_raw, "chunk_ms", 100),
        silence_dbfs=_number(audio_raw, "silence_dbfs", -48.0),
        silence_report_after_ms=_positive_int(audio_raw, "silence_report_after_ms", 3000),
        virtual_output_enabled=_boolean(virtual_output_raw, "enabled", True),
    )
    if audio.input_sample_rate_hz != 16000 or audio.output_sample_rate_hz != 24000:
        raise ConfigurationError("官方当前基线要求输入 16000 Hz、输出 24000 Hz")
    if audio.chunk_ms != 100:
        raise ConfigurationError("V1 固定使用 100 ms 音频块")

    vad_type = _required_string(vad_raw, "type")
    threshold = _number(vad_raw, "threshold", 0.2)
    silence_duration_ms = _positive_int(vad_raw, "silence_duration_ms", 500)
    if vad_type != "server_vad" or not -1.0 <= threshold <= 1.0:
        raise ConfigurationError("VAD 必须为 server_vad 且 threshold 在 [-1,1]")
    if not 200 <= silence_duration_ms <= 6000:
        raise ConfigurationError("vad.silence_duration_ms 必须在 [200,6000]")
    vad = VadSettings(vad_type, threshold, silence_duration_ms)

    queues = QueueSettings(
        input_chunks=_positive_int(queues_raw, "input_chunks", 20),
        output_buffer_ms=_positive_int(queues_raw, "output_buffer_ms", 3000),
    )
    delays = reconnect_raw.get("delays_seconds")
    if not isinstance(delays, list) or not delays or len(delays) > 10:
        raise ConfigurationError("reconnect.delays_seconds 必须是非空数字数组")
    reconnect_delays: list[float] = []
    for value in delays:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigurationError("重连等待必须是正数")
        reconnect_delays.append(float(value))

    max_term_bytes = _positive_int(terminology_raw, "max_file_bytes", 262144)
    config_dir = config_path.parent
    zh_to_en = _load_terminology(
        (config_dir / _required_string(terminology_raw, "zh_to_en")).resolve(), max_term_bytes
    )
    en_to_zh = _load_terminology(
        (config_dir / _required_string(terminology_raw, "en_to_zh")).resolve(), max_term_bytes
    )

    log_dir_value = _required_string(logging_raw, "directory")
    log_dir = Path(log_dir_value)
    if not log_dir.is_absolute():
        log_dir = (config_dir / log_dir).resolve()
    logging_settings = LoggingSettings(
        level=_required_string(logging_raw, "level").upper(),
        directory=log_dir,
        max_files=_positive_int(logging_raw, "max_files", 5),
        max_bytes_per_file=_positive_int(logging_raw, "max_bytes_per_file", 5_242_880),
    )

    web_host = _required_string(web_raw, "host")
    if web_host not in {"127.0.0.1", "localhost"}:
        raise ConfigurationError("web.host 仅允许 127.0.0.1 或 localhost")
    web_port = _positive_int(web_raw, "port", 8765)
    if web_port > 65535:
        raise ConfigurationError("web.port 必须在 1-65535")
    open_browser = web_raw.get("open_browser", True)
    if not isinstance(open_browser, bool):
        raise ConfigurationError("web.open_browser 必须是布尔值")
    web_settings = WebSettings(web_host, web_port, open_browser)

    show_source = subtitle_raw.get("show_source", True)
    show_translation = subtitle_raw.get("show_translation", True)
    if not isinstance(show_source, bool) or not isinstance(show_translation, bool):
        raise ConfigurationError("subtitle.show_source/show_translation 必须是布尔值")
    font_size_px = _positive_int(subtitle_raw, "font_size_px", 28)
    max_segments = _positive_int(subtitle_raw, "max_segments", 50)
    if not 18 <= font_size_px <= 48:
        raise ConfigurationError("subtitle.font_size_px 必须在 18-48")
    if not 10 <= max_segments <= 200:
        raise ConfigurationError("subtitle.max_segments 必须在 10-200")
    subtitle_settings = SubtitleSettings(
        show_source=show_source,
        show_translation=show_translation,
        font_size_px=font_size_px,
        max_segments=max_segments,
    )

    if require_runtime_secrets and not api_key:
        raise ConfigurationError("请在 aliyun.api_key 或 DASHSCOPE_API_KEY 中配置密钥")

    if preset == "local_zh_remote_en":
        local_languages = ("zh", "en", zh_to_en)
        remote_languages = ("en", "zh", en_to_zh)
    else:
        local_languages = ("en", "zh", en_to_zh)
        remote_languages = ("zh", "en", zh_to_en)

    remote_session = SessionSettings(
        channel="remote",
        source_language=remote_languages[0],
        target_language=remote_languages[1],
        modalities=("text",),
        voice_id=None,
        terminology=remote_languages[2],
        enable_voice_clone=False,
        voice_clone_frequency=frequency,
    )
    local_session = SessionSettings(
        channel="local",
        source_language=local_languages[0],
        target_language=local_languages[1],
        modalities=("text",) if mode == "local_subtitle" else ("text", "audio"),
        voice_id=None if mode == "local_subtitle" else voice.voice_id,
        terminology=local_languages[2],
        enable_voice_clone=mode == "full_duplex",
        voice_clone_frequency=frequency,
    )
    return AppSettings(
        config_path=config_path,
        mode=mode,  # type: ignore[arg-type]
        language_preset=preset,
        aliyun=aliyun,
        voice=voice,
        audio=audio,
        vad=vad,
        queues=queues,
        reconnect_delays_seconds=tuple(reconnect_delays),
        logging=logging_settings,
        web=web_settings,
        subtitle=subtitle_settings,
        remote_session=remote_session,
        local_session=local_session,
    )
