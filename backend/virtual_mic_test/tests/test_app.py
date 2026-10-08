from ai_interpreter.audio import CaptureIssue, DeviceResolutionError
from virtual_mic_test import app


def test_check_classifies_missing_device_as_configuration_error(monkeypatch):
    monkeypatch.setattr(app, "load_test_settings", lambda _path: object())

    def missing_device(_self):
        raise DeviceResolutionError(
            "没有找到唯一的 output 设备：CABLE Input",
            CaptureIssue.DEVICE_NOT_FOUND,
        )

    monkeypatch.setattr(app.VirtualMicTestEngine, "validate", missing_device)
    assert app.main(["--config", "ignored.yaml", "--check"]) == 2


def test_stop_command_uses_graceful_shutdown_endpoint(monkeypatch):
    monkeypatch.setattr(app, "load_test_settings", lambda _path: object())
    called = []
    monkeypatch.setattr(app, "stop_running_server", lambda path: called.append(path) or 0)

    assert app.main(["--config", "station.yaml", "--stop"]) == 0
    assert str(called[0]) == "station.yaml"
