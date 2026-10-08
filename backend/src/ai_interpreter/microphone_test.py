from __future__ import annotations

import threading
import wave
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio import CaptureWorker, DeviceManager
from .buffers import DropOldestQueue
from .config import ConfigurationError, load_settings
from .models import AudioFrame, PushState, StatusEvent, SubtitleUpdate
from .session import LiveTranslateSession, SingleSessionThread

TestEventCallback = Callable[[dict[str, Any]], None]


class MicrophoneTestError(RuntimeError):
    """麦克风克隆译音测试无法安全启动。"""


class CloneWaveSink:
    """将云端返回的 24 kHz PCM16 克隆译音写入可试听 WAV。"""

    def __init__(self, path: Path, sample_rate_hz: int = 24000) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._wave = wave.open(str(path), "wb")  # noqa: SIM115 - 跨越整个测试会话
        self._wave.setnchannels(1)
        self._wave.setsampwidth(2)
        self._wave.setframerate(sample_rate_hz)
        self.bytes_written = 0
        self.closed = False

    def write(self, pcm: bytes) -> int:
        with self._lock:
            if self.closed:
                return 0
            self._wave.writeframesraw(pcm)
            self.bytes_written += len(pcm)
        # LiveTranslate 协议将返回值解释为“因缓冲区满而丢弃的字节数”。
        return 0

    def clear(self) -> int:
        # 文件不是待回放队列；重连时禁止重发由输入队列负责，已有测试结果予以保留。
        return 0

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self._wave.close()
            self.closed = True


class MicrophoneTestManager:
    def __init__(self, config_path: Path, publish: TestEventCallback) -> None:
        self.config_path = config_path.resolve()
        self.output_directory = (self.config_path.parent / "../runtime/microphone_tests").resolve()
        self.publish = publish
        self._lock = threading.RLock()
        self._running = False
        self._stopping = False
        self._stop_event = threading.Event()
        self._capture: CaptureWorker | None = None
        self._cloud: SingleSessionThread | None = None
        self._sink: CloneWaveSink | None = None
        self._timer: threading.Timer | None = None
        self._latest: dict[str, Any] = {
            "running": False,
            "stopping": False,
            "source": "",
            "translation": "",
            "recording_url": None,
        }
        self._source = ""
        self._translation = ""
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running or self._stopping

    def start(self, duration_seconds: int = 20) -> dict[str, Any]:
        with self._lock:
            if self._running or self._stopping:
                raise MicrophoneTestError("麦克风翻译测试已经在运行")
            try:
                settings = load_settings(self.config_path)
            except ConfigurationError as exc:
                raise MicrophoneTestError(str(exc)) from exc
            if "audio" not in settings.local_session.modalities:
                raise MicrophoneTestError("请将运行模式设为“双路同传”以启用克隆音频模态")
            if not settings.local_session.voice_id:
                raise MicrophoneTestError("请先配置有效的克隆音色 voice_id")

            microphone = DeviceManager().resolve_microphone(settings.audio.microphone)
            seconds = max(5, min(int(duration_seconds), 60))
            self.output_directory.mkdir(parents=True, exist_ok=True)
            path = self.output_directory / (
                f"microphone-test-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}.wav"
            )
            sink = CloneWaveSink(path, settings.audio.output_sample_rate_hz)
            input_queue: DropOldestQueue[AudioFrame] = DropOldestQueue(
                settings.queues.input_chunks
            )
            capture = CaptureWorker(
                channel="local",
                device=microphone,
                settings=settings.audio,
                output=input_queue,
                status=self._on_status,
            )
            self._stop_event.clear()
            session = LiveTranslateSession(
                app=settings,
                session=settings.local_session,
                input_queue=input_queue,
                audio_buffer=sink,  # CloneWaveSink 满足 write/clear 契约
                stop_event=self._stop_event,
                on_status=self._on_status,
                on_subtitle=self._on_subtitle,
            )
            cloud = SingleSessionThread(session)
            self._capture = capture
            self._cloud = cloud
            self._sink = sink
            self._source = ""
            self._translation = ""
            self._last_error = None
            self._running = True
            self._latest = {
                "running": True,
                "stopping": False,
                "duration_seconds": seconds,
                "microphone": microphone.name,
                "source": "",
                "translation": "",
                "recording_name": path.name,
                "recording_url": None,
                "bytes_written": 0,
                "error": None,
            }
            cloud.start()
            timer = threading.Timer(seconds, self.stop)
            timer.name = "microphone-test-timeout"
            timer.daemon = True
            self._timer = timer
            timer.start()
            self._publish("started", message=f"最长 {seconds} 秒", counters={})
            return dict(self._latest)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._stopping:
                return {**self._latest, "running": False, "stopping": True}
            if not self._running:
                return dict(self._latest)
            self._running = False
            self._stopping = True
            timer = self._timer
            self._timer = None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
            capture = self._capture
            cloud = self._cloud
            sink = self._sink
            if capture is not None:
                capture.stop()
            self._stop_event.set()
            self._publish("stopping", message="正在发送 session.finish", counters={})

        if capture is not None and capture.is_alive():
            capture.join(timeout=3)
        if cloud is not None and cloud.is_alive():
            cloud.join(timeout=20)
        if sink is not None:
            sink.close()

        cloud_error = None
        if cloud is not None and cloud.error is not None:
            cloud_error = f"{type(cloud.error).__name__}: {str(cloud.error)[:160]}"
        with self._lock:
            bytes_written = sink.bytes_written if sink is not None else 0
            recording_name = sink.path.name if sink is not None else None
            result = {
                "running": False,
                "stopping": False,
                "source": self._source,
                "translation": self._translation,
                "recording_name": recording_name,
                "recording_url": (
                    f"/api/microphone-test/audio/{recording_name}"
                    if recording_name and bytes_written > 0
                    else None
                ),
                "bytes_written": bytes_written,
                "error": cloud_error or self._last_error,
            }
            self._latest = result
            self._stopping = False
            self._trim_recordings(10)
            self._publish(
                "finished" if bytes_written > 0 else "no_audio",
                message=("克隆音频已生成" if bytes_written > 0 else "未收到克隆音频"),
                counters={"bytes_written": bytes_written},
            )
            return dict(result)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sink_bytes = self._sink.bytes_written if self._sink is not None else 0
            return {
                **self._latest,
                "running": self._running,
                "stopping": self._stopping,
                "source": self._source,
                "translation": self._translation,
                "bytes_written": sink_bytes,
                "error": self._last_error or self._latest.get("error"),
            }

    def resolve_recording(self, name: str) -> Path | None:
        if Path(name).name != name or not name.startswith("microphone-test-"):
            return None
        if not name.lower().endswith(".wav"):
            return None
        path = (self.output_directory / name).resolve()
        if path.parent != self.output_directory or not path.is_file():
            return None
        return path

    def _on_status(self, event: StatusEvent) -> None:
        if event.component == "cloud_push" and event.state == PushState.STREAMING.value:
            with self._lock:
                capture = self._capture
                if capture is not None and capture.ident is None:
                    capture.start()
        if event.state in {"error", "auth_failed"}:
            with self._lock:
                self._last_error = event.message or event.category
        self.publish(
            {
                "type": "microphone_test_status",
                "component": event.component,
                "state": event.state,
                "category": event.category,
                "message": event.message[:180],
                "counters": event.counters,
                "at_ns": event.at_ns,
            }
        )

    def _on_subtitle(self, update: SubtitleUpdate) -> None:
        with self._lock:
            self._source = update.source_confirmed + update.source_stash
            self._translation = update.translation_confirmed + update.translation_stash
            payload = {
                "type": "microphone_test_subtitle",
                "source": self._source,
                "translation": self._translation,
                "source_final": update.source_final,
                "translation_final": update.translation_final,
            }
        self.publish(payload)

    def _publish(self, state: str, *, message: str, counters: dict[str, int]) -> None:
        self.publish(
            {
                "type": "microphone_test_status",
                "component": "microphone_test",
                "state": state,
                "category": "none",
                "message": message,
                "counters": counters,
            }
        )

    def _trim_recordings(self, keep: int) -> None:
        items = sorted(
            self.output_directory.glob("microphone-test-*.wav"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for item in items[keep:]:
            item.unlink(missing_ok=True)
