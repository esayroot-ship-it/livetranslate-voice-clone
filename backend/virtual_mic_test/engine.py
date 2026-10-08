from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from time import monotonic, monotonic_ns
from typing import Any

from ai_interpreter.audio import CaptureWorker, DeviceManager, VirtualOutputWorker
from ai_interpreter.buffers import DropOldestQueue, PcmRingBuffer
from ai_interpreter.config import AppSettings, ConfigurationError, load_settings
from ai_interpreter.models import AudioFrame, PushState, StatusEvent, SubtitleUpdate
from ai_interpreter.session import LiveTranslateSession, SingleSessionThread

from .audio_io import CableRecorder, WaveTeeSink, resolve_input, resolve_output, run_route_test
from .config import TestSettings, load_test_settings


class TestEngineError(RuntimeError):
    pass


class VirtualMicTestEngine:
    def __init__(
        self,
        config_path: str | Path,
        main_settings_provider: Callable[[], AppSettings] | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self._main_settings_provider = main_settings_provider
        self._lock = threading.RLock()
        self._running = False
        self._stop_event = threading.Event()
        self._mic_worker: CaptureWorker | None = None
        self._output_worker: VirtualOutputWorker | None = None
        self._recorder: CableRecorder | None = None
        self._cloud_thread: SingleSessionThread | None = None
        self._sink: WaveTeeSink | None = None
        self._buffer: PcmRingBuffer | None = None
        self._timer: threading.Timer | None = None
        self._started_at_ns: int | None = None
        self._mic_speech_ns: int | None = None
        self._output_error: str | None = None
        self._latest_result: dict[str, Any] | None = None
        self._events: list[dict[str, Any]] = []
        self._statuses: dict[str, dict[str, Any]] = {}
        self._subtitle: dict[str, str] = {"source": "", "translation": ""}

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def settings(self) -> TestSettings:
        return load_test_settings(self.config_path)

    def _main_settings(self, settings: TestSettings) -> AppSettings:
        if self._main_settings_provider is not None:
            return self._main_settings_provider()
        return load_settings(settings.interpreter_config_path)

    def list_devices(self) -> list[dict[str, Any]]:
        return [
            {
                "index": item.index,
                "name": item.name,
                "host_api": item.host_api,
                "inputs": item.max_input_channels,
                "outputs": item.max_output_channels,
                "sample_rate": item.default_sample_rate,
                "loopback": item.is_loopback,
            }
            for item in DeviceManager().list_devices()
        ]

    def validate(self) -> dict[str, Any]:
        settings = self.settings()
        try:
            main = self._main_settings(settings)
        except ConfigurationError as exc:
            raise TestEngineError(str(exc)) from exc
        if main.mode != "full_duplex" or "audio" not in main.local_session.modalities:
            raise TestEngineError("主程序必须使用 full_duplex 且本地会话包含 audio 模态")
        microphone = resolve_input(settings.audio.microphone)
        playback = resolve_output(settings.audio.virtual_playback)
        recording = resolve_input(settings.audio.virtual_recording)
        if playback.name.casefold() == recording.name.casefold():
            raise TestEngineError("虚拟播放与虚拟录音不应是同一个设备名称")
        return {
            "config": "ok",
            "api_key_configured": bool(main.aliyun.api_key),
            "workspace_id_configured": bool(main.aliyun.workspace_id),
            "voice_id": main.voice.voice_id,
            "microphone": microphone.name,
            "virtual_playback": playback.name,
            "virtual_recording": recording.name,
        }

    def run_route_test(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                raise TestEngineError("云端麦克风测试正在运行")
        settings = self.settings()
        playback = resolve_output(settings.audio.virtual_playback)
        recording = resolve_input(settings.audio.virtual_recording)
        path = self._new_output_path(settings, "route_test")
        self._append_event({"component": "route_test", "state": "starting"})
        result = run_route_test(
            playback=playback,
            recording=recording,
            output_path=path,
            tone_hz=settings.audio.route_tone_hz,
            tone_seconds=settings.audio.route_tone_seconds,
            tail_ms=settings.audio.route_record_tail_ms,
            threshold_dbfs=settings.audio.signal_threshold_dbfs,
            on_event=self._append_event,
        )
        with self._lock:
            self._latest_result = {"type": "route", **result}
        self._trim_recordings(settings)
        return result

    def start_cloud_test(self, duration_seconds: int | None = None) -> dict[str, Any]:
        with self._lock:
            if self._running:
                raise TestEngineError("云端麦克风测试已在运行")
            settings = self.settings()
            main = self._main_settings(settings)
            if main.mode != "full_duplex" or "audio" not in main.local_session.modalities:
                raise TestEngineError("主程序不是可输出克隆译音的 full_duplex 配置")
            microphone = resolve_input(settings.audio.microphone)
            playback = resolve_output(settings.audio.virtual_playback)
            recording = resolve_input(settings.audio.virtual_recording)
            seconds = duration_seconds or settings.audio.cloud_max_duration_seconds
            seconds = max(5, min(seconds, settings.audio.cloud_max_duration_seconds))

            settings.output_directory.mkdir(parents=True, exist_ok=True)
            clone_path = self._new_output_path(settings, "cloud_clone")
            virtual_path = self._new_output_path(settings, "virtual_mic")
            audio_settings = replace(
                main.audio,
                microphone=settings.audio.microphone,
                virtual_output=settings.audio.virtual_playback,
                virtual_output_enabled=True,
            )
            queue_settings = replace(
                main.queues,
                output_buffer_ms=settings.audio.output_buffer_ms,
            )
            app_settings = replace(main, audio=audio_settings, queues=queue_settings)

            input_queue: DropOldestQueue[AudioFrame] = DropOldestQueue(
                app_settings.queues.input_chunks
            )
            capacity = int(
                app_settings.audio.output_sample_rate_hz
                * 2
                * app_settings.queues.output_buffer_ms
                / 1000
            )
            self._buffer = PcmRingBuffer(capacity)
            self._sink = WaveTeeSink(
                self._buffer,
                clone_path,
                threshold_dbfs=settings.audio.signal_threshold_dbfs,
                on_event=self._append_event,
            )
            self._mic_worker = CaptureWorker(
                channel="local",
                device=microphone,
                settings=app_settings.audio,
                output=input_queue,
                status=self._on_status,
            )
            self._output_worker = VirtualOutputWorker(
                device=playback,
                settings=app_settings.audio,
                buffer=self._buffer,
                status=self._on_status,
                signal_threshold_dbfs=settings.audio.signal_threshold_dbfs,
            )
            self._recorder = CableRecorder(
                device=recording,
                path=virtual_path,
                threshold_dbfs=settings.audio.signal_threshold_dbfs,
                on_event=self._append_event,
            )
            self._stop_event.clear()
            cloud = LiveTranslateSession(
                app=app_settings,
                session=app_settings.local_session,
                input_queue=input_queue,
                audio_buffer=self._sink,  # type: ignore[arg-type]
                stop_event=self._stop_event,
                on_status=self._on_status,
                on_subtitle=self._on_subtitle,
            )
            self._cloud_thread = SingleSessionThread(cloud)
            self._running = True
            self._started_at_ns = monotonic_ns()
            self._mic_speech_ns = None
            self._output_error = None
            self._latest_result = None
            self._statuses.clear()
            self._subtitle = {"source": "", "translation": ""}
            self._cloud_thread.start()
            self._timer = threading.Timer(seconds, self.stop_cloud_test)
            self._timer.daemon = True
            self._timer.start()
            self._append_event(
                {
                    "component": "test",
                    "state": "started",
                    "message": f"最长 {seconds} 秒",
                }
            )
            return {
                "running": True,
                "duration_seconds": seconds,
                "cloud_recording": clone_path.name,
                "virtual_recording": virtual_path.name,
            }

    def stop_cloud_test(self) -> dict[str, Any]:
        with self._lock:
            if not self._running:
                return self._latest_result or {"running": False}
            timer = self._timer
            self._timer = None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
            mic = self._mic_worker
            cloud = self._cloud_thread
            output = self._output_worker
            recorder = self._recorder
            sink = self._sink
            buffer = self._buffer
            if mic is not None:
                mic.stop()
            self._stop_event.set()

        if mic is not None:
            mic.join(3)
        if cloud is not None:
            cloud.join(20)
        deadline = monotonic() + 2
        while buffer is not None and buffer.size > 0 and monotonic() < deadline:
            threading.Event().wait(0.05)
        if output is not None:
            output.stop()
        if recorder is not None:
            recorder.stop()
        for worker in (output, recorder):
            if worker is not None:
                worker.join(3)
        if sink is not None:
            sink.close()

        result = self._build_cloud_result()
        settings = self.settings()
        self._trim_recordings(settings)
        with self._lock:
            self._running = False
            self._latest_result = result
            self._append_event({"component": "test", "state": "stopped"})
        return result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = self._build_cloud_result() if self._running else self._latest_result
            return {
                "running": self._running,
                "started_at_ns": self._started_at_ns,
                "statuses": dict(self._statuses),
                "events": list(self._events[-100:]),
                "subtitle": dict(self._subtitle),
                "metrics": result,
            }

    def recordings(self) -> list[dict[str, Any]]:
        settings = self.settings()
        settings.output_directory.mkdir(parents=True, exist_ok=True)
        items = sorted(
            settings.output_directory.glob("*.wav"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        return [
            {
                "name": item.name,
                "size_bytes": item.stat().st_size,
                "modified_at": datetime.fromtimestamp(item.stat().st_mtime).isoformat(
                    timespec="seconds"
                ),
            }
            for item in items[: settings.max_recordings]
        ]

    def _on_status(self, event: StatusEvent) -> None:
        payload = {
            "component": event.component,
            "channel": event.channel,
            "state": event.state,
            "category": event.category,
            "message": event.message,
            "counters": event.counters,
            "at_ns": event.at_ns,
        }
        key = f"{event.channel}:{event.component}" if event.channel else event.component
        start_audio_workers = (
            event.component == "cloud_push" and event.state == PushState.STREAMING.value
        )
        with self._lock:
            self._statuses[key] = payload
            self._events.append(payload)
            self._events = self._events[-200:]
            if event.component == "audio_capture" and event.state == "speech":
                self._mic_speech_ns = self._mic_speech_ns or event.at_ns
            if event.component == "virtual_output" and event.state == "error":
                detail = event.message or event.category
                self._output_error = detail or "虚拟输出设备打开或写入失败"
        if start_audio_workers:
            self._start_audio_workers()

    def _start_audio_workers(self) -> None:
        recorder = self._recorder
        output = self._output_worker
        mic = self._mic_worker
        if recorder is not None and recorder.ident is None:
            recorder.start()
            recorder.ready_event.wait(5)
            recorder.arm_detection()
        if output is not None and output.ident is None:
            output.start()
        if mic is not None and mic.ident is None:
            mic.start()

    def _on_subtitle(self, update: SubtitleUpdate) -> None:
        with self._lock:
            self._subtitle = {
                "source": update.source_confirmed + update.source_stash,
                "translation": update.translation_confirmed + update.translation_stash,
            }

    def _append_event(self, event: dict[str, Any]) -> None:
        payload = {
            "component": str(event.get("component", "test")),
            "state": str(event.get("state", "unknown")),
            "message": str(event.get("message", ""))[:180],
            "counters": event.get("counters", {}),
            "at_ns": monotonic_ns(),
        }
        with self._lock:
            self._events.append(payload)
            self._events = self._events[-200:]

    def _build_cloud_result(self) -> dict[str, Any]:
        sink = self._sink
        output = self._output_worker
        recorder = self._recorder
        cloud_first_ms = None
        output_after_cloud_ms = None
        mic_to_output_ms = None
        cable_after_output_ms = None
        mic_to_cable_ms = None
        if (
            self._mic_speech_ns is not None
            and sink is not None
            and sink.first_signal_ns is not None
        ):
            cloud_first_ms = round((sink.first_signal_ns - self._mic_speech_ns) / 1_000_000, 1)
        if (
            sink is not None
            and sink.first_signal_ns is not None
            and output is not None
            and output.first_signal_write_ns is not None
        ):
            output_after_cloud_ms = round(
                (output.first_signal_write_ns - sink.first_signal_ns) / 1_000_000,
                1,
            )
        if (
            self._mic_speech_ns is not None
            and output is not None
            and output.first_signal_write_ns is not None
        ):
            mic_to_output_ms = round(
                (output.first_signal_write_ns - self._mic_speech_ns) / 1_000_000,
                1,
            )
        if (
            output is not None
            and output.first_signal_write_ns is not None
            and recorder is not None
            and recorder.first_signal_ns is not None
        ):
            cable_after_output_ms = round(
                (recorder.first_signal_ns - output.first_signal_write_ns) / 1_000_000,
                1,
            )
        if (
            self._mic_speech_ns is not None
            and recorder is not None
            and recorder.first_signal_ns is not None
        ):
            mic_to_cable_ms = round(
                (recorder.first_signal_ns - self._mic_speech_ns) / 1_000_000,
                1,
            )
        return {
            "type": "cloud",
            "running": self._running,
            "mic_to_cloud_audio_ms": cloud_first_ms,
            "cloud_to_virtual_output_ms": output_after_cloud_ms,
            "mic_to_virtual_output_ms": mic_to_output_ms,
            "virtual_output_to_cable_recording_ms": cable_after_output_ms,
            "mic_to_virtual_mic_ms": mic_to_cable_ms,
            "cloud_peak_dbfs": round(sink.peak_dbfs, 2) if sink is not None else None,
            "virtual_output_peak_dbfs": (
                round(output.peak_dbfs, 2) if output is not None else None
            ),
            "virtual_peak_dbfs": round(recorder.peak_dbfs, 2) if recorder is not None else None,
            "cloud_bytes_written": sink.bytes_written if sink is not None else 0,
            "virtual_output_started": bool(
                output is not None and output.first_signal_write_ns is not None
            ),
            "virtual_output_error": self._output_error,
            "cable_return_detected": bool(
                recorder is not None and recorder.first_signal_ns is not None
            ),
            "cloud_recording": sink.path.name if sink is not None else None,
            "virtual_recording": recorder.path.name if recorder is not None else None,
            "virtual_recording_error": recorder.error if recorder is not None else None,
        }

    @staticmethod
    def _new_output_path(settings: TestSettings, prefix: str) -> Path:
        settings.output_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        return settings.output_directory / f"{prefix}-{stamp}.wav"

    @staticmethod
    def _trim_recordings(settings: TestSettings) -> None:
        items = sorted(
            settings.output_directory.glob("*.wav"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for item in items[settings.max_recordings :]:
            item.unlink(missing_ok=True)
