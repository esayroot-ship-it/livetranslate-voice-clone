from __future__ import annotations

import asyncio

from .models import CaptureIssue, PushIssue


def classify_capture_exception(exc: BaseException) -> CaptureIssue:
    text = str(exc).lower()
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if code == -9981 or "overflow" in text:
        return CaptureIssue.INPUT_OVERFLOW
    if code in {-9996, -9985} or "invalid device" in text or "disconnected" in text:
        return CaptureIssue.DEVICE_DISCONNECTED
    if code in {-9997, -9998} or "unsupported" in text or "sample format" in text:
        return CaptureIssue.FORMAT_UNSUPPORTED
    if code == -9990 or "permission" in text or "access denied" in text:
        return CaptureIssue.PERMISSION_DENIED
    if code == -9986 or "device unavailable" in text or "busy" in text:
        return CaptureIssue.DEVICE_BUSY
    return CaptureIssue.INTERNAL_ERROR


def classify_push_exception(exc: BaseException) -> PushIssue:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in {401, 403}:
        return PushIssue.AUTHENTICATION if status == 401 else PushIssue.PERMISSION
    if status == 429:
        return PushIssue.RATE_LIMIT
    if isinstance(status, int) and status >= 500:
        return PushIssue.SERVICE_UNAVAILABLE
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return PushIssue.NETWORK_TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return PushIssue.NETWORK_DISCONNECTED
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "connectionclosed" in name or "connection closed" in text:
        return PushIssue.NETWORK_DISCONNECTED
    if "authentication" in text or "unauthorized" in text or "invalid_api_key" in text:
        return PushIssue.AUTHENTICATION
    if "permission" in text or "forbidden" in text:
        return PushIssue.PERMISSION
    if "rate" in text and "limit" in text:
        return PushIssue.RATE_LIMIT
    if "invalid_request" in text or "invalid_value" in text or "protocol" in name:
        return PushIssue.PROTOCOL_INVALID
    return PushIssue.INTERNAL_ERROR
