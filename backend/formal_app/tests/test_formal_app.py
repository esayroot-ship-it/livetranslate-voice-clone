from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from ai_interpreter.audio import DeviceManager
from ai_interpreter.controller import InterpreterController
from ai_interpreter.models import DeviceDescriptor, StatusEvent, SubtitleUpdate
from ai_interpreter.overlay import subtitle_channels
from ai_interpreter.protocol import build_session_update
from formal_app.config import DEFAULT_CONFIG, ConfigStore, FormalConfigurationError
from formal_app.runtime import EventHub, FormalRuntime
from formal_app.server import create_app
from formal_app.testing import FormalTestLab, SubtitleTestManager


@pytest.fixture()
def store(tmp_path: Path) -> ConfigStore:
    settings = tmp_path / "settings.yaml"
    secrets = tmp_path / "settings.local.yaml"
    settings.write_text(
        yaml.safe_dump(copy.deepcopy(DEFAULT_CONFIG), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    secrets.write_text("aliyun:\n  api_key: test-secret\n", encoding="utf-8")
    return ConfigStore(settings, secrets)


def test_public_config_masks_key_and_preserves_it_on_blank_save(store: ConfigStore) -> None:
    public = store.public()
    assert public["aliyun"]["api_key"] == ""
    assert public["aliyun"]["api_key_configured"] is True
    store.save({"aliyun": {"api_key": "", "workspace_id": "workspace_123"}})
    assert store.raw()["aliyun"]["api_key"] == "test-secret"


def test_mode_contracts_map_to_remote_text_and_local_clone_audio(store: ConfigStore) -> None:
    store.save(
        {
            "app": {"mode": "full_interpretation"},
            "aliyun": {"workspace_id": "workspace_123"},
            "voice": {"voice_id": "qwen_voice_123", "clone_frequency": "never"},
        }
    )
    settings = store.to_app_settings(for_start=True)
    remote = build_session_update(settings, settings.remote_session)["session"]
    local = build_session_update(settings, settings.local_session)["session"]
    assert remote["modalities"] == ["text"]
    assert remote["input_audio_transcription"] == {"model": "qwen3-asr-flash-realtime"}
    assert local["modalities"] == ["text", "audio"]
    assert local["enable_voice_clone"] is True
    assert local["voice"] == "qwen_voice_123"
    assert local["translation"]["same_language_skip_options"]["skip_audio"] is True


def test_microphone_subtitle_mode_can_show_original_without_translation(
    store: ConfigStore,
) -> None:
    store.save(
        {
            "app": {"mode": "microphone_subtitle"},
            "microphone": {"translate_enabled": False, "clone_audio_enabled": False},
            "sessions": {"local": {"source_language": "zh"}},
            "subtitle": {"source_mode": "local"},
        }
    )
    settings = store.to_app_settings(for_start=False)
    assert settings.mode == "local_subtitle"
    assert settings.local_session.modalities == ("text",)
    assert settings.local_session.target_language == "zh"
    assert settings.local_session.same_language_skip_text is True
    assert settings.local_session.same_language_skip_audio is True
    assert settings.subtitle.source_mode == "local"


def test_microphone_subtitle_mode_can_output_cloned_translation(store: ConfigStore) -> None:
    store.save(
        {
            "app": {"mode": "microphone_subtitle"},
            "microphone": {"translate_enabled": True, "clone_audio_enabled": True},
            "voice": {"voice_id": "qwen_voice_123", "clone_frequency": "never"},
        }
    )
    settings = store.to_app_settings(for_start=False)
    assert settings.mode == "microphone_interpretation"
    assert settings.local_session.modalities == ("text", "audio")
    assert settings.local_session.enable_voice_clone is True
    assert settings.local_session.voice_id == "qwen_voice_123"


def test_microphone_clone_mode_validates_mic_and_virtual_output_without_remote_loopback(
    store: ConfigStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.save(
        {
            "app": {"mode": "microphone_subtitle"},
            "microphone": {"translate_enabled": True, "clone_audio_enabled": True},
            "voice": {"voice_id": "qwen_voice_123", "clone_frequency": "never"},
        }
    )
    microphone = DeviceDescriptor(1, "Physical Mic", "Windows WASAPI", 1, 0, 48000)
    virtual = DeviceDescriptor(2, "CABLE Input", "Windows WASAPI", 0, 2, 48000)
    monkeypatch.setattr(DeviceManager, "resolve_microphone", lambda *_args: microphone)
    monkeypatch.setattr(DeviceManager, "resolve_output", lambda *_args: virtual)

    def reject_remote(*_args: object) -> None:
        raise AssertionError("microphone-only mode must not resolve remote loopback")

    monkeypatch.setattr(DeviceManager, "resolve_remote_loopback", reject_remote)
    resolved = InterpreterController(store.to_app_settings()).validate_devices()
    assert resolved == {"microphone": "Physical Mic", "virtual_output": "CABLE Input"}


def test_clone_output_requires_explicit_different_source_and_target(store: ConfigStore) -> None:
    store.save(
        {
            "app": {"mode": "microphone_subtitle"},
            "microphone": {"translate_enabled": True, "clone_audio_enabled": True},
            "aliyun": {"workspace_id": "workspace_123"},
            "voice": {"voice_id": "qwen_voice_123", "clone_frequency": "never"},
            "sessions": {
                "local": {
                    "source_language": "auto",
                    "target_language": "en",
                }
            },
        }
    )
    with pytest.raises(FormalConfigurationError, match="明确选择麦克风源语言"):
        store.to_app_settings(for_start=True)
    store.save({"sessions": {"local": {"source_language": "en"}}})
    with pytest.raises(FormalConfigurationError, match="不能相同"):
        store.to_app_settings(for_start=True)


def test_push_to_talk_fails_closed_and_only_opens_while_held(store: ConfigStore) -> None:
    store.save(
        {
            "app": {"mode": "microphone_subtitle"},
            "microphone": {
                "translate_enabled": True,
                "clone_audio_enabled": False,
                "push_to_talk_enabled": True,
            },
        }
    )
    controller = InterpreterController(store.to_app_settings())
    assert controller._microphone_transmit_enabled() is False
    controller.set_microphone_transmit(True)
    assert controller._microphone_transmit_enabled() is True
    controller.set_microphone_transmit(False)
    assert controller._microphone_transmit_enabled() is False


def test_push_to_talk_shortcut_is_configurable_and_validated(store: ConfigStore) -> None:
    store.save({"microphone": {"push_to_talk_shortcut": "Ctrl+Shift+M"}})
    assert store.raw()["microphone"]["push_to_talk_shortcut"] == "Ctrl+Shift+M"
    with pytest.raises(FormalConfigurationError, match="快捷键"):
        store.save({"microphone": {"push_to_talk_shortcut": "Space"}})


def test_controller_stops_cloud_before_virtual_output_to_preserve_audio_tail(
    store: ConfigStore,
) -> None:
    order: list[str] = []

    class FakeCloud:
        def join(self, timeout: float) -> None:
            order.append(f"cloud:{timeout}")

    class FakeVirtual:
        def is_alive(self) -> bool:
            return True

        def stop(self) -> None:
            order.append("virtual-stop")

        def join(self, timeout: float) -> None:
            order.append(f"virtual-join:{timeout}")

    class FakeBuffer:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def size(self) -> int:
            self.calls += 1
            return 8 if self.calls <= 2 else 0

        def clear(self) -> int:
            return 0

    controller = InterpreterController(store.to_app_settings())
    controller._started = True
    controller._cloud_thread = FakeCloud()  # type: ignore[assignment]
    controller._virtual_output = FakeVirtual()  # type: ignore[assignment]
    controller._audio_buffer = FakeBuffer()  # type: ignore[assignment]
    controller.stop()
    assert order[0].startswith("cloud:")
    assert order[1:] == ["virtual-stop", "virtual-join:3"]


def test_subtitle_source_modes_map_to_expected_channels() -> None:
    assert subtitle_channels("remote") == ("remote",)
    assert subtitle_channels("local") == ("local",)
    assert subtitle_channels("both") == ("remote", "local")
    assert subtitle_channels("invalid") == ("remote",)


def test_runtime_snapshot_keeps_local_microphone_subtitle_separate(store: ConfigStore) -> None:
    runtime = FormalRuntime(store, EventHub())
    runtime._on_subtitle(
        SubtitleUpdate(
            channel="local",
            source_item_id="local-source-1",
            translation_item_id="local-translation-1",
            source_confirmed="今天开始演讲",
            translation_confirmed="Today I begin the presentation",
        )
    )
    snapshot = runtime.snapshot()
    assert snapshot["subtitles"]["local"]["source_confirmed"] == "今天开始演讲"
    assert snapshot["subtitles"]["local"]["translation_confirmed"].startswith("Today")
    assert "remote" not in snapshot["subtitles"]


def test_formal_web_pages_and_health_are_separate_routes(store: ConfigStore) -> None:
    app = create_app(store)
    paths = {route.path for route in app.routes}
    assert "/" in paths
    assert "/{page_name}.html" in paths
    assert "/api/runtime/start" in paths
    assert "/api/test/cloud/start" in paths
    assert "/api/test/subtitle/start" in paths
    assert "/api/test/subtitle/status" in paths
    assert "/api/devices/probe-microphone" in paths
    assert "/api/runtime/microphone-transmit" in paths
    assert "/api/voices" in paths
    health = next(route.endpoint for route in app.routes if route.path == "/api/health")
    assert asyncio.run(health())["app"] == "formal_interpreter"


def test_recording_delete_rejects_path_traversal(store: ConfigStore) -> None:
    lab = FormalTestLab(store)
    with pytest.raises(ValueError):
        lab.resolve_recording("../secret.wav")


def test_config_route_never_returns_secret(store: ConfigStore) -> None:
    app = create_app(store)
    get_config = next(
        route.endpoint
        for route in app.routes
        if route.path == "/api/config" and "GET" in (route.methods or set())
    )
    body = asyncio.run(get_config())
    assert body["aliyun"]["api_key"] == ""
    assert "test-secret" not in str(body)


def test_config_save_persists_while_runtime_is_active_and_requires_restart(
    store: ConfigStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(FormalRuntime, "running", property(lambda _self: True))
    app = create_app(store)
    put_config = next(
        route.endpoint
        for route in app.routes
        if route.path == "/api/config" and "PUT" in (route.methods or set())
    )
    result = asyncio.run(
        put_config(
            {
                "allow_pending_restart": True,
                "sessions": {"remote": {"target_language": "en"}},
                "vad": {"threshold": 0.35},
            }
        )
    )
    assert result["save_status"] == {
        "restart_required": True,
        "active_components": ["正式程序"],
    }
    assert store.raw()["sessions"]["remote"]["target_language"] == "en"
    assert store.raw()["vad"]["threshold"] == 0.35
    with pytest.raises(HTTPException) as blocked:
        asyncio.run(put_config({"audio": {"chunk_ms": 50}}))
    assert blocked.value.status_code == 409


def test_subtitle_test_tracks_latency_and_final_history(store: ConfigStore) -> None:
    manager = SubtitleTestManager(store)
    manager._on_status(
        StatusEvent(
            component="latency",
            channel="remote",
            state="subtitle_first",
            counters={"server_vad_to_first_ms": 812.5},
        )
    )
    manager._on_subtitle(
        SubtitleUpdate(
            channel="remote",
            source_item_id="source-1",
            translation_item_id="translation-1",
            source_confirmed="Good morning",
            translation_confirmed="早上好",
            source_final=True,
            translation_final=True,
        )
    )
    snapshot = manager.snapshot()
    assert snapshot["metrics"]["server_vad_to_first_subtitle_ms"] == 812.5
    assert snapshot["metrics"]["final_segments"] == 1
    assert snapshot["history"] == [{"source": "Good morning", "translation": "早上好"}]


def test_test_page_and_config_help_expose_new_guidance() -> None:
    root = Path(__file__).resolve().parents[1] / "web"
    assert "字幕效果测试" in (root / "test.html").read_text(encoding="utf-8")
    config_script = (root / "config.js").read_text(encoding="utf-8")
    assert "同语言跳过" in config_script
    assert "目标语言仅 zh / en" in config_script
    assert "beforeunload" in config_script
    assert "restart_required" in config_script
    assert "保存并校验" in (root / "config.html").read_text(encoding="utf-8")


def test_overview_aggregates_each_measured_latency_without_overwrite(
    store: ConfigStore,
) -> None:
    runtime = FormalRuntime(store, EventHub())
    events = (
        StatusEvent(
            component="audio_capture",
            channel="remote",
            state="speech",
            at_ns=1_000_000_000,
        ),
        StatusEvent(
            component="cloud_vad", channel="remote", state="speech_started", at_ns=1_125_000_000
        ),
        StatusEvent(
            component="latency",
            channel="remote",
            state="subtitle_first",
            counters={"server_vad_to_first_ms": 812.5},
            at_ns=1_937_500_000,
        ),
        StatusEvent(
            component="audio_capture",
            channel="local",
            state="speech",
            at_ns=2_000_000_000,
        ),
        StatusEvent(
            component="cloud_vad", channel="local", state="speech_started", at_ns=2_200_000_000
        ),
        StatusEvent(
            component="latency",
            channel="local",
            state="subtitle_first",
            counters={"server_vad_to_first_ms": 500.0},
            at_ns=2_700_000_000,
        ),
        StatusEvent(
            component="latency",
            channel="local",
            state="audio_first",
            counters={"server_vad_to_first_ms": 850.0},
            at_ns=3_050_000_000,
        ),
        StatusEvent(
            component="virtual_output", channel="local", state="playing", at_ns=3_090_000_000
        ),
    )
    for event in events:
        runtime._on_status(event)

    snapshot = runtime.snapshot()
    assert snapshot["latencies"] == {
        "remote_capture_to_vad_ms": 125.0,
        "remote_vad_to_subtitle_ms": 812.5,
        "local_capture_to_vad_ms": 200.0,
        "local_vad_to_translation_ms": 500.0,
        "local_vad_to_audio_ms": 850.0,
        "cloud_audio_to_cable_ms": 40.0,
        "local_vad_to_cable_ms": 890.0,
    }
    assert "local:latency:subtitle_first" in snapshot["statuses"]
    assert "local:latency:audio_first" in snapshot["statuses"]


def test_overview_exposes_readable_controls_and_latency_cards() -> None:
    root = Path(__file__).resolve().parents[1] / "web"
    page = (root / "index.html").read_text(encoding="utf-8")
    styles = (root / "dashboard.css").read_text(encoding="utf-8")
    assert "启动程序" in page
    assert "停止程序" in page
    assert "启动前检查" in page
    assert "latency-local-cable" in page
    assert ".hero .btn.ghost" in styles
    assert "color: #f7fbf9" in styles


def test_audio_page_exposes_microphone_parameters_and_one_click_presets() -> None:
    root = Path(__file__).resolve().parents[1] / "web"
    page = (root / "audio.html").read_text(encoding="utf-8")
    script = (root / "audio.js").read_text(encoding="utf-8")
    assert "audio.microphone_gain_db" in page
    assert "audio.microphone_noise_gate_enabled" in page
    assert "audio.target_language_guard_enabled" in page
    assert "防止中文原声直接发给对方" in page
    assert "CABLE Output" in page
    assert "microphone.push_to_talk_enabled" in page
    assert 'data-path="audio.output_sample_rate_hz" data-type="number"' in page
    assert "vad.silence_duration_ms" in page
    assert "4 秒麦克风校准" in page
    assert "clear" in script
    assert "balanced" in script
    assert "low_latency" in script
    assert "noisy" not in script
    assert page.count("data-preset=") == 3
    assert "✓ 当前配置" in script
    assert "button.disabled = selected" in script


def test_dashboard_and_subtitle_page_expose_microphone_and_dual_source_modes() -> None:
    root = Path(__file__).resolve().parents[1] / "web"
    dashboard = (root / "index.html").read_text(encoding="utf-8")
    dashboard_script = (root / "dashboard.js").read_text(encoding="utf-8")
    subtitle_page = (root / "subtitles.html").read_text(encoding="utf-8")
    assert 'data-mode="microphone_subtitle"' in dashboard
    assert "microphone-translate" in dashboard
    assert "microphone-clone" in dashboard
    assert "ptt-enabled" in dashboard
    assert "ptt-hold" in dashboard
    assert "ptt-record-shortcut" in dashboard
    assert "自定义快捷键" in dashboard
    assert "savePttShortcut" in dashboard_script
    assert "local-subtitle-panel" in dashboard
    assert "meeting-subtitle-options" in dashboard
    assert 'data-subtitle-source="remote"' in dashboard
    assert 'data-subtitle-source="local"' in dashboard
    assert 'data-subtitle-source="both"' in dashboard
    assert 'payload.type === "subtitle"' in dashboard_script
    assert "saveMeetingSubtitleSource" in dashboard_script
    assert 'value="remote"' in subtitle_page
    assert 'value="local"' in subtitle_page
    assert 'value="both"' in subtitle_page
    assert "subtitle.show_channel_labels" in subtitle_page
    assert "subtitle.compact_background" in subtitle_page
    assert "subtitle.window_width_percent" in subtitle_page
    assert "subtitle.source_font_color" in subtitle_page
    assert "translation-contrast" in subtitle_page
    assert "会议同传模式也可以只显示" in subtitle_page
    assert "会议同传是什么" in dashboard_script
    assert "sourceByMode" not in dashboard_script


def test_subtitle_layout_settings_are_validated_and_mapped(store: ConfigStore) -> None:
    store.save(
        {
            "subtitle": {
                "source_mode": "both",
                "compact_background": True,
                "window_width_percent": 58,
                "horizontal_padding_px": 10,
                "vertical_padding_px": 4,
                "source_font_size_px": 14,
                "source_font_color": "#f1f5f3",
                "channel_label_color": "#ffe182",
                "background_opacity": 0.15,
            }
        }
    )
    subtitle = store.to_app_settings().subtitle
    assert subtitle.source_mode == "both"
    assert subtitle.compact_background is True
    assert subtitle.window_width_percent == 58
    assert subtitle.horizontal_padding_px == 10
    assert subtitle.vertical_padding_px == 4
    assert subtitle.source_font_size_px == 14
    assert subtitle.source_font_color == "#f1f5f3"
    assert subtitle.channel_label_color == "#ffe182"


def test_formal_audio_processing_parameters_are_validated_and_mapped(
    store: ConfigStore,
) -> None:
    store.save(
        {
            "audio": {
                "microphone_gain_db": 3.5,
                "microphone_noise_gate_enabled": True,
                "microphone_noise_gate_dbfs": -47.0,
                "microphone_noise_gate_hold_ms": 450,
                "target_language_guard_enabled": False,
                "chunk_ms": 50,
            },
            "vad": {"threshold": 0.35, "silence_duration_ms": 400},
            "queues": {"input_chunks": 16, "output_buffer_ms": 1500},
        }
    )
    settings = store.to_app_settings(for_start=False)
    assert settings.audio.microphone_gain_db == 3.5
    assert settings.audio.microphone_noise_gate_enabled is True
    assert settings.audio.microphone_noise_gate_dbfs == -47.0
    assert settings.audio.microphone_noise_gate_hold_ms == 450
    assert settings.audio.target_language_guard_enabled is False
    assert settings.audio.chunk_ms == 50
    assert settings.vad.threshold == 0.35
    assert settings.vad.silence_duration_ms == 400
    assert settings.queues.output_buffer_ms == 1500


def test_formal_audio_processing_rejects_unsafe_gain(store: ConfigStore) -> None:
    with pytest.raises(FormalConfigurationError, match="microphone_gain_db"):
        store.save({"audio": {"microphone_gain_db": 13}})
