from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic_ns
from typing import Any

from .buffers import PcmRingBuffer
from .config import AppSettings, SessionSettings
from .models import PushIssue, PushState, StatusEvent, SubtitleUpdate


class ProtocolError(RuntimeError):
    pass


class CloudServiceError(RuntimeError):
    def __init__(self, *, error_type: str, code: str, message: str, param: str) -> None:
        super().__init__(f"{error_type}:{code}:{param}")
        self.error_type = error_type
        self.code = code
        self.safe_message = message[:200]
        self.param = param

    @property
    def issue(self) -> PushIssue:
        value = f"{self.error_type} {self.code}".lower()
        if any(token in value for token in ("auth", "api_key", "unauthorized")):
            return PushIssue.AUTHENTICATION
        if "permission" in value or "forbidden" in value:
            return PushIssue.PERMISSION
        if "rate" in value or "quota" in value or "thrott" in value:
            return PushIssue.RATE_LIMIT
        if "invalid" in value or "request" in value:
            return PushIssue.INVALID_CONFIGURATION
        return PushIssue.SERVICE_UNAVAILABLE


def new_event_id() -> str:
    return f"event_{uuid.uuid4().hex}"


def build_session_update(app: AppSettings, session: SessionSettings) -> dict[str, Any]:
    body: dict[str, Any] = {
        "modalities": list(session.modalities),
        "sample_rate": app.audio.input_sample_rate_hz,
        "input_audio_format": session.input_audio_format,
        "turn_detection": {
            "type": app.vad.type,
            "threshold": app.vad.threshold,
            "silence_duration_ms": app.vad.silence_duration_ms,
        },
        "translation": {
            "language": session.target_language,
            "same_language_skip_options": {
                "skip_text": session.same_language_skip_text,
                "skip_audio": session.same_language_skip_audio,
            },
        },
    }
    if session.transcription_model:
        transcription: dict[str, Any] = {"model": session.transcription_model}
        if session.source_language:
            transcription["language"] = session.source_language
        body["input_audio_transcription"] = transcription
    if session.terminology:
        body["translation"]["corpus"] = {"phrases": session.terminology}
    if "audio" in session.modalities:
        body.update(
            {
                "output_audio_format": session.output_audio_format,
                "voice": session.voice_id,
                "enable_voice_clone": session.enable_voice_clone,
            }
        )
        if session.enable_voice_clone:
            body["voice_clone_options"] = {
                "frequency": session.voice_clone_frequency or app.voice.clone_frequency
            }
    return {"event_id": new_event_id(), "type": "session.update", "session": body}


def build_audio_append(pcm: bytes) -> dict[str, str]:
    if not pcm or len(pcm) % 2:
        raise ValueError("PCM16 音频块必须为非空偶数字节")
    return {
        "event_id": new_event_id(),
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm).decode("ascii"),
    }


def build_session_finish() -> dict[str, str]:
    return {"event_id": new_event_id(), "type": "session.finish"}


@dataclass(slots=True)
class _MutableSegment:
    source_item_id: str
    translation_item_id: str | None = None
    source_confirmed: str = ""
    source_stash: str = ""
    translation_confirmed: str = ""
    translation_stash: str = ""
    source_final: bool = False
    translation_final: bool = False

    def snapshot(self, channel: str) -> SubtitleUpdate:
        return SubtitleUpdate(
            channel=channel,  # type: ignore[arg-type]
            source_item_id=self.source_item_id,
            translation_item_id=self.translation_item_id,
            source_confirmed=self.source_confirmed,
            source_stash=self.source_stash,
            translation_confirmed=self.translation_confirmed,
            translation_stash=self.translation_stash,
            source_final=self.source_final,
            translation_final=self.translation_final,
        )


class CloudEventProcessor:
    """把阿里云 LiveTranslate 服务端事件转换为无敏感载荷的领域事件。"""

    def __init__(
        self,
        *,
        session: SessionSettings,
        expected_model: str,
        audio_buffer: PcmRingBuffer | None,
        on_status: Callable[[StatusEvent], None],
        on_subtitle: Callable[[SubtitleUpdate], None],
        on_configured: Callable[[], None],
        on_finished: Callable[[], None],
        target_language_guard_enabled: bool = False,
    ) -> None:
        self.session = session
        self.expected_model = expected_model
        self.audio_buffer = audio_buffer
        self.on_status = on_status
        self.on_subtitle = on_subtitle
        self.on_configured = on_configured
        self.on_finished = on_finished
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._segments: dict[str, _MutableSegment] = {}
        self._translation_to_source: dict[str, str] = {}
        self._pending_translation: dict[str, tuple[str, str, bool]] = {}
        self._speech_started_ns: int | None = None
        self._subtitle_latency_reported = False
        self._audio_latency_reported = False
        self._discarded_audio_chunks = 0
        self._discarded_audio_bytes = 0
        self._language_guard_enabled = bool(
            target_language_guard_enabled
            and session.channel == "local"
            and session.target_language == "en"
            and "audio" in session.modalities
        )
        self._guard_pending_audio: OrderedDict[str, bytearray] = OrderedDict()
        self._guard_approved: set[str] = set()
        self._guard_blocked: set[str] = set()
        self._guard_max_bytes = 24_000 * 2 * 3

    def reset(self) -> None:
        self._seen.clear()
        self._segments.clear()
        self._translation_to_source.clear()
        self._pending_translation.clear()
        self._speech_started_ns = None
        self._subtitle_latency_reported = False
        self._audio_latency_reported = False
        self._discarded_audio_chunks = 0
        self._discarded_audio_bytes = 0
        self._guard_pending_audio.clear()
        self._guard_approved.clear()
        self._guard_blocked.clear()

    def process_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            raise ProtocolError("服务端返回了不支持的二进制 WebSocket 消息")
        try:
            event = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ProtocolError("服务端消息不是有效 JSON") from exc
        if not isinstance(event, dict):
            raise ProtocolError("服务端事件顶层必须是对象")
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ProtocolError("服务端事件缺少 type")
        event_id = event.get("event_id")
        if isinstance(event_id, str) and self._is_duplicate(event_id):
            return

        if event_type == "error":
            error = event.get("error")
            if not isinstance(error, dict):
                raise ProtocolError("error 事件缺少 error 对象")
            raise CloudServiceError(
                error_type=str(error.get("type", "unknown")),
                code=str(error.get("code", "unknown")),
                message=str(error.get("message", "cloud request failed")),
                param=str(error.get("param", "")),
            )
        if event_type == "session.updated":
            self._validate_session_updated(event)
            self.on_configured()
            return
        if event_type == "session.finished":
            self.on_finished()
            return
        if event_type == "input_audio_buffer.speech_started":
            self._speech_started_ns = monotonic_ns()
            self._subtitle_latency_reported = False
            self._audio_latency_reported = False
            self._emit_status("cloud_vad", "speech_started")
            return
        if event_type == "input_audio_buffer.speech_stopped":
            self._emit_status("cloud_vad", "speech_stopped")
            return
        if event_type == "conversation.item.created":
            self._handle_item_created(event)
            return
        if event_type == "conversation.item.input_audio_transcription.text":
            self._handle_source_text(event, final=False)
            return
        if event_type == "conversation.item.input_audio_transcription.completed":
            self._handle_source_text(event, final=True)
            return
        if event_type == "conversation.item.input_audio_transcription.failed":
            self._emit_status("cloud_asr", "failed", PushIssue.SERVICE_UNAVAILABLE)
            return
        if event_type in {"response.text.text", "response.audio_transcript.text"}:
            self._handle_translation(event, final=False)
            return
        if event_type in {"response.text.done", "response.audio_transcript.done"}:
            self._handle_translation(event, final=True)
            return
        if event_type == "response.audio.delta":
            self._handle_audio(event)
            return
        if event_type == "response.audio.done":
            self._finish_audio(event)
            return
        if event_type in {
            "session.created",
            "response.created",
            "response.done",
            "response.output_item.added",
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
        }:
            return
        self._emit_status("cloud_protocol", "unknown_event", PushIssue.NONE, unknown_events=1)

    def _is_duplicate(self, event_id: str) -> bool:
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            return True
        self._seen[event_id] = None
        if len(self._seen) > 1000:
            self._seen.popitem(last=False)
        return False

    def _validate_session_updated(self, event: dict[str, Any]) -> None:
        server = event.get("session")
        if not isinstance(server, dict):
            raise ProtocolError("session.updated 缺少 session")
        if server.get("model") != self.expected_model:
            raise ProtocolError("服务端确认的模型与配置不一致")
        if int(server.get("sample_rate", 0)) != 16000:
            raise ProtocolError("服务端确认的输入采样率不是 16000 Hz")
        if server.get("input_audio_format") != "pcm":
            raise ProtocolError("服务端确认的输入格式不是 pcm")
        modalities = server.get("modalities")
        if not isinstance(modalities, list) or set(modalities) != set(self.session.modalities):
            raise ProtocolError("服务端确认的输出模态与配置不一致")
        translation = server.get("translation")
        if (
            not isinstance(translation, dict)
            or translation.get("language") != self.session.target_language
        ):
            raise ProtocolError("服务端确认的目标语种与配置不一致")
        transcription = server.get("input_audio_transcription")
        if not isinstance(transcription, dict):
            raise ProtocolError("服务端未确认源语言识别配置")
        if transcription.get("model") != "qwen3-asr-flash-realtime":
            raise ProtocolError("服务端确认的原文识别模型不一致")
        if transcription.get("language") != self.session.source_language:
            raise ProtocolError("服务端确认的源语种与配置不一致")
        if "audio" in self.session.modalities:
            if server.get("output_audio_format") != "pcm":
                raise ProtocolError("服务端确认的输出音频格式不是 pcm")
            if server.get("enable_voice_clone") is not True:
                raise ProtocolError("服务端未确认声音复刻")
            if server.get("voice") != self.session.voice_id:
                raise ProtocolError("服务端确认的固定音色与配置不一致")
            clone_options = server.get("voice_clone_options")
            if not isinstance(clone_options, dict) or clone_options.get("frequency") != "never":
                raise ProtocolError("服务端确认的声音复刻频率不是 never")

    def _handle_item_created(self, event: dict[str, Any]) -> None:
        previous = event.get("previous_item_id")
        item = event.get("item")
        if not isinstance(previous, str) or not isinstance(item, dict):
            return
        translation_id = item.get("id")
        if not isinstance(translation_id, str):
            raise ProtocolError("conversation.item.created 缺少 item.id")
        self._translation_to_source[translation_id] = previous
        segment = self._segments.setdefault(previous, _MutableSegment(previous))
        segment.translation_item_id = translation_id
        pending = self._pending_translation.pop(translation_id, None)
        if pending:
            confirmed, stash, final = pending
            segment.translation_confirmed = confirmed
            segment.translation_stash = stash
            segment.translation_final = final
        self.on_subtitle(segment.snapshot(self.session.channel))

    def _handle_source_text(self, event: dict[str, Any], *, final: bool) -> None:
        item_id = event.get("item_id")
        if not isinstance(item_id, str):
            raise ProtocolError("原文事件缺少 item_id")
        segment = self._segments.setdefault(item_id, _MutableSegment(item_id))
        if segment.source_final and not final:
            return
        if final:
            value = event.get("transcript")
            if not isinstance(value, str):
                raise ProtocolError("原文完成事件缺少 transcript")
            segment.source_confirmed = value
            segment.source_stash = ""
            segment.source_final = True
        else:
            text = event.get("text", "")
            stash = event.get("stash", "")
            if not isinstance(text, str) or not isinstance(stash, str):
                raise ProtocolError("原文流式事件 text/stash 类型错误")
            segment.source_confirmed = text
            segment.source_stash = stash
        self.on_subtitle(segment.snapshot(self.session.channel))

    def _handle_translation(self, event: dict[str, Any], *, final: bool) -> None:
        translation_id = event.get("item_id")
        if not isinstance(translation_id, str):
            raise ProtocolError("译文事件缺少 item_id")
        if final:
            key = "text" if event.get("type") == "response.text.done" else "transcript"
            value = event.get(key)
            if not isinstance(value, str):
                raise ProtocolError("译文完成事件缺少完整文本")
            confirmed, stash = value, ""
        else:
            confirmed = event.get("text", "")
            stash = event.get("stash", "")
            if not isinstance(confirmed, str) or not isinstance(stash, str):
                raise ProtocolError("译文流式事件 text/stash 类型错误")
        self._update_audio_language_guard(translation_id, confirmed + stash, final=final)
        if (confirmed or stash) and not self._subtitle_latency_reported:
            self._report_latency("subtitle_first")
            self._subtitle_latency_reported = True
        source_id = self._translation_to_source.get(translation_id)
        if source_id is None:
            if len(self._pending_translation) >= 100:
                oldest = next(iter(self._pending_translation))
                self._pending_translation.pop(oldest)
                self._emit_status(
                    "cloud_protocol",
                    "pending_overflow",
                    PushIssue.PROTOCOL_INVALID,
                    pending_dropped=1,
                )
            self._pending_translation[translation_id] = (confirmed, stash, final)
            return
        segment = self._segments.setdefault(source_id, _MutableSegment(source_id))
        if segment.translation_final and not final:
            return
        segment.translation_item_id = translation_id
        segment.translation_confirmed = confirmed
        segment.translation_stash = stash
        segment.translation_final = final
        self.on_subtitle(segment.snapshot(self.session.channel))

    def _handle_audio(self, event: dict[str, Any]) -> None:
        if "audio" not in self.session.modalities:
            raise ProtocolError("text-only 会话收到了音频事件")
        encoded = event.get("delta")
        if not isinstance(encoded, str) or not encoded:
            raise ProtocolError("response.audio.delta 缺少 delta")
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProtocolError("response.audio.delta 不是有效 Base64") from exc
        if not pcm or len(pcm) % 2:
            raise ProtocolError("response.audio.delta 不是有效 PCM16 字节")
        if not self._audio_latency_reported:
            self._report_latency("audio_first")
            self._audio_latency_reported = True
        if self._language_guard_enabled:
            item_id = event.get("item_id")
            if not isinstance(item_id, str):
                raise ProtocolError("response.audio.delta 缺少 item_id")
            if item_id in self._guard_blocked:
                return
            if item_id not in self._guard_approved:
                pending = self._guard_pending_audio.setdefault(item_id, bytearray())
                pending.extend(pcm)
                if len(pending) == len(pcm):
                    self._emit_status(
                        "audio_language_guard",
                        "waiting_translation",
                        buffered_bytes=len(pending),
                    )
                if len(pending) > self._guard_max_bytes:
                    discarded = len(self._guard_pending_audio.pop(item_id))
                    self._guard_blocked.add(item_id)
                    self._emit_status(
                        "audio_language_guard",
                        "blocked_no_translation",
                        PushIssue.PROTOCOL_INVALID,
                        discarded_bytes=discarded,
                    )
                return
        self._write_audio(pcm)

    def _write_audio(self, pcm: bytes) -> None:
        if self.audio_buffer is None:
            self._discarded_audio_chunks += 1
            self._discarded_audio_bytes += len(pcm)
            if self._discarded_audio_chunks == 1 or self._discarded_audio_chunks % 10 == 0:
                self._emit_status(
                    "cloud_audio",
                    "received_no_output",
                    received_chunks=self._discarded_audio_chunks,
                    discarded_bytes=self._discarded_audio_bytes,
                )
            return
        dropped = self.audio_buffer.write(pcm)
        if dropped:
            self._emit_status(
                "cloud_audio",
                PushState.BACKPRESSURE.value,
                PushIssue.QUEUE_BACKPRESSURE,
                output_dropped_bytes=dropped,
            )
        else:
            self._emit_status("cloud_audio", "audio_received")

    def _update_audio_language_guard(self, item_id: str, text: str, *, final: bool) -> None:
        if not self._language_guard_enabled or item_id in self._guard_approved:
            return
        decision = self._english_text_decision(text, final=final)
        if decision is True:
            self._guard_approved.add(item_id)
            pending = bytes(self._guard_pending_audio.pop(item_id, b""))
            if pending:
                self._write_audio(pending)
            self._emit_status(
                "audio_language_guard",
                "approved_english",
                released_bytes=len(pending),
            )
        elif decision is False:
            discarded = len(self._guard_pending_audio.pop(item_id, b""))
            self._guard_blocked.add(item_id)
            self._emit_status(
                "audio_language_guard",
                "blocked_non_english",
                PushIssue.PROTOCOL_INVALID,
                discarded_bytes=discarded,
            )
        self._trim_language_guard_state()

    def _finish_audio(self, event: dict[str, Any]) -> None:
        if not self._language_guard_enabled:
            return
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or item_id not in self._guard_pending_audio:
            return
        discarded = len(self._guard_pending_audio.pop(item_id))
        self._guard_blocked.add(item_id)
        self._emit_status(
            "audio_language_guard",
            "blocked_no_transcript",
            PushIssue.PROTOCOL_INVALID,
            discarded_bytes=discarded,
        )
        self._trim_language_guard_state()

    def _trim_language_guard_state(self) -> None:
        while len(self._guard_pending_audio) > 8:
            self._guard_pending_audio.popitem(last=False)
        while len(self._guard_approved) > 100:
            self._guard_approved.pop()
        while len(self._guard_blocked) > 100:
            self._guard_blocked.pop()

    @staticmethod
    def _english_text_decision(text: str, *, final: bool) -> bool | None:
        latin = sum(1 for char in text if char.isascii() and char.isalpha())
        cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        if not final:
            return True if latin >= 4 and latin >= cjk else None
        return cjk <= latin

    def _report_latency(self, state: str) -> None:
        if self._speech_started_ns is None:
            return
        latency_ms = (monotonic_ns() - self._speech_started_ns) / 1_000_000
        self.on_status(
            StatusEvent(
                component="latency",
                channel=self.session.channel,
                state=state,
                counters={"server_vad_to_first_ms": round(latency_ms, 1)},
            )
        )

    def _emit_status(
        self,
        component: str,
        state: str,
        issue: PushIssue = PushIssue.NONE,
        **counters: int,
    ) -> None:
        self.on_status(
            StatusEvent(
                component=component,
                channel=self.session.channel,
                state=state,
                category=issue.value,
                counters=counters,
            )
        )
