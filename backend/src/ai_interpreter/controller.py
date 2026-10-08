from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from time import monotonic, monotonic_ns

from .audio import CaptureWorker, DeviceManager, VirtualOutputWorker
from .buffers import DropOldestQueue, PcmRingBuffer
from .config import AppSettings
from .logging_setup import log_status
from .models import AudioFrame, DeviceDescriptor, PushState, StatusEvent, SubtitleUpdate
from .session import DualSessionThread, LiveTranslateSession, SingleSessionThread


class ControllerError(RuntimeError):
    pass


class InterpreterController:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._status_callbacks: list[Callable[[StatusEvent], None]] = []
        self._subtitle_callbacks: list[Callable[[SubtitleUpdate], None]] = []
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._remote_queue: DropOldestQueue[AudioFrame] | None = None
        self._local_queue: DropOldestQueue[AudioFrame] | None = None
        self._audio_buffer: PcmRingBuffer | None = None
        self._remote_capture: CaptureWorker | None = None
        self._local_capture: CaptureWorker | None = None
        self._virtual_output: VirtualOutputWorker | None = None
        self._cloud_thread: threading.Thread | None = None
        self._started = False
        self._logger = logging.getLogger("controller")
        self._ptt_lock = threading.Lock()
        self._ptt_until_ns = 0
        self._ptt_active = False

    def subscribe_status(self, callback: Callable[[StatusEvent], None]) -> None:
        self._status_callbacks.append(callback)

    def subscribe_subtitle(self, callback: Callable[[SubtitleUpdate], None]) -> None:
        self._subtitle_callbacks.append(callback)

    def validate_devices(self) -> dict[str, str]:
        manager = DeviceManager()
        mic = manager.resolve_microphone(self.settings.audio.microphone)
        if self.settings.mode == "local_subtitle":
            return {"microphone": mic.name}
        if self.settings.mode == "microphone_interpretation":
            result = {"microphone": mic.name}
            if not self.settings.audio.virtual_output_enabled:
                result["virtual_output"] = "disabled (clone audio is received then discarded)"
                return result
            result["virtual_output"] = manager.resolve_output(
                self.settings.audio.virtual_output
            ).name
            return result
        remote = manager.resolve_remote_loopback(self.settings.audio.remote_playback)
        result = {
            "microphone": mic.name,
            "remote_loopback": remote.name,
        }
        if not self.settings.audio.virtual_output_enabled:
            result["virtual_output"] = "disabled (clone audio is received then discarded)"
            return result
        virtual = manager.resolve_output(self.settings.audio.virtual_output)
        if self._looks_like_same_virtual_route(remote.name, virtual.name):
            raise ControllerError("远端整机回环不能指向同一个 VB-CABLE 端点")
        result["virtual_output"] = virtual.name
        return result

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._stop_event.clear()
            with self._ptt_lock:
                self._ptt_until_ns = 0
                self._ptt_active = False
            manager = DeviceManager()
            self._emit(StatusEvent(component="controller", state="validating"))
            mic = manager.resolve_microphone(self.settings.audio.microphone)
            if self.settings.mode in {"local_subtitle", "microphone_interpretation"}:
                self._start_microphone_only(mic)
                return
            remote = manager.resolve_remote_loopback(self.settings.audio.remote_playback)
            virtual = None
            if self.settings.audio.virtual_output_enabled:
                virtual = manager.resolve_output(self.settings.audio.virtual_output)
                if self._looks_like_same_virtual_route(remote.name, virtual.name):
                    raise ControllerError("远端整机回环与虚拟输出可能形成反馈，已阻止启动")

            self._remote_queue = DropOldestQueue(self.settings.queues.input_chunks)
            self._local_queue = DropOldestQueue(self.settings.queues.input_chunks)
            capacity = 0
            if virtual is not None:
                capacity = int(
                    self.settings.audio.output_sample_rate_hz
                    * 2
                    * self.settings.queues.output_buffer_ms
                    / 1000
                )
                self._audio_buffer = PcmRingBuffer(capacity)
            self._remote_capture = CaptureWorker(
                channel="remote",
                device=remote,
                settings=self.settings.audio,
                output=self._remote_queue,
                status=self._on_status,
            )
            self._local_capture = CaptureWorker(
                channel="local",
                device=mic,
                settings=self.settings.audio,
                output=self._local_queue,
                status=self._on_status,
                transmit_enabled=self._microphone_transmit_enabled,
            )
            if virtual is not None and self._audio_buffer is not None:
                self._virtual_output = VirtualOutputWorker(
                    device=virtual,
                    settings=self.settings.audio,
                    buffer=self._audio_buffer,
                    status=self._on_status,
                )
            else:
                self._emit(
                    StatusEvent(
                        component="virtual_output",
                        state="disabled",
                        message="已接收克隆译音，但未写入虚拟麦克风",
                        channel="local",
                    )
                )
            remote_session = LiveTranslateSession(
                app=self.settings,
                session=self.settings.remote_session,
                input_queue=self._remote_queue,
                audio_buffer=None,
                stop_event=self._stop_event,
                on_status=self._on_status,
                on_subtitle=self._on_subtitle,
            )
            local_session = LiveTranslateSession(
                app=self.settings,
                session=self.settings.local_session,
                input_queue=self._local_queue,
                audio_buffer=self._audio_buffer,
                stop_event=self._stop_event,
                on_status=self._on_status,
                on_subtitle=self._on_subtitle,
            )
            self._cloud_thread = DualSessionThread(remote_session, local_session)
            self._started = True
            self._cloud_thread.start()
            self._emit(
                StatusEvent(
                    component="controller",
                    state="started",
                    counters={
                        "input_queue_chunks": self.settings.queues.input_chunks,
                        "output_buffer_bytes": capacity,
                    },
                )
            )

    def _start_microphone_only(self, mic: DeviceDescriptor) -> None:
        self._local_queue = DropOldestQueue(self.settings.queues.input_chunks)
        with_audio = "audio" in self.settings.local_session.modalities
        capacity = 0
        if with_audio and self.settings.audio.virtual_output_enabled:
            virtual = DeviceManager().resolve_output(self.settings.audio.virtual_output)
            capacity = int(
                self.settings.audio.output_sample_rate_hz
                * 2
                * self.settings.queues.output_buffer_ms
                / 1000
            )
            self._audio_buffer = PcmRingBuffer(capacity)
            self._virtual_output = VirtualOutputWorker(
                device=virtual,
                settings=self.settings.audio,
                buffer=self._audio_buffer,
                status=self._on_status,
            )
        elif with_audio:
            self._emit(
                StatusEvent(
                    component="virtual_output",
                    state="disabled",
                    message="已接收克隆译音，但未写入虚拟麦克风",
                    channel="local",
                )
            )
        self._local_capture = CaptureWorker(
            channel="local",
            device=mic,
            settings=self.settings.audio,
            output=self._local_queue,
            status=self._on_status,
            transmit_enabled=self._microphone_transmit_enabled,
        )
        local_session = LiveTranslateSession(
            app=self.settings,
            session=self.settings.local_session,
            input_queue=self._local_queue,
            audio_buffer=self._audio_buffer,
            stop_event=self._stop_event,
            on_status=self._on_status,
            on_subtitle=self._on_subtitle,
        )
        self._cloud_thread = SingleSessionThread(local_session)
        self._started = True
        self._cloud_thread.start()
        self._emit(
            StatusEvent(
                component="controller",
                state="started",
                counters={
                    "input_queue_chunks": self.settings.queues.input_chunks,
                    "output_buffer_bytes": capacity,
                },
            )
        )

    def stop(self, timeout: float = 20.0) -> None:
        with self._lock:
            if not self._started:
                return
            self._emit(StatusEvent(component="controller", state="stopping"))
            self._stop_event.set()
            for worker in (self._remote_capture, self._local_capture):
                if worker is not None:
                    worker.stop()
            cloud = self._cloud_thread
        if cloud is not None:
            cloud.join(timeout=timeout)
        if (
            self._virtual_output is not None
            and self._virtual_output.is_alive()
            and self._audio_buffer is not None
            and self._audio_buffer.size > 0
        ):
            self._emit(
                StatusEvent(
                    component="virtual_output",
                    channel="local",
                    state="draining",
                    counters={"buffered_bytes": self._audio_buffer.size},
                )
            )
            drain_timeout = min(5.0, max(0.5, self.settings.queues.output_buffer_ms / 1000))
            deadline = monotonic() + drain_timeout
            while (
                self._audio_buffer.size > 0
                and self._virtual_output.is_alive()
                and monotonic() < deadline
            ):
                threading.Event().wait(0.02)
        if self._virtual_output is not None:
            self._virtual_output.stop()
        for worker in (self._remote_capture, self._local_capture, self._virtual_output):
            if worker is not None and worker.is_alive():
                worker.join(timeout=3)
        with self._lock:
            if self._remote_queue is not None:
                self._remote_queue.clear()
            if self._local_queue is not None:
                self._local_queue.clear()
            if self._audio_buffer is not None:
                self._audio_buffer.clear()
            self._started = False
            self._emit(StatusEvent(component="controller", state="stopped"))

    def _on_status(self, event: StatusEvent) -> None:
        if event.component == "cloud_push" and event.state == PushState.STREAMING.value:
            with self._lock:
                if event.channel == "remote" and self._remote_capture is not None:
                    self._start_worker_once(self._remote_capture, "remote_capture")
                elif event.channel == "local":
                    if self._local_capture is not None:
                        self._start_worker_once(self._local_capture, "local_capture")
                    if (
                        self._virtual_output is not None
                        and "audio" in self.settings.local_session.modalities
                    ):
                        self._start_worker_once(self._virtual_output, "virtual_output")
        self._emit(event)

    def set_microphone_transmit(self, active: bool) -> None:
        if not self.settings.microphone_push_to_talk:
            raise ControllerError("按住说话模式未启用")
        with self._ptt_lock:
            self._ptt_until_ns = monotonic_ns() + 2_000_000_000 if active else 0
            changed = self._ptt_active != active
            self._ptt_active = active
        if changed:
            self._emit(
                StatusEvent(
                    component="microphone_transmit",
                    channel="local",
                    state="held" if active else "released",
                )
            )

    def _microphone_transmit_enabled(self) -> bool:
        if not self.settings.microphone_push_to_talk:
            return True
        expired = False
        with self._ptt_lock:
            enabled = monotonic_ns() < self._ptt_until_ns
            if self._ptt_active and not enabled:
                self._ptt_active = False
                expired = True
        if expired:
            self._emit(
                StatusEvent(
                    component="microphone_transmit",
                    channel="local",
                    state="expired",
                )
            )
        return enabled

    def _start_worker_once(self, worker: threading.Thread, component: str) -> None:
        if worker.ident is None:
            worker.start()
        elif not worker.is_alive():
            self._emit(
                StatusEvent(
                    component=component,
                    state="error",
                    category="worker_terminated",
                    message="worker cannot be restarted; stop and start a new meeting",
                )
            )

    def _emit(self, event: StatusEvent) -> None:
        log_status(self._logger, event)
        for callback in tuple(self._status_callbacks):
            callback(event)

    def _on_subtitle(self, update: SubtitleUpdate) -> None:
        for callback in tuple(self._subtitle_callbacks):
            callback(update)

    @staticmethod
    def _looks_like_same_virtual_route(remote_name: str, output_name: str) -> bool:
        remote = remote_name.casefold().replace("[loopback]", "").replace("(loopback)", "")
        output = output_name.casefold()
        if "cable" in remote and "cable" in output:
            return True
        return remote.strip() == output.strip()
