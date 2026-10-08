from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import imageio_ffmpeg
import websockets
import yaml

from .client import VoiceCloneConfigurationError, load_voice_clone_settings


class TranslationTestError(RuntimeError):
    """文件翻译测试失败，错误文本不得包含密钥或原始音频。"""


@dataclass(frozen=True, slots=True)
class TranslationTestSettings:
    config_path: Path
    api_key: str = field(repr=False)
    workspace_id: str
    region: str
    model: str
    voice_id: str
    input_file: Path
    output_file: Path
    source_language: str
    target_language: str
    chunk_ms: int
    realtime_pacing: bool
    configure_timeout_seconds: float
    finish_timeout_seconds: float

    @property
    def websocket_url(self) -> str:
        host = {
            "cn-beijing": "cn-beijing.maas.aliyuncs.com",
            "ap-southeast-1": "ap-southeast-1.maas.aliyuncs.com",
        }[self.region]
        query = urlencode({"model": self.model})
        return f"wss://{self.workspace_id}.{host}/api-ws/v1/realtime?{query}"


@dataclass(slots=True)
class TranslationResult:
    pcm: bytearray = field(default_factory=bytearray)
    source_transcripts: list[str] = field(default_factory=list)
    translation_transcripts: list[str] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    event_counts: Counter[str] = field(default_factory=Counter)
    usage: dict[str, Any] = field(default_factory=dict)


def _event_id() -> str:
    return f"event_{uuid.uuid4().hex}"


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoiceCloneConfigurationError(f"{name} 必须是对象")
    return value


def _required_string(section: dict[str, Any], key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise VoiceCloneConfigurationError(f"translation_test.{key} 必须是非空字符串")
    return value.strip()


def _resolve(config_path: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate.resolve()


def load_translation_test_settings(path: str | Path) -> TranslationTestSettings:
    base = load_voice_clone_settings(path)
    try:
        raw = yaml.safe_load(base.config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise VoiceCloneConfigurationError("无法读取翻译测试配置") from exc
    root = _mapping(raw, "配置文件")
    section = _mapping(root.get("translation_test"), "translation_test")

    voice_id = _required_string(section, "voice_id")
    if not voice_id.startswith("qwen-translate-vc-") or "请替换" in voice_id:
        raise VoiceCloneConfigurationError(
            "translation_test.voice_id 必须是有效的 qwen-translate-vc-* 音色"
        )
    input_file = _resolve(base.config_path, _required_string(section, "input_file"))
    output_file = _resolve(base.config_path, _required_string(section, "output_file"))
    if not input_file.is_file():
        raise VoiceCloneConfigurationError(f"翻译测试输入文件不存在：{input_file}")
    if input_file == output_file:
        raise VoiceCloneConfigurationError("翻译测试输入和输出文件不能相同")

    chunk_ms = section.get("chunk_ms", 100)
    if isinstance(chunk_ms, bool) or not isinstance(chunk_ms, int) or not 20 <= chunk_ms <= 1000:
        raise VoiceCloneConfigurationError("translation_test.chunk_ms 必须在 20-1000")
    realtime_pacing = section.get("realtime_pacing", True)
    if not isinstance(realtime_pacing, bool):
        raise VoiceCloneConfigurationError("translation_test.realtime_pacing 必须是布尔值")

    def timeout(name: str, default: float) -> float:
        value = section.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise VoiceCloneConfigurationError(f"translation_test.{name} 必须是正数")
        return float(value)

    return TranslationTestSettings(
        config_path=base.config_path,
        api_key=base.api_key,
        workspace_id=base.workspace_id,
        region=base.region,
        model=base.target_model,
        voice_id=voice_id,
        input_file=input_file,
        output_file=output_file,
        source_language=_required_string(section, "source_language"),
        target_language=_required_string(section, "target_language"),
        chunk_ms=chunk_ms,
        realtime_pacing=realtime_pacing,
        configure_timeout_seconds=timeout("configure_timeout_seconds", 15),
        finish_timeout_seconds=timeout("finish_timeout_seconds", 60),
    )


def _prepare_input_pcm(source: Path) -> tuple[bytes, float]:
    with tempfile.TemporaryDirectory(prefix="livetranslate-input-") as temp_dir:
        converted = Path(temp_dir) / "input-16k-mono.wav"
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(converted),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode:
            detail = completed.stderr.strip()[-500:]
            raise TranslationTestError(f"输入音频转换失败：{detail}")
        try:
            with wave.open(str(converted), "rb") as stream:
                if (
                    stream.getnchannels() != 1
                    or stream.getsampwidth() != 2
                    or stream.getframerate() != 16_000
                ):
                    raise TranslationTestError("转换后的输入不是 16 kHz 单声道 PCM16")
                pcm = stream.readframes(stream.getnframes())
                duration = stream.getnframes() / 16_000
        except (OSError, EOFError, wave.Error) as exc:
            raise TranslationTestError("无法读取转换后的输入音频") from exc
    if not pcm:
        raise TranslationTestError("转换后的输入音频为空")
    return pcm, duration


def _session_update(settings: TranslationTestSettings) -> dict[str, Any]:
    return {
        "event_id": _event_id(),
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "sample_rate": 16000,
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            "input_audio_transcription": {
                "model": "qwen3-asr-flash-realtime",
                "language": settings.source_language,
            },
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "silence_duration_ms": 500,
            },
            "translation": {"language": settings.target_language},
            "voice": settings.voice_id,
            "enable_voice_clone": True,
            "voice_clone_options": {"frequency": "never"},
        },
    }


def _validate_session(event: dict[str, Any], settings: TranslationTestSettings) -> None:
    session = event.get("session")
    if not isinstance(session, dict):
        raise TranslationTestError("session.updated 缺少 session")
    checks = {
        "model": settings.model,
        "sample_rate": 16000,
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "voice": settings.voice_id,
        "enable_voice_clone": True,
    }
    for key, expected in checks.items():
        if session.get(key) != expected:
            raise TranslationTestError(f"服务端未确认会话字段：{key}")
    options = session.get("voice_clone_options")
    if not isinstance(options, dict) or options.get("frequency") != "never":
        raise TranslationTestError("服务端未确认固定音色模式")


async def _receive(
    ws: Any,
    settings: TranslationTestSettings,
    configured: asyncio.Event,
    finished: asyncio.Event,
    result: TranslationResult,
) -> None:
    async for message in ws:
        if not isinstance(message, str):
            raise TranslationTestError("服务端返回了不支持的二进制消息")
        try:
            event = json.loads(message)
        except json.JSONDecodeError as exc:
            raise TranslationTestError("服务端返回无效 JSON") from exc
        if not isinstance(event, dict):
            raise TranslationTestError("服务端事件顶层不是对象")
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise TranslationTestError("服务端事件缺少 type")
        result.event_counts[event_type] += 1
        request_id = event.get("request_id")
        if isinstance(request_id, str) and request_id not in result.request_ids:
            result.request_ids.append(request_id)

        if event_type == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            code = str(error.get("code", "unknown"))[:100]
            param = str(error.get("param", ""))[:100]
            raise TranslationTestError(f"百炼服务错误：{code}；参数：{param}")
        if event_type == "session.updated":
            _validate_session(event, settings)
            configured.set()
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript")
            if isinstance(transcript, str) and transcript:
                result.source_transcripts.append(transcript)
        elif event_type == "response.audio_transcript.done":
            transcript = event.get("transcript")
            if isinstance(transcript, str) and transcript:
                result.translation_transcripts.append(transcript)
        elif event_type == "response.audio.delta":
            encoded = event.get("delta")
            if not isinstance(encoded, str) or not encoded:
                raise TranslationTestError("音频增量事件缺少 delta")
            try:
                pcm = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise TranslationTestError("音频增量不是有效 Base64") from exc
            if not pcm or len(pcm) % 2:
                raise TranslationTestError("音频增量不是有效 PCM16")
            result.pcm.extend(pcm)
            if len(result.pcm) > 20_000_000:
                raise TranslationTestError("输出音频超过 20 MB 安全上限")
        elif event_type == "response.done":
            response = event.get("response")
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                result.usage = response["usage"]
        elif event_type == "session.finished":
            finished.set()
            return


async def _wait_event_or_receiver(
    event: asyncio.Event,
    receiver: asyncio.Task[None],
    timeout: float,
    name: str,
) -> None:
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, receiver}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if event.is_set():
            return
        if receiver in done:
            await receiver
            raise TranslationTestError(f"接收循环在 {name} 前结束")
        raise TranslationTestError(f"等待 {name} 超时")
    finally:
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def _stream_pcm(ws: Any, pcm: bytes, settings: TranslationTestSettings) -> None:
    frames_per_chunk = 16_000 * settings.chunk_ms // 1000
    bytes_per_chunk = frames_per_chunk * 2
    chunk_seconds = frames_per_chunk / 16_000
    started = time.monotonic()
    for index, offset in enumerate(range(0, len(pcm), bytes_per_chunk), start=1):
        chunk = pcm[offset : offset + bytes_per_chunk]
        event = {
            "event_id": _event_id(),
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(chunk).decode("ascii"),
        }
        await ws.send(json.dumps(event))
        if settings.realtime_pacing:
            target = started + index * chunk_seconds
            await asyncio.sleep(max(0, target - time.monotonic()))


def _write_output(path: Path, pcm: bytes) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24_000)
        stream.writeframes(pcm)
    return len(pcm) / (24_000 * 2)


async def run_translation_test(settings: TranslationTestSettings) -> dict[str, Any]:
    pcm, input_duration = _prepare_input_pcm(settings.input_file)
    configured = asyncio.Event()
    finished = asyncio.Event()
    result = TranslationResult()
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    try:
        ws = await websockets.connect(
            settings.websocket_url,
            additional_headers=headers,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=4_194_304,
        )
    except Exception as exc:
        raise TranslationTestError(f"WebSocket 连接失败：{type(exc).__name__}") from exc

    receiver = asyncio.create_task(
        _receive(ws, settings, configured, finished, result)
    )
    started = time.monotonic()
    try:
        await ws.send(json.dumps(_session_update(settings), ensure_ascii=False))
        await _wait_event_or_receiver(
            configured,
            receiver,
            settings.configure_timeout_seconds,
            "session.updated",
        )
        await _stream_pcm(ws, pcm, settings)
        await ws.send(json.dumps({"event_id": _event_id(), "type": "session.finish"}))
        await _wait_event_or_receiver(
            finished,
            receiver,
            settings.finish_timeout_seconds,
            "session.finished",
        )
    finally:
        if not receiver.done():
            receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await ws.close()

    if not result.pcm:
        raise TranslationTestError("服务端未返回翻译音频")
    output_duration = _write_output(settings.output_file, bytes(result.pcm))
    summary = {
        "status": "success",
        "model": settings.model,
        "voice_id": settings.voice_id,
        "source_language": settings.source_language,
        "target_language": settings.target_language,
        "input_file": str(settings.input_file),
        "input_duration_seconds": round(input_duration, 3),
        "output_file": str(settings.output_file),
        "output_bytes": len(result.pcm),
        "output_duration_seconds": round(output_duration, 3),
        "source_transcript": " ".join(result.source_transcripts),
        "translation_transcript": " ".join(result.translation_transcripts),
        "request_ids": result.request_ids,
        "usage": result.usage,
        "event_counts": dict(sorted(result.event_counts.items())),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    summary_path = settings.output_file.with_suffix(".json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary["summary_file"] = str(summary_path)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将本地音频发送到 LiveTranslate，并保存固定克隆音色的译音"
    )
    parser.add_argument("--config", type=Path, default=Path("config/voice_clone.yaml"))
    parser.add_argument("--yes", action="store_true", help="确认执行真实云端翻译")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_translation_test_settings(args.config)
        if not args.yes:
            if not sys.stdin.isatty():
                print("真实云端翻译需要确认；非交互模式请添加 --yes", file=sys.stderr)
                return 5
            answer = input("将产生一次实时翻译调用，是否继续 [y/N]: ").strip().casefold()
            if answer not in {"y", "yes"}:
                return 5
        summary = asyncio.run(run_translation_test(settings))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except VoiceCloneConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except TranslationTestError as exc:
        print(f"翻译测试失败：{exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("操作已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
