from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_interpreter.buffers import DropOldestQueue, PcmRingBuffer
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
from ai_interpreter.models import AudioFrame, PushState
from ai_interpreter.session import LiveTranslateSession


def _settings() -> AppSettings:
    selector = DeviceSelector("WASAPI", "device", False)
    remote = SessionSettings("remote", "en", "zh", ("text",), None, {})
    local = SessionSettings(
        "local", "zh", "en", ("text", "audio"), "qwen-translate-vc-test", {}
    )
    return AppSettings(
        config_path=Path("settings.yaml"),
        mode="full_duplex",
        language_preset="local_zh_remote_en",
        aliyun=AliyunSettings(
            api_key="test-secret",
            region="cn-beijing",
            workspace_id="ws-test",
            model="qwen3.5-livetranslate-flash-realtime",
            connect_timeout_seconds=1,
            configure_timeout_seconds=1,
            finish_timeout_seconds=1,
            max_message_bytes=100_000,
        ),
        voice=VoiceSettings("qwen-translate-vc-test", "never"),
        audio=AudioSettings(selector, selector, selector, 16000, 24000, 100, -48, 3000),
        vad=VadSettings("server_vad", 0.2, 500),
        queues=QueueSettings(20, 3000),
        reconnect_delays_seconds=(0.01,),
        logging=LoggingSettings("INFO", Path("logs"), 1, 1000),
        web=WebSettings("127.0.0.1", 8765, False),
        subtitle=SubtitleSettings(True, True, 28, 50),
        remote_session=remote,
        local_session=local,
    )


class FakeWebSocket:
    def __init__(self, app: AppSettings) -> None:
        self.app = app
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        event = json.loads(message)
        self.sent.append(event)
        if event["type"] == "session.update":
            await self.incoming.put(
                json.dumps(
                    {
                        "event_id": "configured",
                        "type": "session.updated",
                        "session": {
                            "model": self.app.aliyun.model,
                            "sample_rate": 16000,
                            "input_audio_format": "pcm",
                            "output_audio_format": "pcm",
                            "modalities": ["text", "audio"],
                            "input_audio_transcription": {
                                "model": "qwen3-asr-flash-realtime",
                                "language": "zh",
                            },
                            "translation": {"language": "en"},
                            "enable_voice_clone": True,
                            "voice": "qwen-translate-vc-test",
                            "voice_clone_options": {"frequency": "never"},
                        },
                    }
                )
            )
        elif event["type"] == "session.finish":
            await self.incoming.put(
                json.dumps({"event_id": "finished", "type": "session.finished"})
            )

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


def test_session_follows_official_finish_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_exercise_session(monkeypatch))


async def _exercise_session(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _settings()
    ws = FakeWebSocket(app)

    async def connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        return ws

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    stop = threading.Event()
    frames = DropOldestQueue[AudioFrame](20)
    states: list[str] = []

    def on_status(event):  # type: ignore[no-untyped-def]
        states.append(event.state)
        if event.state == PushState.STREAMING.value:
            frames.put_latest(AudioFrame(1, 1, b"\x00\x00" * 1600))
            threading.Timer(0.05, stop.set).start()

    session = LiveTranslateSession(
        app=app,
        session=app.local_session,
        input_queue=frames,
        audio_buffer=PcmRingBuffer(100_000),
        stop_event=stop,
        on_status=on_status,
        on_subtitle=lambda _: None,
    )
    await asyncio.wait_for(session.run(), timeout=2)
    sent_types = [event["type"] for event in ws.sent]
    assert sent_types[0] == "session.update"
    assert "input_audio_buffer.append" in sent_types
    assert sent_types[-1] == "session.finish"
    assert states[-2:] == [PushState.FINISHING.value, PushState.FINISHED.value]
    assert ws.closed is True
