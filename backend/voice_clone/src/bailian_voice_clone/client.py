from __future__ import annotations

import base64
import math
import os
import re
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import requests
import yaml


class VoiceCloneError(RuntimeError):
    """声音复刻 CLI 的基础错误。"""


class VoiceCloneConfigurationError(VoiceCloneError):
    """配置不完整或不符合官方接口约束。"""


class AudioSampleError(VoiceCloneError):
    """本地声音样本不符合可静态验证的要求。"""


class VoiceCloneApiError(VoiceCloneError):
    """百炼声音复刻接口返回失败。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str = "",
        error_code: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.error_code = error_code


_TARGET_MODEL = "qwen3.5-livetranslate-flash-realtime"
_LANGUAGES = {
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
    "Dongbei",
    "Shannxi",
    "Sichuan",
    "Henan",
    "Changsha",
    "Tianjin",
    "Hangzhou",
    "Liaoning",
    "Shenyang",
    "Anshan",
}
_MIME_BY_SUFFIX = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}
_MIME_TYPES = set(_MIME_BY_SUFFIX.values())
_PLACEHOLDER_MARKERS = ("请替换", "replace", "your-", "<", ">")


@dataclass(frozen=True, slots=True)
class VoiceCloneSettings:
    config_path: Path
    api_key: str = field(repr=False)
    workspace_id: str = ""
    region: Literal["cn-beijing", "ap-southeast-1"] = "cn-beijing"
    request_timeout_seconds: float = 90
    enrollment_model: str = "qwen-voice-enrollment"
    target_model: str = _TARGET_MODEL
    preferred_name: str = "meeting_voice"
    audio_file: Path | None = None
    audio_url: str = ""
    audio_mime_type: str = "auto"
    transcript: str = ""
    language: str = ""
    max_encoded_bytes: int = 10_000_000
    validate_wav_metadata: bool = True
    page_index: int = 0
    page_size: int = 10
    output_format: Literal["table", "json"] = "table"
    show_request_id: bool = True
    recording_prompt: str = ""

    @property
    def endpoint(self) -> str:
        host = {
            "cn-beijing": "cn-beijing.maas.aliyuncs.com",
            "ap-southeast-1": "ap-southeast-1.maas.aliyuncs.com",
        }[self.region]
        return (
            f"https://{self.workspace_id}.{host}"
            "/api/v1/services/audio/tts/customization"
        )


@dataclass(frozen=True, slots=True)
class AudioSample:
    data: str
    mime_type: str
    source: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CreatedVoice:
    voice: str
    target_model: str
    request_id: str
    billed_count: int


@dataclass(frozen=True, slots=True)
class VoiceRecord:
    voice: str
    gmt_create: str
    target_model: str


@dataclass(frozen=True, slots=True)
class VoiceListResult:
    voices: tuple[VoiceRecord, ...]
    request_id: str


@dataclass(frozen=True, slots=True)
class DeleteResult:
    voice: str
    request_id: str


def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise VoiceCloneConfigurationError(f"配置项 {key} 必须是对象")
    return value


def _string(parent: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = parent.get(key, "")
    if not isinstance(value, str):
        raise VoiceCloneConfigurationError(f"配置项 {key} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise VoiceCloneConfigurationError(f"配置项 {key} 不能为空")
    return value


def _int(parent: dict[str, Any], key: str, default: int, *, minimum: int = 0) -> int:
    value = parent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise VoiceCloneConfigurationError(f"配置项 {key} 必须是不小于 {minimum} 的整数")
    return value


def _float(parent: dict[str, Any], key: str, default: float) -> float:
    value = parent.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise VoiceCloneConfigurationError(f"配置项 {key} 必须是正数")
    return float(value)


def _bool(parent: dict[str, Any], key: str, default: bool) -> bool:
    value = parent.get(key, default)
    if not isinstance(value, bool):
        raise VoiceCloneConfigurationError(f"配置项 {key} 必须是布尔值")
    return value


def _contains_placeholder(value: str) -> bool:
    lower = value.lower()
    return any(marker in lower for marker in _PLACEHOLDER_MARKERS)


def load_voice_clone_settings(
    path: str | Path, *, require_api_key: bool = True
) -> VoiceCloneSettings:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise VoiceCloneConfigurationError(f"无法读取配置文件：{config_path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise VoiceCloneConfigurationError("voice_clone.yaml 的 schema_version 必须为 1")
    allowed = {
        "schema_version",
        "aliyun",
        "service",
        "audio",
        "listing",
        "output",
        "recording",
        "translation_test",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise VoiceCloneConfigurationError(
            f"存在未知顶层配置项：{', '.join(sorted(unknown))}"
        )

    aliyun = _mapping(raw, "aliyun")
    service = _mapping(raw, "service")
    audio = _mapping(raw, "audio")
    listing = _mapping(raw, "listing")
    output = _mapping(raw, "output")
    recording = _mapping(raw, "recording")

    configured_key = _string(aliyun, "api_key")
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip() or configured_key
    if require_api_key and not api_key:
        raise VoiceCloneConfigurationError(
            "请在 aliyun.api_key 或 DASHSCOPE_API_KEY 中配置 API Key"
        )
    workspace_id = _string(aliyun, "workspace_id", required=True)
    if _contains_placeholder(workspace_id) or not re.fullmatch(
        r"[A-Za-z0-9_-]{2,128}", workspace_id
    ):
        raise VoiceCloneConfigurationError("请填写有效的 aliyun.workspace_id")
    region = _string(aliyun, "region", required=True)
    if region not in {"cn-beijing", "ap-southeast-1"}:
        raise VoiceCloneConfigurationError("aliyun.region 仅支持 cn-beijing 或 ap-southeast-1")

    enrollment_model = _string(service, "enrollment_model", required=True)
    if enrollment_model != "qwen-voice-enrollment":
        raise VoiceCloneConfigurationError(
            "service.enrollment_model 必须为 qwen-voice-enrollment"
        )
    target_model = _string(service, "target_model", required=True)
    if target_model != _TARGET_MODEL:
        raise VoiceCloneConfigurationError(
            f"service.target_model 必须为 {_TARGET_MODEL}"
        )
    preferred_name = _string(service, "preferred_name", required=True)
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", preferred_name):
        raise VoiceCloneConfigurationError(
            "service.preferred_name 仅允许数字、字母、下划线，长度 1-16"
        )

    audio_file_value = _string(audio, "file")
    audio_file = None
    if audio_file_value:
        candidate = Path(audio_file_value).expanduser()
        audio_file = candidate if candidate.is_absolute() else (config_path.parent / candidate)
        audio_file = audio_file.resolve()
    audio_url = _string(audio, "url")
    if audio_file is not None and audio_url:
        raise VoiceCloneConfigurationError("audio.file 和 audio.url 只能配置一个")
    mime_type = _string(audio, "mime_type", required=True)
    if mime_type != "auto" and mime_type not in _MIME_TYPES:
        raise VoiceCloneConfigurationError(
            "audio.mime_type 仅支持 auto/audio/wav/audio/mpeg/audio/mp4"
        )
    language = _string(audio, "language")
    if language and language not in _LANGUAGES:
        raise VoiceCloneConfigurationError("audio.language 不在官方支持列表中")

    output_format = _string(output, "format", required=True)
    if output_format not in {"table", "json"}:
        raise VoiceCloneConfigurationError("output.format 仅支持 table 或 json")

    page_index = _int(listing, "page_index", 0)
    page_size = _int(listing, "page_size", 10)
    if page_index > 1_000_000 or page_size > 1_000_000:
        raise VoiceCloneConfigurationError("listing 页码和页大小不能超过 1000000")

    return VoiceCloneSettings(
        config_path=config_path,
        api_key=api_key,
        workspace_id=workspace_id,
        region=region,  # type: ignore[arg-type]
        request_timeout_seconds=_float(aliyun, "request_timeout_seconds", 90),
        enrollment_model=enrollment_model,
        target_model=target_model,
        preferred_name=preferred_name,
        audio_file=audio_file,
        audio_url=audio_url,
        audio_mime_type=mime_type,
        transcript=_string(audio, "text"),
        language=language,
        max_encoded_bytes=_int(audio, "max_encoded_bytes", 10_000_000, minimum=1),
        validate_wav_metadata=_bool(audio, "validate_wav_metadata", True),
        page_index=page_index,
        page_size=page_size,
        output_format=output_format,  # type: ignore[arg-type]
        show_request_id=_bool(output, "show_request_id", True),
        recording_prompt=_string(recording, "prompt", required=True),
    )


def load_recording_prompt(path: str | Path) -> str:
    config_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise VoiceCloneConfigurationError(f"无法读取配置文件：{config_path}") from exc
    if not isinstance(raw, dict):
        raise VoiceCloneConfigurationError("声音复刻配置文件顶层必须是对象")
    recording = _mapping(raw, "recording")
    return _string(recording, "prompt", required=True)


def _mime_for_file(path: Path, configured: str) -> str:
    try:
        detected = _MIME_BY_SUFFIX[path.suffix.lower()]
    except KeyError as exc:
        raise AudioSampleError("本地音频仅支持 WAV、MP3 或 M4A") from exc
    if configured != "auto" and configured != detected:
        raise AudioSampleError("audio.mime_type 与本地文件扩展名不一致")
    return detected


def _validate_wav(path: Path) -> tuple[str, ...]:
    try:
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frame_count = stream.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioSampleError("WAV 文件无法读取或格式损坏") from exc
    if sample_width != 2:
        raise AudioSampleError("WAV 必须为 16-bit PCM")
    if channels != 1:
        raise AudioSampleError("LiveTranslate 声音复刻样本必须为单声道")
    if sample_rate < 24_000:
        raise AudioSampleError("LiveTranslate 声音复刻样本采样率必须不低于 24 kHz")
    duration = frame_count / sample_rate if sample_rate else 0
    if duration <= 0 or duration > 60:
        raise AudioSampleError("声音样本时长必须大于 0 且不超过 60 秒")
    if duration < 3:
        raise AudioSampleError("声音样本必须至少包含 3 秒连续清晰语音")
    warnings: list[str] = []
    if duration < 10 or duration > 20:
        warnings.append(f"当前 WAV 时长 {duration:.1f} 秒；官方推荐 10-20 秒")
    return tuple(warnings)


def prepare_audio_sample(
    settings: VoiceCloneSettings,
    *,
    file_path: str | Path | None = None,
    audio_url: str | None = None,
    mime_type: str | None = None,
) -> AudioSample:
    if file_path is not None:
        selected_file = Path(file_path).expanduser().resolve()
        selected_url = ""
    elif audio_url is not None:
        selected_file = None
        selected_url = audio_url.strip()
    else:
        selected_file = settings.audio_file
        selected_url = settings.audio_url
    if selected_file is not None and selected_url:
        raise AudioSampleError("本地文件和公网 URL 只能选择一种音频来源")
    if selected_url:
        parsed = urlparse(selected_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise AudioSampleError("audio.url 必须是不含凭据的公网 HTTPS URL")
        return AudioSample(data=selected_url, mime_type="url", source=selected_url)
    if selected_file is None:
        raise AudioSampleError("请通过 audio.file、audio.url 或 --audio 指定声音样本")
    if not selected_file.is_file():
        raise AudioSampleError(f"声音样本文件不存在：{selected_file}")

    selected_mime = _mime_for_file(selected_file, mime_type or settings.audio_mime_type)
    raw_size = selected_file.stat().st_size
    if raw_size <= 0:
        raise AudioSampleError("声音样本文件为空")
    encoded_size = len(f"data:{selected_mime};base64,") + 4 * math.ceil(raw_size / 3)
    if encoded_size >= settings.max_encoded_bytes:
        raise AudioSampleError(
            f"声音样本编码后约 {encoded_size} bytes，必须小于 {settings.max_encoded_bytes}"
        )
    warnings: tuple[str, ...] = ()
    if selected_mime == "audio/wav" and settings.validate_wav_metadata:
        warnings = _validate_wav(selected_file)
    elif selected_mime != "audio/wav":
        warnings = ("MP3/M4A 的采样率、声道和时长将由百炼服务端校验",)
    encoded = base64.b64encode(selected_file.read_bytes()).decode("ascii")
    return AudioSample(
        data=f"data:{selected_mime};base64,{encoded}",
        mime_type=selected_mime,
        source=str(selected_file),
        warnings=warnings,
    )


def build_create_payload(
    settings: VoiceCloneSettings,
    sample: AudioSample,
    *,
    preferred_name: str | None = None,
    target_model: str | None = None,
    transcript: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    name = preferred_name or settings.preferred_name
    model = target_model or settings.target_model
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", name):
        raise VoiceCloneConfigurationError("音色名称仅允许数字、字母、下划线，长度 1-16")
    if model != _TARGET_MODEL:
        raise VoiceCloneConfigurationError(f"target_model 必须为 {_TARGET_MODEL}")
    selected_language = settings.language if language is None else language
    if selected_language and selected_language not in _LANGUAGES:
        raise VoiceCloneConfigurationError("language 不在官方支持列表中")
    input_body: dict[str, Any] = {
        "action": "create",
        "target_model": model,
        "preferred_name": name,
        "audio": {"data": sample.data},
    }
    selected_text = settings.transcript if transcript is None else transcript.strip()
    if selected_text:
        input_body["text"] = selected_text
    if selected_language:
        input_body["language"] = selected_language
    return {"model": settings.enrollment_model, "input": input_body}


class VoiceCloneClient:
    def __init__(self, settings: VoiceCloneSettings, *, session: Any | None = None) -> None:
        self.settings = settings
        self._session = session or requests.Session()

    def create(self, payload: dict[str, Any]) -> CreatedVoice:
        data = self._post(payload)
        output = data.get("output")
        if not isinstance(output, dict):
            raise VoiceCloneApiError("创建响应缺少 output", request_id=_request_id(data))
        voice = output.get("voice")
        target_model = output.get("target_model")
        if not isinstance(voice, str) or not voice:
            raise VoiceCloneApiError("创建响应缺少 voice", request_id=_request_id(data))
        if not isinstance(target_model, str) or not target_model:
            raise VoiceCloneApiError(
                "创建响应缺少 target_model", request_id=_request_id(data)
            )
        return CreatedVoice(
            voice=voice,
            target_model=target_model,
            request_id=_request_id(data),
            billed_count=_usage_count(data),
        )

    def list(self, *, page_index: int, page_size: int) -> VoiceListResult:
        payload = {
            "model": self.settings.enrollment_model,
            "input": {"action": "list", "page_size": page_size, "page_index": page_index},
        }
        data = self._post(payload)
        output = data.get("output")
        voice_list = output.get("voice_list") if isinstance(output, dict) else None
        if not isinstance(voice_list, list):
            raise VoiceCloneApiError("列表响应缺少 output.voice_list", request_id=_request_id(data))
        records: list[VoiceRecord] = []
        for item in voice_list:
            if not isinstance(item, dict):
                raise VoiceCloneApiError("列表响应包含无效音色记录", request_id=_request_id(data))
            voice = item.get("voice")
            created = item.get("gmt_create")
            model = item.get("target_model")
            if not all(isinstance(value, str) for value in (voice, created, model)):
                raise VoiceCloneApiError("音色记录字段类型错误", request_id=_request_id(data))
            records.append(VoiceRecord(voice=voice, gmt_create=created, target_model=model))
        return VoiceListResult(voices=tuple(records), request_id=_request_id(data))

    def delete(self, voice: str) -> DeleteResult:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", voice):
            raise VoiceCloneConfigurationError("待删除的 voice 格式无效")
        payload = {
            "model": self.settings.enrollment_model,
            "input": {"action": "delete", "voice": voice},
        }
        data = self._post(payload)
        return DeleteResult(voice=voice, request_id=_request_id(data))

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._session.post(
                self.settings.endpoint,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.settings.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise VoiceCloneApiError(f"网络请求失败：{type(exc).__name__}") from exc
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise VoiceCloneApiError(
                "服务端返回了非 JSON 响应", status_code=getattr(response, "status_code", None)
            ) from exc
        if not isinstance(data, dict):
            raise VoiceCloneApiError(
                "服务端 JSON 顶层不是对象", status_code=getattr(response, "status_code", None)
            )
        status_code = int(getattr(response, "status_code", 0))
        if not 200 <= status_code < 300 or "code" in data:
            code = str(data.get("code", "HTTP_ERROR"))[:100]
            message = _safe_service_message(
                str(data.get("message", "百炼接口请求失败")), self.settings.api_key
            )
            raise VoiceCloneApiError(
                f"{code}: {message}",
                status_code=status_code,
                request_id=_request_id(data),
                error_code=code,
            )
        return data


def _request_id(data: dict[str, Any]) -> str:
    value = data.get("request_id", "")
    return value if isinstance(value, str) else ""


def _usage_count(data: dict[str, Any]) -> int:
    usage = data.get("usage")
    value = usage.get("count", 0) if isinstance(usage, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _safe_service_message(message: str, api_key: str) -> str:
    value = message[:1000]
    if api_key:
        value = value.replace(api_key, "[REDACTED]")
    value = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", value, flags=re.IGNORECASE)
    value = re.sub(r"data:audio/[^;,]+;base64,[A-Za-z0-9+/=]+", "[AUDIO REDACTED]", value)
    return value[:300]
