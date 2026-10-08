
from ai_interpreter.classification import classify_capture_exception, classify_push_exception
from ai_interpreter.models import CaptureIssue, PushIssue


def test_capture_error_categories() -> None:
    assert (
        classify_capture_exception(OSError(-9981, "Input overflowed"))
        == CaptureIssue.INPUT_OVERFLOW
    )
    assert (
        classify_capture_exception(OSError(-9996, "Invalid device"))
        == CaptureIssue.DEVICE_DISCONNECTED
    )
    assert (
        classify_capture_exception(PermissionError("access denied"))
        == CaptureIssue.PERMISSION_DENIED
    )


def test_push_error_categories() -> None:
    assert classify_push_exception(TimeoutError()) == PushIssue.NETWORK_TIMEOUT
    assert classify_push_exception(ConnectionError("closed")) == PushIssue.NETWORK_DISCONNECTED
