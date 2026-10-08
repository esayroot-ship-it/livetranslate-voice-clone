from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic_ns
from typing import Any, Literal

Channel = Literal["remote", "local"]


class CaptureState(StrEnum):
    IDLE = "idle"
    DISCOVERING = "discovering"
    OPENING = "opening"
    READY = "ready"
    CAPTURING = "capturing"
    SPEECH = "speech"
    SILENT = "silent"
    DEGRADED = "degraded"
    OVERFLOW = "overflow"
    DEVICE_LOST = "device_lost"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class CaptureIssue(StrEnum):
    NONE = "none"
    DEVICE_NOT_FOUND = "device_not_found"
    DEVICE_AMBIGUOUS = "device_ambiguous"
    FORMAT_UNSUPPORTED = "format_unsupported"
    PERMISSION_DENIED = "permission_denied"
    DEVICE_BUSY = "device_busy"
    DEVICE_DISCONNECTED = "device_disconnected"
    INPUT_OVERFLOW = "input_overflow"
    PROLONGED_SILENCE = "prolonged_silence"
    INTERNAL_ERROR = "internal_error"


class PushState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONFIGURING = "configuring"
    CONFIGURED = "configured"
    STREAMING = "streaming"
    BACKPRESSURE = "backpressure"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    NETWORK_ERROR = "network_error"
    PROTOCOL_ERROR = "protocol_error"
    SERVICE_ERROR = "service_error"
    RECONNECT_WAIT = "reconnect_wait"
    FINISHING = "finishing"
    FINISHED = "finished"
    STOPPED = "stopped"
    ERROR = "error"


class PushIssue(StrEnum):
    NONE = "none"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    INVALID_CONFIGURATION = "invalid_configuration"
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_DISCONNECTED = "network_disconnected"
    PROTOCOL_INVALID = "protocol_invalid"
    SERVICE_UNAVAILABLE = "service_unavailable"
    QUEUE_BACKPRESSURE = "queue_backpressure"
    FINISH_TIMEOUT = "finish_timeout"
    INTERNAL_ERROR = "internal_error"


class OutputState(StrEnum):
    IDLE = "idle"
    DISCOVERING = "discovering"
    OPENING = "opening"
    READY = "ready"
    PLAYING = "playing"
    UNDERRUN = "underrun"
    OVERFLOW = "overflow"
    DEVICE_LOST = "device_lost"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AudioFormat:
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int = 2
    encoding: str = "pcm_s16le"


@dataclass(frozen=True, slots=True)
class AudioFrame:
    sequence: int
    captured_at_ns: int
    pcm: bytes
    format: AudioFormat = field(default_factory=lambda: AudioFormat(16000, 1))


@dataclass(frozen=True, slots=True)
class StatusEvent:
    component: str
    state: str
    category: str = "none"
    message: str = ""
    channel: Channel | None = None
    counters: dict[str, int | float] = field(default_factory=dict)
    at_ns: int = field(default_factory=monotonic_ns)


@dataclass(frozen=True, slots=True)
class SubtitleUpdate:
    channel: Channel
    source_item_id: str
    translation_item_id: str | None
    source_confirmed: str = ""
    source_stash: str = ""
    translation_confirmed: str = ""
    translation_stash: str = ""
    source_final: bool = False
    translation_final: bool = False


@dataclass(frozen=True, slots=True)
class DeviceDescriptor:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: int
    is_loopback: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
