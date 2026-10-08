from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from time import monotonic_ns
from typing import Any

import yaml

from virtual_mic_test.engine import VirtualMicTestEngine

from .config import ConfigStore
from .runtime import RemoteSubtitleController


class SubtitleTestManager:
    """只运行远端媒体回环与文本翻译，用于验证真实字幕效果。"""

    def __init__(self, store: ConfigStore, publish: Any | None = None) -> None:
        self.store = store
        self.publish = publish
        self._lock = threading.RLock()
        self._controller: RemoteSubtitleController | None = None
        self._timer: threading.Timer | None = None
        self._running = False
        self._started_at_ns: int | None = None
        self._statuses: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._subtitle = {"source": "", "translation": ""}
        self._history: list[dict[str, str]] = []
        self._final_items: set[str] = set()
        self._metrics: dict[str, Any] = {
            "server_vad_to_first_subtitle_ms": None,
            "speech_detected": False,
            "final_segments": 0,
        }

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def validate(self) -> dict[str, Any]:
        settings = self.store.to_app_settings(for_start=True)
        controller = RemoteSubtitleController(settings)
        resolved = controller.validate_devices()
        return {
            "api_key_configured": bool(settings.aliyun.api_key),
            "workspace_id_configured": bool(settings.aliyun.workspace_id),
            "source_language": settings.remote_session.source_language or "auto",
            "target_language": settings.remote_session.target_language,
            **resolved,
        }

    def start(self, duration_seconds: int = 60) -> dict[str, Any]:
        with self._lock:
            if self._running:
                raise RuntimeError("字幕效果测试已经在运行")
            settings = self.store.to_app_settings(for_start=True)
            controller = RemoteSubtitleController(settings)
            controller.subscribe_status(self._on_status)
            controller.subscribe_subtitle(self._on_subtitle)
            self._controller = controller
            self._running = True
            self._started_at_ns = monotonic_ns()
            self._statuses.clear()
            self._events.clear()
            self._subtitle = {"source": "", "translation": ""}
            self._history.clear()
            self._final_items.clear()
            self._metrics = {
                "server_vad_to_first_subtitle_ms": None,
                "speech_detected": False,
                "final_segments": 0,
            }
        try:
            controller.start()
        except BaseException:
            with self._lock:
                self._controller = None
                self._running = False
            raise
        seconds = max(5, min(int(duration_seconds), 300))
        timer = threading.Timer(seconds, self.stop)
        timer.daemon = True
        with self._lock:
            self._timer = timer
        timer.start()
        return {
            "running": True,
            "duration_seconds": seconds,
            "source_language": settings.remote_session.source_language or "auto",
            "target_language": settings.remote_session.target_language,
        }

    def stop(self) -> dict[str, Any]:
        with self._lock:
            controller = self._controller
            timer = self._timer
            self._timer = None
            self._controller = None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
        if controller is not None:
            controller.stop()
        with self._lock:
            self._running = False
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "started_at_ns": self._started_at_ns,
                "statuses": dict(self._statuses),
                "events": list(self._events[-100:]),
                "subtitle": dict(self._subtitle),
                "history": list(self._history[-30:]),
                "metrics": dict(self._metrics),
            }

    def _on_status(self, event: Any) -> None:
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
        with self._lock:
            self._statuses[key] = payload
            self._events.append(payload)
            self._events = self._events[-200:]
            if event.component == "cloud_vad" and event.state == "speech_started":
                self._metrics["speech_detected"] = True
            if event.component == "audio_capture" and event.state == "speech":
                self._metrics["speech_detected"] = True
            if event.component == "latency" and event.state == "subtitle_first":
                value = event.counters.get("server_vad_to_first_ms")
                if value is not None:
                    self._metrics["server_vad_to_first_subtitle_ms"] = value
        if self.publish is not None:
            self.publish({"type": "test_subtitle_status", **payload})

    def _on_subtitle(self, update: Any) -> None:
        source = update.source_confirmed + update.source_stash
        translation = update.translation_confirmed + update.translation_stash
        payload = {
            "type": "subtitle",
            "channel": "remote",
            "source_item_id": update.source_item_id,
            "translation_item_id": update.translation_item_id,
            "source_confirmed": update.source_confirmed,
            "source_stash": update.source_stash,
            "translation_confirmed": update.translation_confirmed,
            "translation_stash": update.translation_stash,
            "source_final": update.source_final,
            "translation_final": update.translation_final,
        }
        with self._lock:
            self._subtitle = {"source": source, "translation": translation}
            if update.translation_final and update.source_item_id not in self._final_items:
                self._final_items.add(update.source_item_id)
                self._history.append(
                    {
                        "source": source,
                        "translation": translation,
                    }
                )
                self._metrics["final_segments"] = len(self._history)
        if self.publish is not None:
            self.publish(payload)


class FormalTestLab:
    def __init__(self, store: ConfigStore, publish: Any | None = None) -> None:
        self.store = store
        self.runtime_dir = Path(__file__).with_name("runtime")
        self.output_dir = self.runtime_dir / "test_audio"
        self.test_config = self.runtime_dir / "test-settings.yaml"
        self._sync_config()
        self.engine = VirtualMicTestEngine(
            self.test_config,
            main_settings_provider=self._test_app_settings,
        )
        self.subtitle = SubtitleTestManager(store, publish)

    def _test_app_settings(self):
        settings = self.store.to_app_settings(for_start=True)
        voice = self.store.raw()["voice"]
        if not str(voice.get("voice_id", "")).strip():
            raise RuntimeError("实时克隆测试前必须在音色管理页选择 voice_id")
        local = replace(
            settings.local_session,
            modalities=("text", "audio"),
            voice_id=str(voice["voice_id"]).strip(),
            enable_voice_clone=True,
            voice_clone_frequency=voice["clone_frequency"],
        )
        return replace(settings, mode="full_duplex", local_session=local)

    def _sync_config(self) -> None:
        raw = self.store.raw()
        audio = raw["audio"]
        test = raw["test"]
        value = {
            "schema_version": 1,
            "interpreter_config": str(self.store.config_path),
            "web": {"host": "127.0.0.1", "port": int(raw["web"]["port"]), "open_browser": False},
            "devices": {
                "microphone": audio["microphone"],
                "virtual_playback": audio["virtual_output"],
                "virtual_recording": audio["virtual_recording"],
            },
            "audio": {
                "signal_threshold_dbfs": test["signal_threshold_dbfs"],
                "route_tone_hz": test["route_tone_hz"],
                "route_tone_seconds": test["route_tone_seconds"],
                "route_record_tail_ms": test["route_record_tail_ms"],
                "cloud_max_duration_seconds": test["cloud_max_duration_seconds"],
                "output_buffer_ms": raw["queues"]["output_buffer_ms"],
            },
            "outputs": {
                "directory": str(self.output_dir),
                "max_recordings": test["max_recordings"],
            },
        }
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        data = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        fd, name = tempfile.mkstemp(
            prefix=".test-settings.", suffix=".tmp", dir=self.runtime_dir, text=True
        )
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
            os.replace(temp, self.test_config)
        finally:
            temp.unlink(missing_ok=True)

    def refresh(self) -> None:
        if self.engine.running or self.subtitle.running:
            raise RuntimeError("测试进行中，不能刷新测试配置")
        self._sync_config()

    def recordings(self) -> list[dict[str, Any]]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for path in sorted(
            self.output_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            items.append(
                {"name": path.name, "bytes": path.stat().st_size, "modified": path.stat().st_mtime}
            )
        return items

    def resolve_recording(self, name: str) -> Path:
        if not name or Path(name).name != name or not name.lower().endswith(".wav"):
            raise ValueError("录音文件名无效")
        path = (self.output_dir / name).resolve()
        if path.parent != self.output_dir.resolve() or not path.is_file():
            raise FileNotFoundError(name)
        return path

    def delete_recording(self, name: str) -> None:
        self.resolve_recording(name).unlink()

    def delete_all(self) -> int:
        paths = [item for item in self.output_dir.glob("*.wav") if item.is_file()]
        for path in paths:
            path.unlink()
        return len(paths)
