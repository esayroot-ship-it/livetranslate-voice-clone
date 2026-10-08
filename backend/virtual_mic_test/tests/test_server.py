import asyncio

import pytest
from fastapi import HTTPException

from ai_interpreter.audio import CaptureIssue, DeviceResolutionError
from virtual_mic_test.server import create_app


def test_validate_reports_missing_device_as_422(monkeypatch, tmp_path):
    main_config = tmp_path / "main.yaml"
    main_config.write_text("schema_version: 1\n", encoding="utf-8")
    config = tmp_path / "test.yaml"
    config.write_text(
        f"""
schema_version: 1
interpreter_config: {main_config.as_posix()}
web:
  host: 127.0.0.1
  port: 8776
  open_browser: false
devices:
  microphone:
    host_api: WASAPI
    name: ''
    use_default: true
  virtual_playback:
    host_api: WASAPI
    name: CABLE Input
    use_default: false
  virtual_recording:
    host_api: WASAPI
    name: CABLE Output
    use_default: false
audio:
  signal_threshold_dbfs: -48
  route_tone_hz: 1000
  route_tone_seconds: 1
  route_record_tail_ms: 800
  cloud_max_duration_seconds: 30
  output_buffer_ms: 5000
outputs:
  directory: outputs
  max_recordings: 20
""".strip(),
        encoding="utf-8",
    )
    app = create_app(config)

    def missing_device():
        raise DeviceResolutionError(
            "没有找到唯一的 output 设备：CABLE Input",
            CaptureIssue.DEVICE_NOT_FOUND,
        )

    # Patch the class method because the engine instance is private to create_app.
    monkeypatch.setattr(
        "virtual_mic_test.engine.VirtualMicTestEngine.validate", lambda self: missing_device()
    )
    route = next(route for route in app.routes if route.path == "/api/validate")
    with pytest.raises(HTTPException) as captured:
        asyncio.run(route.endpoint())

    assert captured.value.status_code == 422
    assert "CABLE Input" in captured.value.detail


def test_shutdown_endpoint_schedules_server_exit(tmp_path):
    main_config = tmp_path / "main.yaml"
    main_config.write_text("schema_version: 1\n", encoding="utf-8")
    config = tmp_path / "test.yaml"
    config.write_text(
        f"""
schema_version: 1
interpreter_config: {main_config.as_posix()}
web:
  host: 127.0.0.1
  port: 8776
  open_browser: false
devices:
  microphone: {{host_api: WASAPI, name: '', use_default: true}}
  virtual_playback: {{host_api: WASAPI, name: CABLE Input, use_default: false}}
  virtual_recording: {{host_api: WASAPI, name: CABLE Output, use_default: false}}
audio:
  signal_threshold_dbfs: -48
  route_tone_hz: 1000
  route_tone_seconds: 1
  route_record_tail_ms: 800
  cloud_max_duration_seconds: 30
  output_buffer_ms: 5000
outputs:
  directory: outputs
  max_recordings: 20
""".strip(),
        encoding="utf-8",
    )
    stopped = []
    app = create_app(config, shutdown_callback=lambda: stopped.append(True))
    route = next(route for route in app.routes if route.path == "/api/shutdown")

    async def request_shutdown():
        result = await route.endpoint()
        await asyncio.sleep(0.25)
        return result

    assert asyncio.run(request_shutdown())["ok"] is True
    assert stopped == [True]
