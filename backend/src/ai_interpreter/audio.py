from __future__ import annotations

import math
import threading
from collections.abc import Callable
from time import monotonic, monotonic_ns
from typing import Any, Literal

import numpy as np

from .buffers import DropOldestQueue, PcmRingBuffer
from .classification import classify_capture_exception
from .config import AudioSettings, DeviceSelector
from .models import (
    AudioFormat,
    AudioFrame,
    CaptureIssue,
    CaptureState,
    Channel,
    DeviceDescriptor,
    OutputState,
    StatusEvent,
)

StatusCallback = Callable[[StatusEvent], None]
_PORTAUDIO_LIFECYCLE_LOCK = threading.Lock()


def _pcm_dbfs(pcm: bytes) -> float:
    values = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean(values * values))) if len(values) else 0.0
    return 20.0 * math.log10(max(rms, 1.0) / 32768.0)


class MicrophoneProcessor:
    """本地麦克风数字增益与轻量噪声门；不替代真正的语音降噪。"""

    def __init__(
        self,
        *,
        gain_db: float,
        gate_enabled: bool,
        gate_dbfs: float,
        gate_hold_ms: int,
        chunk_ms: int,
    ) -> None:
        self._gain = 10 ** (gain_db / 20.0)
        self._gate_enabled = gate_enabled
        self._gate_dbfs = gate_dbfs
        self._hold_chunks = max(1, math.ceil(gate_hold_ms / chunk_ms))
        self._remaining_hold_chunks = 0

    def process(self, pcm: bytes) -> tuple[bytes, float, bool]:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        if self._gain != 1.0:
            samples *= self._gain
            samples = np.clip(np.rint(samples), -32768, 32767)
            pcm = samples.astype("<i2").tobytes()
        level = _pcm_dbfs(pcm)
        if not self._gate_enabled:
            return pcm, level, False
        if level >= self._gate_dbfs:
            self._remaining_hold_chunks = self._hold_chunks
        elif self._remaining_hold_chunks > 0:
            self._remaining_hold_chunks -= 1
        else:
            return bytes(len(pcm)), level, True
        return pcm, level, False


class AudioDependencyError(RuntimeError):
    pass


class DeviceResolutionError(RuntimeError):
    def __init__(self, message: str, issue: CaptureIssue) -> None:
        super().__init__(message)
        self.issue = issue


def _import_pyaudio() -> Any:
    try:
        import pyaudiowpatch as pyaudio
    except ImportError as exc:
        raise AudioDependencyError("未安装 PyAudioWPatch") from exc
    return pyaudio


def _host_api_name(p: Any, info: dict[str, Any]) -> str:
    try:
        return str(p.get_host_api_info_by_index(int(info["hostApi"]))["name"])
    except (KeyError, TypeError, ValueError, OSError):
        return "unknown"


def _descriptor(p: Any, info: dict[str, Any]) -> DeviceDescriptor:
    return DeviceDescriptor(
        index=int(info["index"]),
        name=str(info.get("name", "")),
        host_api=_host_api_name(p, info),
        max_input_channels=int(info.get("maxInputChannels", 0)),
        max_output_channels=int(info.get("maxOutputChannels", 0)),
        default_sample_rate=int(float(info.get("defaultSampleRate", 0))),
        is_loopback=bool(info.get("isLoopbackDevice", False)),
        raw=dict(info),
    )


def _refresh_input_device(p: Any, device: DeviceDescriptor) -> DeviceDescriptor:
    """在真正打开流的 PyAudio 实例中重新解析设备，避免使用过期 index。"""
    if device.is_loopback:
        candidates = [_descriptor(p, item) for item in p.get_loopback_device_info_generator()]
    else:
        candidates = [
            _descriptor(p, p.get_device_info_by_index(index))
            for index in range(p.get_device_count())
        ]
    matches = [
        item
        for item in candidates
        if item.name.casefold() == device.name.casefold()
        and item.host_api.casefold().endswith("wasapi")
        and item.max_input_channels > 0
    ]
    if len(matches) != 1:
        raise DeviceResolutionError(
            f"打开音频流前无法重新唯一解析设备：{device.name}",
            CaptureIssue.DEVICE_DISCONNECTED,
        )
    return matches[0]


def _refresh_output_device(p: Any, device: DeviceDescriptor) -> DeviceDescriptor:
    candidates = [
        _descriptor(p, p.get_device_info_by_index(index))
        for index in range(p.get_device_count())
    ]
    matches = [
        item
        for item in candidates
        if item.name.casefold() == device.name.casefold()
        and item.host_api.casefold().endswith("wasapi")
        and item.max_output_channels > 0
    ]
    if len(matches) != 1:
        raise DeviceResolutionError(
            f"打开音频流前无法重新唯一解析输出设备：{device.name}",
            CaptureIssue.DEVICE_DISCONNECTED,
        )
    return matches[0]


class DeviceManager:
    def list_devices(self) -> list[DeviceDescriptor]:
        pyaudio = _import_pyaudio()
        with pyaudio.PyAudio() as p:
            return [
                _descriptor(p, p.get_device_info_by_index(i))
                for i in range(p.get_device_count())
            ]

    def resolve_microphone(self, selector: DeviceSelector) -> DeviceDescriptor:
        pyaudio = _import_pyaudio()
        with pyaudio.PyAudio() as p:
            if selector.use_default:
                return self._default_wasapi_device(p, pyaudio, "input")
            candidates = [
                _descriptor(p, p.get_device_info_by_index(i))
                for i in range(p.get_device_count())
            ]
            return self._unique_match(candidates, selector.name, "input")

    def resolve_remote_loopback(self, selector: DeviceSelector) -> DeviceDescriptor:
        pyaudio = _import_pyaudio()
        with pyaudio.PyAudio() as p:
            return self._resolve_remote_loopback_in_instance(p, pyaudio, selector)

    def probe_remote_loopback(
        self,
        selector: DeviceSelector,
        *,
        duration_seconds: float = 2.0,
        detection_dbfs: float = -48.0,
    ) -> dict[str, Any]:
        """仅在内存中统计回环电平，不保存、不推流音频。"""
        pyaudio = _import_pyaudio()
        chunks = 0
        captured_bytes = 0
        peak_dbfs = -120.0

        with _PORTAUDIO_LIFECYCLE_LOCK:
            p = pyaudio.PyAudio()
            device = self._resolve_remote_loopback_in_instance(p, pyaudio, selector)
            native_frames = max(1, int(device.default_sample_rate * 0.05))

            def callback(
                in_data: bytes | None,
                _frame_count: int,
                _time_info: dict[str, Any],
                _status_flags: int,
            ) -> tuple[None, int]:
                nonlocal chunks, captured_bytes, peak_dbfs
                if in_data:
                    samples = np.frombuffer(in_data, dtype="<i2").astype(np.float32)
                    rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
                    dbfs = 20.0 * math.log10(max(rms, 1.0) / 32768.0)
                    peak_dbfs = max(peak_dbfs, dbfs)
                    chunks += 1
                    captured_bytes += len(in_data)
                return (None, pyaudio.paContinue)

            stream = p.open(
                format=pyaudio.paInt16,
                channels=max(1, device.max_input_channels),
                rate=device.default_sample_rate,
                input=True,
                input_device_index=device.index,
                frames_per_buffer=native_frames,
                stream_callback=callback,
            )
        try:
            deadline = monotonic() + duration_seconds
            while monotonic() < deadline:
                threading.Event().wait(min(0.05, deadline - monotonic()))
        finally:
            with _PORTAUDIO_LIFECYCLE_LOCK:
                stream.stop_stream()
                stream.close()
                p.terminate()
        return {
            "device": device.name,
            "duration_ms": round(duration_seconds * 1000),
            "callback_chunks": chunks,
            "captured_bytes": captured_bytes,
            "peak_dbfs": round(peak_dbfs, 2),
            "audio_detected": chunks > 0 and peak_dbfs >= detection_dbfs,
        }

    def probe_microphone(
        self,
        selector: DeviceSelector,
        *,
        duration_seconds: float = 3.5,
        quiet_seconds: float = 1.0,
        detection_dbfs: float = -48.0,
    ) -> dict[str, Any]:
        """先采集安静底噪、再采集讲话峰值；仅在内存中计算电平。"""
        pyaudio = _import_pyaudio()
        levels: list[tuple[float, float]] = []
        captured_bytes = 0
        started_at = monotonic()

        with _PORTAUDIO_LIFECYCLE_LOCK:
            p = pyaudio.PyAudio()
            if selector.use_default:
                device = self._default_wasapi_device(p, pyaudio, "input")
            else:
                candidates = [
                    _descriptor(p, p.get_device_info_by_index(index))
                    for index in range(p.get_device_count())
                ]
                device = self._unique_match(candidates, selector.name, "input")
            native_frames = max(1, int(device.default_sample_rate * 0.05))

            def callback(
                in_data: bytes | None,
                _frame_count: int,
                _time_info: dict[str, Any],
                _status_flags: int,
            ) -> tuple[None, int]:
                nonlocal captured_bytes
                if in_data:
                    levels.append((monotonic() - started_at, _pcm_dbfs(in_data)))
                    captured_bytes += len(in_data)
                return (None, pyaudio.paContinue)

            stream = p.open(
                format=pyaudio.paInt16,
                channels=max(1, device.max_input_channels),
                rate=device.default_sample_rate,
                input=True,
                input_device_index=device.index,
                frames_per_buffer=native_frames,
                stream_callback=callback,
            )
        try:
            deadline = monotonic() + duration_seconds
            while monotonic() < deadline:
                threading.Event().wait(min(0.05, deadline - monotonic()))
        finally:
            with _PORTAUDIO_LIFECYCLE_LOCK:
                stream.stop_stream()
                stream.close()
                p.terminate()

        quiet = [level for elapsed, level in levels if elapsed <= quiet_seconds]
        speaking = [level for elapsed, level in levels if elapsed > quiet_seconds]
        noise_floor = float(np.median(quiet)) if quiet else -120.0
        peak = max(speaking, default=max((level for _, level in levels), default=-120.0))
        snr = max(0.0, peak - noise_floor)
        if snr >= 15:
            quality = "good"
            recommendation = "信噪比较好，可按建议阈值启用噪声门"
        elif snr >= 10:
            quality = "marginal"
            recommendation = "信噪比一般，优先靠近麦克风或使用按住说话"
        else:
            quality = "poor"
            recommendation = "信噪比不足，不建议启用噪声门；请使用按住说话"
        recommended_gate: float | None = None
        if snr >= 10:
            value = min(noise_floor + 6.0, peak - 8.0)
            recommended_gate = round(min(-20.0, max(-80.0, value)), 1)
        return {
            "device": device.name,
            "duration_ms": round(duration_seconds * 1000),
            "quiet_phase_ms": round(quiet_seconds * 1000),
            "callback_chunks": len(levels),
            "captured_bytes": captured_bytes,
            "noise_floor_dbfs": round(noise_floor, 2),
            "speech_peak_dbfs": round(peak, 2),
            "estimated_snr_db": round(snr, 2),
            "recommended_gate_dbfs": recommended_gate,
            "calibration_quality": quality,
            "recommendation": recommendation,
            "audio_detected": bool(speaking) and peak >= detection_dbfs,
        }

    def _resolve_remote_loopback_in_instance(
        self, p: Any, pyaudio: Any, selector: DeviceSelector
    ) -> DeviceDescriptor:
        loopbacks = [_descriptor(p, item) for item in p.get_loopback_device_info_generator()]
        if selector.use_default:
            helper = getattr(p, "get_default_wasapi_loopback", None)
            if callable(helper):
                return _descriptor(p, helper())
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            output = _descriptor(p, p.get_device_info_by_index(wasapi["defaultOutputDevice"]))
            return self._unique_match(loopbacks, output.name, "input")
        return self._unique_match(loopbacks, selector.name, "input")

    def resolve_output(self, selector: DeviceSelector) -> DeviceDescriptor:
        pyaudio = _import_pyaudio()
        with pyaudio.PyAudio() as p:
            if selector.use_default:
                return self._default_wasapi_device(p, pyaudio, "output")
            candidates = [
                _descriptor(p, p.get_device_info_by_index(i))
                for i in range(p.get_device_count())
            ]
            return self._unique_match(candidates, selector.name, "output")

    @staticmethod
    def _unique_match(
        candidates: list[DeviceDescriptor], name: str, capability: Literal["input", "output"]
    ) -> DeviceDescriptor:
        capable = [
            item
            for item in candidates
            if item.host_api.casefold().endswith("wasapi")
            and (
                item.max_input_channels > 0
                if capability == "input"
                else item.max_output_channels > 0
            )
        ]
        exact = [item for item in capable if item.name.casefold() == name.casefold()]
        matches = exact or [item for item in capable if name.casefold() in item.name.casefold()]
        if not matches:
            raise DeviceResolutionError(
                f"没有找到唯一的 {capability} 设备：{name}", CaptureIssue.DEVICE_NOT_FOUND
            )
        if len(matches) > 1:
            raise DeviceResolutionError(
                f"设备名称匹配到多个 {capability} 设备，请填写更完整名称：{name}",
                CaptureIssue.DEVICE_AMBIGUOUS,
            )
        return matches[0]

    @staticmethod
    def _default_wasapi_device(
        p: Any, pyaudio: Any, capability: Literal["input", "output"]
    ) -> DeviceDescriptor:
        try:
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            key = "defaultInputDevice" if capability == "input" else "defaultOutputDevice"
            index = int(wasapi[key])
            if index < 0:
                raise ValueError("default device is unavailable")
            device = _descriptor(p, p.get_device_info_by_index(index))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise DeviceResolutionError(
                f"没有可用的 Windows WASAPI 默认 {capability} 设备",
                CaptureIssue.DEVICE_NOT_FOUND,
            ) from exc
        channel_count = (
            device.max_input_channels
            if capability == "input"
            else device.max_output_channels
        )
        if channel_count <= 0 or not device.host_api.casefold().endswith("wasapi"):
            raise DeviceResolutionError(
                f"Windows WASAPI 默认 {capability} 设备能力不匹配",
                CaptureIssue.FORMAT_UNSUPPORTED,
            )
        return device


class StreamingResampler:
    def __init__(self, input_rate: int, input_channels: int, output_rate: int = 16000) -> None:
        try:
            import soxr
        except ImportError as exc:
            raise AudioDependencyError("未安装 soxr") from exc
        self._stream = soxr.ResampleStream(
            input_rate,
            output_rate,
            1,
            dtype="float32",
            quality="HQ",
        )
        self.input_channels = input_channels

    def process_pcm16(self, data: bytes) -> bytes:
        samples = np.frombuffer(data, dtype="<i2")
        if self.input_channels > 1:
            usable = len(samples) - (len(samples) % self.input_channels)
            samples = (
                samples[:usable]
                .reshape(-1, self.input_channels)
                .astype(np.float32)
                .mean(axis=1)
            )
        else:
            samples = samples.astype(np.float32)
        normalized = samples / 32768.0
        converted = self._stream.resample_chunk(normalized, last=False)
        clipped = np.clip(converted, -1.0, 32767.0 / 32768.0)
        return np.rint(clipped * 32768.0).astype("<i2").tobytes()


class StreamingOutputConverter:
    """把云端单声道 PCM16 转换为物理输出端点的原生采样率和通道数。"""

    def __init__(self, input_rate: int, output_rate: int, output_channels: int) -> None:
        try:
            import soxr
        except ImportError as exc:
            raise AudioDependencyError("未安装 soxr") from exc
        self._soxr = soxr
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.output_channels = output_channels

    def process_pcm16(self, data: bytes) -> bytes:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        converted = self._soxr.resample(
            samples,
            self.input_rate,
            self.output_rate,
            quality="HQ",
        )
        clipped = np.clip(converted, -1.0, 32767.0 / 32768.0)
        mono = np.rint(clipped * 32768.0).astype("<i2")
        if self.output_channels == 1:
            return mono.tobytes()
        return np.repeat(mono[:, None], self.output_channels, axis=1).reshape(-1).tobytes()


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        *,
        channel: Channel,
        device: DeviceDescriptor,
        settings: AudioSettings,
        output: DropOldestQueue[AudioFrame],
        status: StatusCallback,
        transmit_enabled: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(name=f"{channel}-capture", daemon=False)
        self.channel = channel
        self.device = device
        self.settings = settings
        self.output = output
        self.status = status
        self.transmit_enabled = transmit_enabled
        self.stop_event = threading.Event()
        self.sequence = 0
        self._last_activity: CaptureState | None = None
        self._silent_ms = 0
        self._microphone_processor = (
            MicrophoneProcessor(
                gain_db=settings.microphone_gain_db,
                gate_enabled=settings.microphone_noise_gate_enabled,
                gate_dbfs=settings.microphone_noise_gate_dbfs,
                gate_hold_ms=settings.microphone_noise_gate_hold_ms,
                chunk_ms=settings.chunk_ms,
            )
            if channel == "local"
            else None
        )

    def stop(self) -> None:
        self.stop_event.set()

    def _emit(
        self,
        state: CaptureState,
        issue: CaptureIssue = CaptureIssue.NONE,
        message: str = "",
        **counters: int | float,
    ) -> None:
        self.status(
            StatusEvent(
                component="audio_capture",
                channel=self.channel,
                state=state.value,
                category=issue.value,
                message=message,
                counters=counters,
            )
        )

    def run(self) -> None:
        pyaudio = _import_pyaudio()
        p: Any | None = None
        stream: Any | None = None
        self._emit(CaptureState.OPENING, message=self.device.name)
        try:
            with _PORTAUDIO_LIFECYCLE_LOCK:
                p = pyaudio.PyAudio()
                active_device = _refresh_input_device(p, self.device)
                channels = max(1, active_device.max_input_channels)
                native_rate = active_device.default_sample_rate
                native_frames = max(1, int(native_rate * self.settings.chunk_ms / 1000))
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=native_rate,
                    input=True,
                    input_device_index=active_device.index,
                    frames_per_buffer=native_frames,
                )
            resampler = StreamingResampler(
                native_rate, channels, self.settings.input_sample_rate_hz
            )
            target_bytes = int(
                self.settings.input_sample_rate_hz * self.settings.chunk_ms / 1000 * 2
            )
            pending = bytearray()
            self._emit(CaptureState.READY, message=active_device.name)
            self._emit(CaptureState.CAPTURING, message=active_device.name)
            no_data_since = monotonic()
            while not self.stop_event.is_set():
                try:
                    if stream.get_read_available() < native_frames:
                        silent_ms = int((monotonic() - no_data_since) * 1000)
                        if (
                            silent_ms >= self.settings.silence_report_after_ms
                            and self._last_activity != CaptureState.SILENT
                        ):
                            self._last_activity = CaptureState.SILENT
                            self._emit(
                                CaptureState.SILENT,
                                CaptureIssue.PROLONGED_SILENCE,
                                "设备已打开，但尚未交付音频帧；请检查视频实际播放端点",
                                silent_ms=silent_ms,
                            )
                        self.stop_event.wait(0.02)
                        continue
                    raw = stream.read(native_frames, exception_on_overflow=True)
                    no_data_since = monotonic()
                except OSError as exc:
                    issue = classify_capture_exception(exc)
                    if issue == CaptureIssue.INPUT_OVERFLOW:
                        self._emit(CaptureState.OVERFLOW, issue, overflows=1)
                        continue
                    state = (
                        CaptureState.DEVICE_LOST
                        if issue == CaptureIssue.DEVICE_DISCONNECTED
                        else CaptureState.ERROR
                    )
                    self._emit(state, issue, str(exc)[:160])
                    break
                converted = resampler.process_pcm16(raw)
                pending.extend(converted)
                while len(pending) >= target_bytes:
                    chunk = bytes(pending[:target_bytes])
                    del pending[:target_bytes]
                    if self._microphone_processor is not None:
                        chunk, _input_level, _gated = self._microphone_processor.process(chunk)
                    if self.transmit_enabled is not None and not self.transmit_enabled():
                        chunk = bytes(len(chunk))
                    self._report_activity(chunk)
                    frame = AudioFrame(
                        sequence=self.sequence,
                        captured_at_ns=monotonic_ns(),
                        pcm=chunk,
                        format=AudioFormat(self.settings.input_sample_rate_hz, 1),
                    )
                    self.sequence += 1
                    if self.output.put_latest(frame):
                        self._emit(
                            CaptureState.DEGRADED,
                            CaptureIssue.INPUT_OVERFLOW,
                            queue_dropped=self.output.dropped,
                        )
        except BaseException as exc:
            issue = classify_capture_exception(exc)
            self._emit(CaptureState.ERROR, issue, str(exc)[:160])
        finally:
            self._emit(CaptureState.STOPPING)
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except BaseException:
                    pass
            if p is not None:
                with _PORTAUDIO_LIFECYCLE_LOCK:
                    p.terminate()
            self._emit(CaptureState.STOPPED)

    def _report_activity(self, chunk: bytes) -> None:
        dbfs = _pcm_dbfs(chunk)
        if dbfs >= self.settings.silence_dbfs:
            self._silent_ms = 0
            state = CaptureState.SPEECH
            issue = CaptureIssue.NONE
        else:
            self._silent_ms += self.settings.chunk_ms
            if self._silent_ms < self.settings.silence_report_after_ms:
                return
            state = CaptureState.SILENT
            issue = CaptureIssue.PROLONGED_SILENCE
        if state != self._last_activity:
            self._last_activity = state
            self._emit(state, issue, dbfs=round(dbfs, 2), silent_ms=self._silent_ms)


class VirtualOutputWorker(threading.Thread):
    def __init__(
        self,
        *,
        device: DeviceDescriptor,
        settings: AudioSettings,
        buffer: PcmRingBuffer,
        status: StatusCallback,
        signal_threshold_dbfs: float | None = None,
    ) -> None:
        super().__init__(name="virtual-output", daemon=False)
        self.device = device
        self.settings = settings
        self.buffer = buffer
        self.status = status
        self.signal_threshold_dbfs = signal_threshold_dbfs
        self.stop_event = threading.Event()
        self._last_state: OutputState | None = None
        self.first_signal_write_ns: int | None = None
        self.peak_dbfs = -120.0

    def stop(self) -> None:
        self.stop_event.set()

    def _emit(
        self,
        state: OutputState,
        category: str = "none",
        message: str = "",
        **counters: int | float,
    ) -> None:
        if state == self._last_state and not counters and not message:
            return
        self._last_state = state
        self.status(
            StatusEvent(
                component="virtual_output",
                channel="local",
                state=state.value,
                category=category,
                message=message,
                counters=counters,
            )
        )

    def run(self) -> None:
        pyaudio = _import_pyaudio()
        p: Any | None = None
        stream: Any | None = None
        started = False
        self._emit(OutputState.OPENING)
        source_frames = int(
            self.settings.output_sample_rate_hz * self.settings.chunk_ms / 1000
        )
        source_bytes_per_chunk = source_frames * 2
        try:
            with _PORTAUDIO_LIFECYCLE_LOCK:
                p = pyaudio.PyAudio()
                active_device = _refresh_output_device(p, self.device)
                native_rate = active_device.default_sample_rate
                native_channels = min(2, max(1, active_device.max_output_channels))
                native_frames = int(native_rate * self.settings.chunk_ms / 1000)
                stream = p.open(
                    format=pyaudio.paInt16,
                    channels=native_channels,
                    rate=native_rate,
                    output=True,
                    output_device_index=active_device.index,
                    frames_per_buffer=native_frames,
                )
            converter = StreamingOutputConverter(
                self.settings.output_sample_rate_hz,
                native_rate,
                native_channels,
            )
            self._emit(
                OutputState.READY,
                message=active_device.name,
                source_rate_hz=self.settings.output_sample_rate_hz,
                device_rate_hz=native_rate,
                device_channels=native_channels,
            )
            while not self.stop_event.is_set():
                had_data = self.buffer.size > 0
                source_data, missing = self.buffer.read_or_silence(source_bytes_per_chunk)
                if had_data:
                    started = True
                output_data = converter.process_pcm16(source_data)
                write_started_ns = monotonic_ns()
                try:
                    stream.write(output_data)
                except OSError as exc:
                    issue = classify_capture_exception(exc)
                    state = (
                        OutputState.DEVICE_LOST
                        if issue == CaptureIssue.DEVICE_DISCONNECTED
                        else OutputState.ERROR
                    )
                    self._emit(state, issue.value, str(exc)[:160])
                    break
                level = _pcm_dbfs(source_data) if had_data else -120.0
                self.peak_dbfs = max(self.peak_dbfs, level)
                first_signal = (
                    had_data
                    and self.first_signal_write_ns is None
                    and self.signal_threshold_dbfs is not None
                    and level >= self.signal_threshold_dbfs
                )
                if first_signal:
                    self.first_signal_write_ns = write_started_ns
                    self._emit(
                        OutputState.PLAYING,
                        first_signal=1,
                        dbfs=round(level, 2),
                    )
                elif started and missing:
                    self._emit(
                        OutputState.UNDERRUN,
                        "buffer_underrun",
                        underrun_bytes=self.buffer.underrun_bytes,
                    )
                elif had_data:
                    self._emit(OutputState.PLAYING)
        except BaseException as exc:
            self._emit(
                OutputState.ERROR,
                classify_capture_exception(exc).value,
                f"{type(exc).__name__}: {str(exc)[:140]}",
            )
        finally:
            self._emit(OutputState.STOPPING)
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except BaseException:
                    pass
            if p is not None:
                with _PORTAUDIO_LIFECYCLE_LOCK:
                    p.terminate()
            self._emit(OutputState.STOPPED)
