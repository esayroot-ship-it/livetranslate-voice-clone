from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ai_interpreter.audio import CaptureWorker, DeviceManager
from ai_interpreter.buffers import DropOldestQueue
from ai_interpreter.config import AppSettings, DeviceSelector
from ai_interpreter.controller import InterpreterController
from ai_interpreter.models import AudioFrame, PushState, StatusEvent, SubtitleUpdate
from ai_interpreter.session import LiveTranslateSession, SingleSessionThread

from .config import ConfigStore


class EventHub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def publish(self, payload: dict[str, Any]) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._publish, payload)

    def _publish(self, payload: dict[str, Any]) -> None:
        with self._lock:
            queues = tuple(self._subscribers)
        for queue in queues:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(payload)


class RemoteSubtitleController:
    """正式版字幕模式：只捕获选定播放端点的 WASAPI 整机回环。"""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._status: list[Callable[[StatusEvent], None]] = []
        self._subtitle: list[Callable[[SubtitleUpdate], None]] = []
        self._stop = threading.Event()
        self._queue: DropOldestQueue[AudioFrame] | None = None
        self._capture: CaptureWorker | None = None
        self._cloud: SingleSessionThread | None = None
        self._running = False

    def subscribe_status(self, callback: Callable[[StatusEvent], None]) -> None:
        self._status.append(callback)

    def subscribe_subtitle(self, callback: Callable[[SubtitleUpdate], None]) -> None:
        self._subtitle.append(callback)

    def validate_devices(self) -> dict[str, str]:
        remote = DeviceManager().resolve_remote_loopback(self.settings.audio.remote_playback)
        return {"remote_loopback": remote.name}

    def start(self) -> None:
        if self._running:
            return
        self._emit(StatusEvent(component="controller", state="validating"))
        remote = DeviceManager().resolve_remote_loopback(self.settings.audio.remote_playback)
        self._queue = DropOldestQueue(self.settings.queues.input_chunks)
        self._stop.clear()
        self._capture = CaptureWorker(
            channel="remote",
            device=remote,
            settings=self.settings.audio,
            output=self._queue,
            status=self._on_status,
        )
        session = LiveTranslateSession(
            app=self.settings,
            session=self.settings.remote_session,
            input_queue=self._queue,
            audio_buffer=None,
            stop_event=self._stop,
            on_status=self._on_status,
            on_subtitle=self._on_subtitle,
        )
        self._cloud = SingleSessionThread(session)
        self._running = True
        self._cloud.start()
        self._emit(
            StatusEvent(
                component="controller",
                state="started",
                counters={"input_queue_chunks": self.settings.queues.input_chunks},
            )
        )

    def stop(self, timeout: float = 20) -> None:
        if not self._running:
            return
        self._emit(StatusEvent(component="controller", state="stopping"))
        self._stop.set()
        if self._capture:
            self._capture.stop()
        if self._cloud:
            self._cloud.join(timeout)
        if self._capture and self._capture.is_alive():
            self._capture.join(3)
        if self._queue:
            self._queue.clear()
        self._running = False
        self._emit(StatusEvent(component="controller", state="stopped"))

    def _on_status(self, event: StatusEvent) -> None:
        if (
            event.component == "cloud_push"
            and event.channel == "remote"
            and event.state == PushState.STREAMING.value
            and self._capture
            and self._capture.ident is None
        ):
            self._capture.start()
        self._emit(event)

    def _emit(self, event: StatusEvent) -> None:
        for callback in tuple(self._status):
            callback(event)

    def _on_subtitle(self, update: SubtitleUpdate) -> None:
        for callback in tuple(self._subtitle):
            callback(update)


class FormalRuntime:
    _EMPTY_LATENCIES: dict[str, float | None] = {
        "remote_capture_to_vad_ms": None,
        "remote_vad_to_subtitle_ms": None,
        "local_capture_to_vad_ms": None,
        "local_vad_to_translation_ms": None,
        "local_vad_to_audio_ms": None,
        "cloud_audio_to_cable_ms": None,
        "local_vad_to_cable_ms": None,
    }

    def __init__(self, store: ConfigStore, hub: EventHub) -> None:
        self.store = store
        self.hub = hub
        self._lock = asyncio.Lock()
        self._event_lock = threading.RLock()
        self.controller: InterpreterController | RemoteSubtitleController | None = None
        self.latest_status: dict[str, dict[str, Any]] = {}
        self.latest_subtitles: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.latencies = dict(self._EMPTY_LATENCIES)
        self._latency_marks: dict[str, int | None] = {
            "remote_capture_ns": None,
            "remote_vad_ns": None,
            "local_capture_ns": None,
            "local_vad_ns": None,
            "local_audio_first_ns": None,
        }
        self.overlay_process: subprocess.Popen[bytes] | None = None

    @property
    def running(self) -> bool:
        return self.controller is not None

    def snapshot(self) -> dict[str, Any]:
        with self._event_lock:
            return {
                "type": "snapshot",
                "running": self.running,
                "mode": self.store.raw()["app"]["mode"],
                "statuses": dict(self.latest_status),
                "subtitles": dict(self.latest_subtitles),
                "latencies": dict(self.latencies),
                "recent_events": self.history[-50:],
                "overlay_running": self.overlay_process is not None
                and self.overlay_process.poll() is None,
                "push_to_talk_enabled": bool(
                    self.store.raw()["microphone"]["push_to_talk_enabled"]
                ),
            }

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self.controller is not None:
                raise RuntimeError("程序已经在运行")
            settings = self.store.to_app_settings(for_start=True)
            mode = self.store.raw()["app"]["mode"]
            controller: InterpreterController | RemoteSubtitleController
            controller = (
                RemoteSubtitleController(settings)
                if mode == "subtitle_only"
                else InterpreterController(settings)
            )
            controller.subscribe_status(self._on_status)
            controller.subscribe_subtitle(self._on_subtitle)
            self._reset_latencies()
            with self._event_lock:
                self.latest_subtitles.clear()
            try:
                devices = await asyncio.to_thread(controller.validate_devices)
                await asyncio.to_thread(controller.start)
            except BaseException:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(controller.stop)
                raise
            self.controller = controller
            if self.store.raw()["app"].get("auto_open_overlay", True):
                self.open_overlay()
            return {"ok": True, "mode": mode, "devices": devices}

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            controller = self.controller
            self.controller = None
            if controller is not None:
                await asyncio.to_thread(controller.stop)
            return {"ok": True, "message": "翻译程序已停止"}

    def _on_status(self, event: StatusEvent) -> None:
        payload = {"type": "status", **asdict(event)}
        suffix = f":{event.state}" if event.component == "latency" else ""
        key = f"{event.channel or 'global'}:{event.component}{suffix}"
        with self._event_lock:
            self.latest_status[key] = payload
            self.history.append(payload)
            if len(self.history) > 300:
                del self.history[:-200]
            latency_changed = self._update_latencies(event)
            latency_payload = {
                "type": "latency_snapshot",
                "latencies": dict(self.latencies),
            }
        self.hub.publish(payload)
        if latency_changed:
            self.hub.publish(latency_payload)

    def _reset_latencies(self) -> None:
        with self._event_lock:
            self.latencies = dict(self._EMPTY_LATENCIES)
            for key in self._latency_marks:
                self._latency_marks[key] = None

    def _update_latencies(self, event: StatusEvent) -> bool:
        """聚合最近一次真实事件时钟；未观测到的链路保持为空。"""
        channel = event.channel
        if event.component == "audio_capture" and event.state == "speech" and channel:
            self._latency_marks[f"{channel}_capture_ns"] = event.at_ns
            return False

        if event.component == "cloud_vad" and event.state == "speech_started" and channel:
            capture_key = f"{channel}_capture_ns"
            capture_ns = self._latency_marks.get(capture_key)
            self._latency_marks[f"{channel}_vad_ns"] = event.at_ns
            if capture_ns is None:
                return False
            metric = f"{channel}_capture_to_vad_ms"
            self.latencies[metric] = self._elapsed_ms(capture_ns, event.at_ns)
            return True

        if event.component == "latency":
            value = event.counters.get("server_vad_to_first_ms")
            if not isinstance(value, (int, float)):
                return False
            if channel == "remote" and event.state == "subtitle_first":
                self.latencies["remote_vad_to_subtitle_ms"] = float(value)
                return True
            if channel == "local" and event.state == "subtitle_first":
                self.latencies["local_vad_to_translation_ms"] = float(value)
                return True
            if channel == "local" and event.state == "audio_first":
                self.latencies["local_vad_to_audio_ms"] = float(value)
                self._latency_marks["local_audio_first_ns"] = event.at_ns
                return True

        if event.component == "virtual_output" and event.state == "playing":
            vad_ns = self._latency_marks["local_vad_ns"]
            audio_ns = self._latency_marks["local_audio_first_ns"]
            changed = False
            if vad_ns is not None:
                self.latencies["local_vad_to_cable_ms"] = self._elapsed_ms(
                    vad_ns, event.at_ns
                )
                changed = True
            if audio_ns is not None:
                self.latencies["cloud_audio_to_cable_ms"] = self._elapsed_ms(
                    audio_ns, event.at_ns
                )
                changed = True
            if changed:
                self._latency_marks["local_vad_ns"] = None
                self._latency_marks["local_audio_first_ns"] = None
            return changed
        return False

    @staticmethod
    def _elapsed_ms(start_ns: int, end_ns: int) -> float:
        return round(max(0, end_ns - start_ns) / 1_000_000, 1)

    def _on_subtitle(self, update: SubtitleUpdate) -> None:
        payload = {"type": "subtitle", **asdict(update)}
        with self._event_lock:
            self.latest_subtitles[update.channel] = payload
        self.hub.publish(payload)

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

    def validate_devices(self) -> dict[str, str]:
        settings = self.store.to_app_settings(for_start=False)
        mode = self.store.raw()["app"]["mode"]
        controller = (
            RemoteSubtitleController(settings)
            if mode == "subtitle_only"
            else InterpreterController(settings)
        )
        return controller.validate_devices()

    def set_microphone_transmit(self, active: bool) -> dict[str, Any]:
        controller = self.controller
        if not isinstance(controller, InterpreterController):
            raise RuntimeError("当前运行模式没有启用本地麦克风")
        controller.set_microphone_transmit(active)
        return {"ok": True, "active": active}

    def probe_loopback(self, name: str, use_default: bool, duration_ms: int) -> dict[str, Any]:
        selector = DeviceSelector("WASAPI", name.strip(), use_default)
        threshold = float(self.store.raw()["audio"]["silence_dbfs"])
        return DeviceManager().probe_remote_loopback(
            selector, duration_seconds=duration_ms / 1000, detection_dbfs=threshold
        )

    def probe_microphone(self, name: str, use_default: bool, duration_ms: int) -> dict[str, Any]:
        selector = DeviceSelector("WASAPI", name.strip(), use_default)
        threshold = float(self.store.raw()["audio"]["silence_dbfs"])
        return DeviceManager().probe_microphone(
            selector,
            duration_seconds=duration_ms / 1000,
            quiet_seconds=min(1.0, duration_ms / 3000),
            detection_dbfs=threshold,
        )

    def open_overlay(self) -> dict[str, Any]:
        if self.overlay_process is not None and self.overlay_process.poll() is None:
            return {"ok": True, "already_running": True}
        raw = self.store.raw()
        web = raw["web"]
        subtitle = raw["subtitle"]
        microphone = raw["microphone"]
        size = int(subtitle["font_size_px"])
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(root / "src"), str(root), env.get("PYTHONPATH", "")]
        )
        code = (
            "from ai_interpreter.overlay import run_overlay; "
            f"raise SystemExit(run_overlay({web['host']!r},{web['port']},{size},"
            f"source_mode={subtitle['source_mode']!r},"
            f"show_source={bool(subtitle['show_source'])!r},"
            f"show_translation={bool(subtitle['show_translation'])!r},"
            f"show_channel_labels={bool(subtitle['show_channel_labels'])!r},"
            f"local_translation_enabled={bool(microphone['translate_enabled'])!r},"
            f"compact_background={bool(subtitle['compact_background'])!r},"
            f"window_width_percent={int(subtitle['window_width_percent'])!r},"
            f"horizontal_padding_px={int(subtitle['horizontal_padding_px'])!r},"
            f"vertical_padding_px={int(subtitle['vertical_padding_px'])!r},"
            f"source_font_size_px={int(subtitle['source_font_size_px'])!r},"
            f"source_font_color={subtitle['source_font_color']!r},"
            f"channel_label_color={subtitle['channel_label_color']!r},"
            f"font_color={subtitle['font_color']!r},"
            f"background_color={subtitle['background_color']!r},"
            f"opacity={float(subtitle['background_opacity'])!r},"
            f"position={subtitle['position']!r}))"
        )
        self.overlay_process = subprocess.Popen(
            [sys.executable, "-B", "-c", code],
            env=env,
            cwd=str(root),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"ok": True, "pid": self.overlay_process.pid}

    def close_overlay(self) -> dict[str, Any]:
        self.hub.publish({"type": "overlay_command", "command": "close"})
        process = self.overlay_process
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
            self.overlay_process = None
        return {"ok": True}

    async def shutdown(self) -> None:
        await self.stop()
        self.close_overlay()
