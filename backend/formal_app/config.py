from __future__ import annotations

import copy
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ai_interpreter.config import (
    AliyunSettings,
    AppSettings,
    AudioSettings,
    DeviceSelector,
    LoggingSettings,
    QueueSettings,
    SessionSettings,
    SubtitleSettings,
    VadSettings,
    VoiceSettings,
    WebSettings,
)


class FormalConfigurationError(ValueError):
    pass


LANGUAGES = {
    "auto",
    "zh",
    "en",
    "de",
    "it",
    "pt",
    "es",
    "ja",
    "ko",
    "fr",
    "ru",
    "th",
    "id",
    "ar",
    "cs",
    "da",
    "nl",
    "fi",
    "he",
    "hi",
    "is",
    "ms",
    "no",
    "fa",
    "pl",
    "sv",
    "tl",
    "tr",
    "ur",
    "vi",
}
REGIONS = {"cn-beijing", "ap-southeast-1"}
MODEL = "qwen3.5-livetranslate-flash-realtime"


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "app": {"mode": "subtitle_only", "auto_open_overlay": True},
    "microphone": {
        "translate_enabled": True,
        "clone_audio_enabled": False,
        "push_to_talk_enabled": False,
        "push_to_talk_shortcut": "Ctrl+Space",
    },
    "aliyun": {
        "region": "cn-beijing",
        "workspace_id": "",
        "model": MODEL,
        "connect_timeout_seconds": 10,
        "configure_timeout_seconds": 10,
        "finish_timeout_seconds": 15,
        "max_message_bytes": 2097152,
    },
    "sessions": {
        "input_sample_rate_hz": 16000,
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "transcription_model": "qwen3-asr-flash-realtime",
        "remote": {
            "source_language": "auto",
            "target_language": "zh",
            "same_language_skip_text": False,
            "same_language_skip_audio": True,
        },
        "local": {
            "source_language": "zh",
            "target_language": "en",
            "same_language_skip_text": False,
            "same_language_skip_audio": True,
        },
    },
    "voice": {
        "voice_id": "",
        "enable_voice_clone": True,
        "clone_frequency": "never",
        "enrollment_model": "qwen-voice-enrollment",
        "preferred_name": "meeting_voice",
        "request_timeout_seconds": 90,
        "page_size": 10,
        "recording_prompt": (
            "你好，这是我的声音复刻测试。我会用自然、清晰、稳定的语速读完这段文字。"
        ),
    },
    "audio": {
        "microphone": {"host_api": "WASAPI", "name": "", "use_default": True},
        "remote_playback": {"host_api": "WASAPI", "name": "", "use_default": True},
        "virtual_output": {"host_api": "WASAPI", "name": "CABLE Input", "use_default": False},
        "virtual_recording": {"host_api": "WASAPI", "name": "CABLE Output", "use_default": False},
        "virtual_output_enabled": True,
        "output_sample_rate_hz": 24000,
        "chunk_ms": 100,
        "microphone_gain_db": 0.0,
        "microphone_noise_gate_enabled": False,
        "microphone_noise_gate_dbfs": -52.0,
        "microphone_noise_gate_hold_ms": 300,
        "target_language_guard_enabled": True,
        "silence_dbfs": -48.0,
        "silence_report_after_ms": 3000,
    },
    "vad": {"type": "server_vad", "threshold": 0.2, "silence_duration_ms": 500},
    "queues": {"input_chunks": 20, "output_buffer_ms": 5000},
    "reconnect": {"delays_seconds": [1, 2, 4, 8, 15]},
    "hotwords": {"remote": {}, "local": {}},
    "subtitle": {
        "source_mode": "remote",
        "show_source": True,
        "show_translation": True,
        "show_channel_labels": True,
        "compact_background": True,
        "window_width_percent": 72,
        "horizontal_padding_px": 16,
        "vertical_padding_px": 8,
        "source_font_size_px": 16,
        "source_font_color": "#c7d2cc",
        "channel_label_color": "#ddef7e",
        "font_size_px": 30,
        "max_segments": 80,
        "font_color": "#ffffff",
        "background_color": "#111827",
        "background_opacity": 0.86,
        "position": "bottom",
    },
    "test": {
        "signal_threshold_dbfs": -48.0,
        "route_tone_hz": 1000,
        "route_tone_seconds": 1.0,
        "route_record_tail_ms": 800,
        "cloud_max_duration_seconds": 60,
        "max_recordings": 20,
    },
    "web": {"host": "127.0.0.1", "port": 8788, "open_browser": True},
    "logging": {
        "level": "INFO",
        "directory": "../runtime/logs",
        "max_files": 7,
        "max_bytes_per_file": 5242880,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise FormalConfigurationError(f"{key} 必须是对象")
    return value


def _number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise FormalConfigurationError(f"{label} 必须在 {low}-{high} 之间")
    return float(value)


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise FormalConfigurationError(f"{label} 必须是 {low}-{high} 的整数")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise FormalConfigurationError(f"{label} 必须是布尔值")
    return value


def _push_to_talk_shortcut(value: Any) -> str:
    if not isinstance(value, str):
        raise FormalConfigurationError("microphone.push_to_talk_shortcut 必须是字符串")
    parts = value.split("+")
    modifiers = parts[:-1]
    key = parts[-1] if parts else ""
    allowed_key = key == "Space" or bool(re.fullmatch(r"[A-Z]|F(?:[1-9]|1[0-2])", key))
    canonical_modifiers = [item for item in ("Ctrl", "Alt", "Shift") if item in modifiers]
    if (
        not modifiers
        or len(modifiers) != len(set(modifiers))
        or any(item not in {"Ctrl", "Alt", "Shift"} for item in modifiers)
        or modifiers != canonical_modifiers
        or not allowed_key
    ):
        raise FormalConfigurationError(
            "按住说话快捷键必须是 Ctrl/Alt/Shift 组合加 Space、A-Z 或 F1-F12"
        )
    return value


def _language(value: Any, label: str, *, allow_auto: bool) -> str | None:
    if not isinstance(value, str) or value not in LANGUAGES:
        raise FormalConfigurationError(f"{label} 不是受支持语言")
    if value == "auto":
        if not allow_auto:
            raise FormalConfigurationError(f"{label} 不能为 auto")
        return None
    return value


def _selector(raw: dict[str, Any], label: str) -> DeviceSelector:
    host = raw.get("host_api")
    name = raw.get("name")
    default = raw.get("use_default")
    if host != "WASAPI" or not isinstance(name, str) or not isinstance(default, bool):
        raise FormalConfigurationError(f"{label} 必须包含 WASAPI/name/use_default")
    if not default and not name.strip():
        raise FormalConfigurationError(f"{label} 未使用默认设备时必须填写名称")
    return DeviceSelector(host, name.strip(), default)


def _hotwords(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 200:
        raise FormalConfigurationError(f"{label} 必须是最多 200 项的对象")
    result: dict[str, str] = {}
    for key, translated in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(translated, str):
            raise FormalConfigurationError(f"{label} 的键和值必须是字符串")
        result[key.strip()] = translated.strip()
    return result


@dataclass(slots=True)
class ConfigStore:
    config_path: Path
    secret_path: Path

    def __init__(self, config_path: str | Path, secret_path: str | Path) -> None:
        self.config_path = Path(config_path).resolve()
        self.secret_path = Path(secret_path).resolve()

    def _read(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise FormalConfigurationError(f"无法读取配置：{path.name}") from exc
        if not isinstance(value, dict):
            raise FormalConfigurationError(f"{path.name} 顶层必须是对象")
        return value

    def raw(self) -> dict[str, Any]:
        merged = _merge(DEFAULT_CONFIG, self._read(self.config_path))
        return _merge(merged, self._read(self.secret_path))

    def public(self) -> dict[str, Any]:
        raw = self.raw()
        key = str(raw.setdefault("aliyun", {}).pop("api_key", "") or "")
        env_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        raw["aliyun"]["api_key"] = ""
        raw["aliyun"]["api_key_configured"] = bool(env_key or key)
        raw["aliyun"]["api_key_source"] = (
            "environment" if env_key else ("local_file" if key else "none")
        )
        raw["paths"] = {
            "settings": str(self.config_path),
            "secrets": str(self.secret_path),
        }
        return raw

    def validate(self, *, for_start: bool = False) -> list[str]:
        self.to_app_settings(for_start=for_start)
        return []

    def save(self, incoming: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(incoming, dict):
            raise FormalConfigurationError("提交配置必须是对象")
        current = self.raw()
        submitted_key = (
            incoming.get("aliyun", {}).get("api_key")
            if isinstance(incoming.get("aliyun"), dict)
            else None
        )
        clear_key = bool(incoming.pop("clear_api_key", False))
        merged = _merge(current, incoming)
        if isinstance(submitted_key, str) and submitted_key.strip():
            merged.setdefault("aliyun", {})["api_key"] = submitted_key.strip()
        elif clear_key:
            merged.setdefault("aliyun", {})["api_key"] = ""
        else:
            merged.setdefault("aliyun", {})["api_key"] = str(
                current.get("aliyun", {}).get("api_key", "")
            )
        self._validate_raw(merged, for_start=False)
        secret = {"aliyun": {"api_key": merged["aliyun"].pop("api_key", "")}}
        merged.pop("paths", None)
        merged.get("aliyun", {}).pop("api_key_configured", None)
        merged.get("aliyun", {}).pop("api_key_source", None)
        self._atomic_write(self.config_path, merged)
        self._atomic_write(self.secret_path, secret)
        return self.public()

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
        )
        temp = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _validate_raw(self, raw: dict[str, Any], *, for_start: bool) -> None:
        if raw.get("schema_version") != 1:
            raise FormalConfigurationError("schema_version 必须为 1")
        app = _mapping(raw, "app")
        if app.get("mode") not in {
            "subtitle_only",
            "microphone_subtitle",
            "full_interpretation",
        }:
            raise FormalConfigurationError(
                "app.mode 仅允许 subtitle_only/microphone_subtitle/full_interpretation"
            )
        _boolean(app.get("auto_open_overlay"), "app.auto_open_overlay")
        microphone = _mapping(raw, "microphone")
        translate_enabled = _boolean(
            microphone.get("translate_enabled"), "microphone.translate_enabled"
        )
        clone_audio_enabled = _boolean(
            microphone.get("clone_audio_enabled"), "microphone.clone_audio_enabled"
        )
        _boolean(
            microphone.get("push_to_talk_enabled"),
            "microphone.push_to_talk_enabled",
        )
        _push_to_talk_shortcut(microphone.get("push_to_talk_shortcut"))
        if clone_audio_enabled and not translate_enabled:
            raise FormalConfigurationError("输出克隆声音时必须启用麦克风翻译")
        if (
            app.get("mode") == "microphone_subtitle"
            and not translate_enabled
            and _mapping(_mapping(raw, "sessions"), "local").get("source_language") == "auto"
        ):
            raise FormalConfigurationError("关闭麦克风翻译时必须明确选择麦克风源语言")
        aliyun = _mapping(raw, "aliyun")
        if aliyun.get("region") not in REGIONS or aliyun.get("model") != MODEL:
            raise FormalConfigurationError("百炼区域或 LiveTranslate 模型无效")
        workspace = aliyun.get("workspace_id")
        if workspace and (
            not isinstance(workspace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{2,128}", workspace)
        ):
            raise FormalConfigurationError("aliyun.workspace_id 格式无效")
        if for_start:
            key = (
                os.environ.get("DASHSCOPE_API_KEY", "").strip()
                or str(aliyun.get("api_key", "")).strip()
            )
            if not key or not workspace:
                raise FormalConfigurationError("启动前必须配置 API Key 和 Workspace ID")
        for key in (
            "connect_timeout_seconds",
            "configure_timeout_seconds",
            "finish_timeout_seconds",
        ):
            _number(aliyun.get(key), f"aliyun.{key}", 1, 120)
        _integer(
            aliyun.get("max_message_bytes"),
            "aliyun.max_message_bytes",
            65536,
            16777216,
        )
        sessions = _mapping(raw, "sessions")
        if (
            sessions.get("input_sample_rate_hz") != 16000
            or sessions.get("input_audio_format") != "pcm"
            or sessions.get("output_audio_format") != "pcm"
        ):
            raise FormalConfigurationError("正式版链路固定使用 16 kHz PCM 输入和 PCM 输出")
        if sessions.get("transcription_model") != "qwen3-asr-flash-realtime":
            raise FormalConfigurationError(
                "sessions.transcription_model 必须为 qwen3-asr-flash-realtime"
            )
        for channel in ("remote", "local"):
            session = _mapping(sessions, channel)
            _language(
                session.get("source_language"),
                f"sessions.{channel}.source_language",
                allow_auto=True,
            )
            _language(
                session.get("target_language"),
                f"sessions.{channel}.target_language",
                allow_auto=False,
            )
            _boolean(
                session.get("same_language_skip_text"),
                f"sessions.{channel}.same_language_skip_text",
            )
            _boolean(
                session.get("same_language_skip_audio"),
                f"sessions.{channel}.same_language_skip_audio",
            )
        local_session = _mapping(sessions, "local")
        clone_output = app.get("mode") == "full_interpretation" or (
            app.get("mode") == "microphone_subtitle" and clone_audio_enabled
        )
        if clone_output and for_start:
            if local_session.get("source_language") == "auto":
                raise FormalConfigurationError("克隆译音启动前必须明确选择麦克风源语言")
            if local_session.get("source_language") == local_session.get("target_language"):
                raise FormalConfigurationError("克隆译音的麦克风源语言和目标语言不能相同")
        voice = _mapping(raw, "voice")
        if voice.get("clone_frequency") not in {"never", "once", "always"}:
            raise FormalConfigurationError("voice.clone_frequency 仅允许 never/once/always")
        if voice.get("enrollment_model") != "qwen-voice-enrollment":
            raise FormalConfigurationError("声音复刻管理模型必须为 qwen-voice-enrollment")
        voice_id = str(voice.get("voice_id", "")).strip()
        if voice_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", voice_id):
            raise FormalConfigurationError("voice.voice_id 格式无效")
        _boolean(voice.get("enable_voice_clone"), "voice.enable_voice_clone")
        preferred_name = str(voice.get("preferred_name", ""))
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", preferred_name):
            raise FormalConfigurationError("voice.preferred_name 仅允许 1-16 位字母数字下划线")
        _number(
            voice.get("request_timeout_seconds"),
            "voice.request_timeout_seconds",
            1,
            300,
        )
        _integer(voice.get("page_size"), "voice.page_size", 1, 100)
        if (
            (
                app.get("mode") == "full_interpretation"
                or (
                    app.get("mode") == "microphone_subtitle"
                    and clone_audio_enabled
                )
            )
            and for_start
            and not str(voice.get("voice_id", "")).strip()
        ):
            raise FormalConfigurationError("同传模式启动前必须选择 voice_id")
        audio = _mapping(raw, "audio")
        _selector(_mapping(audio, "microphone"), "audio.microphone")
        _selector(_mapping(audio, "remote_playback"), "audio.remote_playback")
        _selector(_mapping(audio, "virtual_output"), "audio.virtual_output")
        _selector(_mapping(audio, "virtual_recording"), "audio.virtual_recording")
        if audio.get("output_sample_rate_hz") != 24000:
            raise FormalConfigurationError("云端克隆译音采样率固定为 24 kHz")
        _integer(audio.get("chunk_ms"), "audio.chunk_ms", 20, 200)
        _number(audio.get("microphone_gain_db"), "audio.microphone_gain_db", -12, 12)
        _boolean(
            audio.get("microphone_noise_gate_enabled"),
            "audio.microphone_noise_gate_enabled",
        )
        _number(
            audio.get("microphone_noise_gate_dbfs"),
            "audio.microphone_noise_gate_dbfs",
            -80,
            -20,
        )
        _integer(
            audio.get("microphone_noise_gate_hold_ms"),
            "audio.microphone_noise_gate_hold_ms",
            50,
            2000,
        )
        _boolean(
            audio.get("target_language_guard_enabled"),
            "audio.target_language_guard_enabled",
        )
        _number(audio.get("silence_dbfs"), "audio.silence_dbfs", -90, -6)
        _boolean(audio.get("virtual_output_enabled"), "audio.virtual_output_enabled")
        _integer(
            audio.get("silence_report_after_ms"),
            "audio.silence_report_after_ms",
            500,
            30000,
        )
        vad = _mapping(raw, "vad")
        if vad.get("type") != "server_vad":
            raise FormalConfigurationError("连续会议链路当前只支持 server_vad")
        _number(vad.get("threshold"), "vad.threshold", -1, 1)
        _integer(vad.get("silence_duration_ms"), "vad.silence_duration_ms", 200, 6000)
        _hotwords(_mapping(raw, "hotwords").get("remote"), "hotwords.remote")
        _hotwords(_mapping(raw, "hotwords").get("local"), "hotwords.local")
        queue = _mapping(raw, "queues")
        _integer(queue.get("input_chunks"), "queues.input_chunks", 5, 100)
        _integer(queue.get("output_buffer_ms"), "queues.output_buffer_ms", 500, 15000)
        delays = _mapping(raw, "reconnect").get("delays_seconds")
        if (
            not isinstance(delays, list)
            or not 1 <= len(delays) <= 10
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= 60
                for value in delays
            )
        ):
            raise FormalConfigurationError("reconnect.delays_seconds 必须是 1-10 个正数")
        subtitle = _mapping(raw, "subtitle")
        if subtitle.get("source_mode") not in {"remote", "local", "both"}:
            raise FormalConfigurationError("subtitle.source_mode 仅允许 remote/local/both")
        _boolean(subtitle.get("show_source"), "subtitle.show_source")
        _boolean(subtitle.get("show_translation"), "subtitle.show_translation")
        _boolean(subtitle.get("show_channel_labels"), "subtitle.show_channel_labels")
        _boolean(subtitle.get("compact_background"), "subtitle.compact_background")
        if not subtitle.get("show_source") and not subtitle.get("show_translation"):
            raise FormalConfigurationError("原文和译文至少显示一项")
        _integer(subtitle.get("font_size_px"), "subtitle.font_size_px", 18, 48)
        _integer(
            subtitle.get("source_font_size_px"),
            "subtitle.source_font_size_px",
            10,
            32,
        )
        _integer(
            subtitle.get("window_width_percent"),
            "subtitle.window_width_percent",
            40,
            100,
        )
        _integer(
            subtitle.get("horizontal_padding_px"),
            "subtitle.horizontal_padding_px",
            0,
            60,
        )
        _integer(
            subtitle.get("vertical_padding_px"),
            "subtitle.vertical_padding_px",
            0,
            40,
        )
        _integer(subtitle.get("max_segments"), "subtitle.max_segments", 10, 200)
        for key in (
            "font_color",
            "source_font_color",
            "channel_label_color",
            "background_color",
        ):
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(subtitle.get(key, ""))):
                raise FormalConfigurationError(f"subtitle.{key} 必须是 #RRGGBB")
        _number(
            subtitle.get("background_opacity"),
            "subtitle.background_opacity",
            0.05,
            1,
        )
        if subtitle.get("position") not in {"top", "bottom"}:
            raise FormalConfigurationError("subtitle.position 仅允许 top/bottom")
        test = _mapping(raw, "test")
        _number(
            test.get("signal_threshold_dbfs"),
            "test.signal_threshold_dbfs",
            -90,
            -6,
        )
        _integer(test.get("route_tone_hz"), "test.route_tone_hz", 100, 5000)
        _number(test.get("route_tone_seconds"), "test.route_tone_seconds", 0.2, 10)
        _integer(test.get("route_record_tail_ms"), "test.route_record_tail_ms", 0, 5000)
        _integer(
            test.get("cloud_max_duration_seconds"),
            "test.cloud_max_duration_seconds",
            5,
            300,
        )
        _integer(test.get("max_recordings"), "test.max_recordings", 1, 100)
        web = _mapping(raw, "web")
        if web.get("host") not in {"127.0.0.1", "localhost"}:
            raise FormalConfigurationError("Web 服务只允许绑定本机")
        _integer(web.get("port"), "web.port", 1, 65535)
        _boolean(web.get("open_browser"), "web.open_browser")
        logging = _mapping(raw, "logging")
        if logging.get("level") not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise FormalConfigurationError("logging.level 无效")
        if not isinstance(logging.get("directory"), str) or not logging["directory"].strip():
            raise FormalConfigurationError("logging.directory 不能为空")
        _integer(logging.get("max_files"), "logging.max_files", 1, 50)
        _integer(
            logging.get("max_bytes_per_file"),
            "logging.max_bytes_per_file",
            65536,
            104857600,
        )

    def to_app_settings(self, *, for_start: bool = False) -> AppSettings:
        raw = self.raw()
        self._validate_raw(raw, for_start=for_start)
        app = raw["app"]
        microphone = raw["microphone"]
        aliyun = raw["aliyun"]
        sessions = raw["sessions"]
        voice = raw["voice"]
        audio = raw["audio"]
        vad = raw["vad"]
        queues = raw["queues"]
        subtitle = raw["subtitle"]
        web = raw["web"]
        logging = raw["logging"]
        mode = app["mode"]
        api_key = (
            os.environ.get("DASHSCOPE_API_KEY", "").strip()
            or str(aliyun.get("api_key", "")).strip()
        )
        remote_raw = sessions["remote"]
        local_raw = sessions["local"]
        microphone_mode = mode == "microphone_subtitle"
        clone_audio = mode == "full_interpretation" or (
            microphone_mode and bool(microphone["clone_audio_enabled"])
        )
        translate_enabled = mode == "full_interpretation" or bool(
            microphone["translate_enabled"]
        )
        remote = SessionSettings(
            "remote",
            _language(remote_raw["source_language"], "remote source", allow_auto=True),
            remote_raw["target_language"],
            ("text",),
            None,
            _hotwords(raw["hotwords"]["remote"], "remote hotwords"),
            transcription_model=sessions["transcription_model"],
            input_audio_format=sessions["input_audio_format"],
            output_audio_format=sessions["output_audio_format"],
            same_language_skip_text=remote_raw["same_language_skip_text"],
            same_language_skip_audio=remote_raw["same_language_skip_audio"],
        )
        local_source = _language(local_raw["source_language"], "local source", allow_auto=True)
        local_target = local_raw["target_language"]
        local_skip_text = local_raw["same_language_skip_text"]
        local_skip_audio = local_raw["same_language_skip_audio"]
        if microphone_mode and not translate_enabled:
            local_target = local_source or local_target
            local_skip_text = True
            local_skip_audio = True
        local = SessionSettings(
            "local",
            local_source,
            local_target,
            ("text", "audio") if clone_audio else ("text",),
            str(voice.get("voice_id", "")).strip() or None,
            _hotwords(raw["hotwords"]["local"], "local hotwords"),
            transcription_model=sessions["transcription_model"],
            input_audio_format=sessions["input_audio_format"],
            output_audio_format=sessions["output_audio_format"],
            enable_voice_clone=clone_audio and voice["enable_voice_clone"],
            voice_clone_frequency=voice["clone_frequency"],
            same_language_skip_text=local_skip_text,
            same_language_skip_audio=local_skip_audio,
        )
        log_path = Path(logging["directory"])
        if not log_path.is_absolute():
            log_path = (self.config_path.parent / log_path).resolve()
        return AppSettings(
            config_path=self.config_path,
            mode=(
                "full_duplex"
                if mode == "full_interpretation"
                else ("microphone_interpretation" if clone_audio else "local_subtitle")
            ),
            language_preset="formal_custom",
            aliyun=AliyunSettings(
                api_key,
                aliyun["region"],
                str(aliyun.get("workspace_id", "")),
                aliyun["model"],
                float(aliyun["connect_timeout_seconds"]),
                float(aliyun["configure_timeout_seconds"]),
                float(aliyun["finish_timeout_seconds"]),
                int(aliyun["max_message_bytes"]),
            ),
            voice=VoiceSettings(str(voice.get("voice_id", "")), voice["clone_frequency"]),
            audio=AudioSettings(
                _selector(audio["microphone"], "microphone"),
                _selector(audio["remote_playback"], "remote_playback"),
                _selector(audio["virtual_output"], "virtual_output"),
                16000,
                24000,
                int(audio["chunk_ms"]),
                float(audio["silence_dbfs"]),
                int(audio["silence_report_after_ms"]),
                bool(audio["virtual_output_enabled"]),
                float(audio["microphone_gain_db"]),
                bool(audio["microphone_noise_gate_enabled"]),
                float(audio["microphone_noise_gate_dbfs"]),
                int(audio["microphone_noise_gate_hold_ms"]),
                bool(audio["target_language_guard_enabled"]),
            ),
            vad=VadSettings(vad["type"], float(vad["threshold"]), int(vad["silence_duration_ms"])),
            queues=QueueSettings(int(queues["input_chunks"]), int(queues["output_buffer_ms"])),
            reconnect_delays_seconds=tuple(float(v) for v in raw["reconnect"]["delays_seconds"]),
            logging=LoggingSettings(
                logging["level"],
                log_path,
                int(logging["max_files"]),
                int(logging["max_bytes_per_file"]),
            ),
            web=WebSettings(web["host"], int(web["port"]), bool(web["open_browser"])),
            subtitle=SubtitleSettings(
                bool(subtitle["show_source"]),
                bool(subtitle["show_translation"]),
                int(subtitle["font_size_px"]),
                int(subtitle["max_segments"]),
                str(subtitle["source_mode"]),
                bool(subtitle["show_channel_labels"]),
                bool(subtitle["compact_background"]),
                int(subtitle["window_width_percent"]),
                int(subtitle["horizontal_padding_px"]),
                int(subtitle["vertical_padding_px"]),
                int(subtitle["source_font_size_px"]),
                str(subtitle["source_font_color"]),
                str(subtitle["channel_label_color"]),
            ),
            remote_session=remote,
            local_session=local,
            microphone_push_to_talk=bool(microphone["push_to_talk_enabled"]),
        )
