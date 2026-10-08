from __future__ import annotations

import math
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from time import monotonic_ns
from typing import Any

import numpy as np

from ai_interpreter.audio import (
    _PORTAUDIO_LIFECYCLE_LOCK,
    DeviceManager,
    _import_pyaudio,
    _refresh_input_device,
    _refresh_output_device,
)
from ai_interpreter.config import DeviceSelector
from ai_interpreter.models import DeviceDescriptor

EventCallback = Callable[[dict[str, Any]], None]


def _dbfs(pcm: bytes) -> float:
    values = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if not len(values):
        return -120.0
    rms = float(np.sqrt(np.mean(values * values)))
    return 20.0 * math.log10(max(rms, 1.0) / 32768.0)


def resolve_input(selector: DeviceSelector) -> DeviceDescriptor:
    return DeviceManager().resolve_microphone(selector)


def resolve_output(selector: DeviceSelector) -> DeviceDescriptor:
    return DeviceManager().resolve_output(selector)


class WaveTeeSink:
    """把云端 24 kHz PCM 同时写入 RingBuffer 和对照 WAV。"""

    def __init__(
        self,
        buffer: Any,
        path: Path,
        *,
        threshold_dbfs: float,
        on_event: EventCallback,
    ) -> None:
        self.buffer = buffer
        self.path = path
        self.threshold_dbfs = threshold_dbfs
        self.on_event = on_event
        self._lock = threading.Lock()
        self._wave = wave.open(str(path), "wb")  # noqa: SIM115 - 会话结束时由 close 关闭
        self._wave.setnchannels(1)
        self._wave.setsampwidth(2)
        self._wave.setframerate(24000)
        self.bytes_written = 0
        self.first_signal_ns: int | None = None
        self.peak_dbfs = -120.0
        self.closed = False

    def write(self, data: bytes) -> int:
        level = _dbfs(data)
        now = monotonic_ns()
        with self._lock:
            if self.closed:
                return len(data)
            self._wave.writeframesraw(data)
            self.bytes_written += len(data)
            self.peak_dbfs = max(self.peak_dbfs, level)
            if self.first_signal_ns is None and level >= self.threshold_dbfs:
                self.first_signal_ns = now
                self.on_event(
                    {
                        "component": "cloud_clone",
                        "state": "first_audio",
                        "counters": {"dbfs": round(level, 2)},
                    }
                )
        return self.buffer.write(data)

    def clear(self) -> int:
        return self.buffer.clear()

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self._wave.close()
            self.closed = True


class CableRecorder(threading.Thread):
    def __init__(
        self,
        *,
        device: DeviceDescriptor,
        path: Path,
        threshold_dbfs: float,
        on_event: EventCallback,
    ) -> None:
        super().__init__(name="virtual-mic-recorder", daemon=False)
        self.device = device
        self.path = path
        self.threshold_dbfs = threshold_dbfs
        self.on_event = on_event
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self._arm_lock = threading.Lock()
        self._armed = False
        self.first_signal_ns: int | None = None
        self.bytes_written = 0
        self.peak_dbfs = -120.0
        self.error: str | None = None
        self.native_rate = device.default_sample_rate
        self.channels = max(1, device.max_input_channels)

    def arm_detection(self) -> None:
        with self._arm_lock:
            self.first_signal_ns = None
            self._armed = True

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        pyaudio = _import_pyaudio()
        p: Any | None = None
        stream: Any | None = None
        wav: wave.Wave_write | None = None
        self.on_event({"component": "virtual_recording", "state": "opening"})
        try:
            with _PORTAUDIO_LIFECYCLE_LOCK:
                p = pyaudio.PyAudio()
                active = _refresh_input_device(p, self.device)
                self.native_rate = active.default_sample_rate
                self.channels = max(1, active.max_input_channels)
                frames = max(1, int(self.native_rate * 0.05))
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=self.channels,
                    rate=self.native_rate,
                    input=True,
                    input_device_index=active.index,
                    frames_per_buffer=frames,
                )
            wav = wave.open(str(self.path), "wb")  # noqa: SIM115 - 跨越整个录音线程
            wav.setnchannels(self.channels)
            wav.setsampwidth(2)
            wav.setframerate(self.native_rate)
            self.ready_event.set()
            self.on_event(
                {
                    "component": "virtual_recording",
                    "state": "recording",
                    "message": active.name,
                }
            )
            while not self.stop_event.is_set():
                if stream.get_read_available() < frames:
                    self.stop_event.wait(0.01)
                    continue
                data = stream.read(frames, exception_on_overflow=False)
                wav.writeframesraw(data)
                self.bytes_written += len(data)
                level = _dbfs(data)
                self.peak_dbfs = max(self.peak_dbfs, level)
                with self._arm_lock:
                    if (
                        self._armed
                        and self.first_signal_ns is None
                        and level >= self.threshold_dbfs
                    ):
                        self.first_signal_ns = monotonic_ns()
                        self.on_event(
                            {
                                "component": "virtual_recording",
                                "state": "first_signal",
                                "counters": {"dbfs": round(level, 2)},
                            }
                        )
        except BaseException as exc:
            self.error = f"{type(exc).__name__}: {str(exc)[:160]}"
            self.on_event(
                {
                    "component": "virtual_recording",
                    "state": "error",
                    "message": self.error,
                }
            )
        finally:
            self.ready_event.set()
            if wav is not None:
                wav.close()
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except BaseException:
                    pass
            if p is not None:
                with _PORTAUDIO_LIFECYCLE_LOCK:
                    p.terminate()
            self.on_event({"component": "virtual_recording", "state": "stopped"})


def run_route_test(
    *,
    playback: DeviceDescriptor,
    recording: DeviceDescriptor,
    output_path: Path,
    tone_hz: int,
    tone_seconds: float,
    tail_ms: int,
    threshold_dbfs: float,
    on_event: EventCallback,
) -> dict[str, Any]:
    recorder = CableRecorder(
        device=recording,
        path=output_path,
        threshold_dbfs=threshold_dbfs,
        on_event=on_event,
    )
    recorder.start()
    if not recorder.ready_event.wait(5) or recorder.error:
        recorder.stop()
        recorder.join(2)
        raise RuntimeError(recorder.error or "虚拟录音设备未就绪")

    pyaudio = _import_pyaudio()
    p: Any | None = None
    stream: Any | None = None
    tone_started_ns: int | None = None
    try:
        with _PORTAUDIO_LIFECYCLE_LOCK:
            p = pyaudio.PyAudio()
            active_output = _refresh_output_device(p, playback)
            rate = active_output.default_sample_rate
            channels = min(2, max(1, active_output.max_output_channels))
            frames_per_chunk = max(1, int(rate * 0.05))
            stream = p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                output=True,
                output_device_index=active_output.index,
                frames_per_buffer=frames_per_chunk,
            )
        sample_count = int(rate * tone_seconds)
        timeline = np.arange(sample_count, dtype=np.float64) / rate
        mono = np.rint(np.sin(2 * np.pi * tone_hz * timeline) * 0.18 * 32767).astype("<i2")
        samples = np.repeat(mono[:, None], channels, axis=1).reshape(-1).tobytes()
        recorder.arm_detection()
        tone_started_ns = monotonic_ns()
        on_event({"component": "route_test", "state": "tone_writing"})
        bytes_per_frame = channels * 2
        chunk_bytes = frames_per_chunk * bytes_per_frame
        for offset in range(0, len(samples), chunk_bytes):
            stream.write(samples[offset : offset + chunk_bytes])
        threading.Event().wait(tail_ms / 1000)
    finally:
        recorder.stop()
        recorder.join(3)
        if stream is not None:
            stream.stop_stream()
            stream.close()
        if p is not None:
            with _PORTAUDIO_LIFECYCLE_LOCK:
                p.terminate()

    latency_ms = None
    if tone_started_ns is not None and recorder.first_signal_ns is not None:
        latency_ms = round((recorder.first_signal_ns - tone_started_ns) / 1_000_000, 1)
    detected = recorder.first_signal_ns is not None and recorder.error is None
    return {
        "ok": detected,
        "playback_device": playback.name,
        "recording_device": recording.name,
        "recording_file": output_path.name,
        "route_latency_ms": latency_ms,
        "peak_dbfs": round(recorder.peak_dbfs, 2),
        "captured_bytes": recorder.bytes_written,
        "error": recorder.error,
    }
