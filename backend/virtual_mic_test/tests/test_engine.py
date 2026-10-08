import threading
from pathlib import Path
from types import SimpleNamespace

from ai_interpreter.models import PushState, StatusEvent
from virtual_mic_test.engine import VirtualMicTestEngine


def test_cloud_metrics_use_actual_virtual_output_write_timestamp(tmp_path: Path) -> None:
    engine = VirtualMicTestEngine(tmp_path / "settings.yaml")
    engine._running = True
    engine._mic_speech_ns = 1_000_000_000
    engine._sink = SimpleNamespace(
        first_signal_ns=2_500_000_000,
        peak_dbfs=-12.0,
        bytes_written=48_000,
        path=tmp_path / "cloud_clone.wav",
    )
    engine._output_worker = SimpleNamespace(
        first_signal_write_ns=2_560_000_000,
        peak_dbfs=-13.0,
    )
    engine._recorder = SimpleNamespace(
        first_signal_ns=2_590_000_000,
        peak_dbfs=-14.0,
        path=tmp_path / "virtual_mic.wav",
        error=None,
    )

    result = engine._build_cloud_result()

    assert result["mic_to_cloud_audio_ms"] == 1500.0
    assert result["cloud_to_virtual_output_ms"] == 60.0
    assert result["mic_to_virtual_output_ms"] == 1560.0
    assert result["virtual_output_to_cable_recording_ms"] == 30.0
    assert result["mic_to_virtual_mic_ms"] == 1590.0
    assert result["virtual_output_started"] is True
    assert result["cable_return_detected"] is True
    assert result["cloud_bytes_written"] == 48_000


def test_output_metrics_do_not_depend_on_cable_return_detection(tmp_path: Path) -> None:
    engine = VirtualMicTestEngine(tmp_path / "settings.yaml")
    engine._mic_speech_ns = 1_000_000_000
    engine._sink = SimpleNamespace(
        first_signal_ns=2_000_000_000,
        peak_dbfs=-12.0,
        bytes_written=24_000,
        path=tmp_path / "cloud_clone.wav",
    )
    engine._output_worker = SimpleNamespace(
        first_signal_write_ns=2_050_000_000,
        peak_dbfs=-13.0,
    )
    engine._recorder = SimpleNamespace(
        first_signal_ns=None,
        peak_dbfs=-120.0,
        path=tmp_path / "virtual_mic.wav",
        error=None,
    )

    result = engine._build_cloud_result()

    assert result["cloud_to_virtual_output_ms"] == 50.0
    assert result["mic_to_virtual_output_ms"] == 1050.0
    assert result["virtual_output_to_cable_recording_ms"] is None
    assert result["cable_return_detected"] is False


def test_streaming_status_starts_audio_workers_without_holding_engine_lock(
    tmp_path: Path,
) -> None:
    engine = VirtualMicTestEngine(tmp_path / "settings.yaml")
    acquired: list[bool] = []

    def verify_lock_is_available() -> None:
        def acquire_from_worker() -> None:
            locked = engine._lock.acquire(timeout=0.2)
            acquired.append(locked)
            if locked:
                engine._lock.release()

        worker = threading.Thread(target=acquire_from_worker)
        worker.start()
        worker.join()

    engine._start_audio_workers = verify_lock_is_available  # type: ignore[method-assign]
    engine._on_status(
        StatusEvent(
            component="cloud_push",
            channel="local",
            state=PushState.STREAMING.value,
        )
    )

    assert acquired == [True]
