from __future__ import annotations

import base64
from pathlib import Path

import pytest

from ai_interpreter.buffers import PcmRingBuffer
from ai_interpreter.config import (
    AliyunSettings,
    AppSettings,
    AudioSettings,
    DeviceSelector,
    LoggingSettings,
    QueueSettings,
    SessionSettings,
    SubtitleSettings,
    VadSettings,
    VoiceSettings,
    WebSettings,
)
from ai_interpreter.protocol import CloudEventProcessor, ProtocolError, build_session_update


def _app() -> AppSettings:
    remote = SessionSettings("remote", "en", "zh", ("text",), None, {"API": "API"})
    local = SessionSettings(
        "local",
        "zh",
        "en",
        ("text", "audio"),
        "qwen-translate-vc-test",
        {"接口": "API"},
        enable_voice_clone=True,
    )
    selector = DeviceSelector("WASAPI", "device", False)
    return AppSettings(
        config_path=Path("settings.yaml"),
        mode="full_duplex",
        language_preset="local_zh_remote_en",
        aliyun=AliyunSettings(
            api_key="test-secret",
            region="cn-beijing",
            workspace_id="ws-test",
            model="qwen3.5-livetranslate-flash-realtime",
            connect_timeout_seconds=10,
            configure_timeout_seconds=10,
            finish_timeout_seconds=15,
            max_message_bytes=2_097_152,
        ),
        voice=VoiceSettings("qwen-translate-vc-test", "never"),
        audio=AudioSettings(selector, selector, selector, 16000, 24000, 100, -48, 3000),
        vad=VadSettings("server_vad", 0.2, 500),
        queues=QueueSettings(20, 3000),
        reconnect_delays_seconds=(1, 2, 4, 8, 15),
        logging=LoggingSettings("INFO", Path("logs"), 5, 10000),
        web=WebSettings("127.0.0.1", 8765, False),
        subtitle=SubtitleSettings(True, True, 28, 50),
        remote_session=remote,
        local_session=local,
    )


def test_session_update_matches_official_modalities() -> None:
    app = _app()
    remote = build_session_update(app, app.remote_session)["session"]
    local = build_session_update(app, app.local_session)["session"]
    assert remote["modalities"] == ["text"]
    assert "voice" not in remote
    assert remote["input_audio_transcription"]["model"] == "qwen3-asr-flash-realtime"
    assert local["modalities"] == ["text", "audio"]
    assert local["voice_clone_options"] == {"frequency": "never"}
    assert local["sample_rate"] == 16000


def test_event_processor_replaces_stash_and_correlates_items() -> None:
    app = _app()
    statuses = []
    subtitles = []
    flags = {"configured": 0, "finished": 0}
    processor = CloudEventProcessor(
        session=app.remote_session,
        expected_model=app.aliyun.model,
        audio_buffer=None,
        on_status=statuses.append,
        on_subtitle=subtitles.append,
        on_configured=lambda: flags.__setitem__("configured", 1),
        on_finished=lambda: flags.__setitem__("finished", 1),
    )
    processor.process_message(
        '{"event_id":"1","type":"conversation.item.input_audio_transcription.text",'
        '"item_id":"src1","text":"I","stash":" think"}'
    )
    processor.process_message(
        '{"event_id":"2","type":"response.text.text","item_id":"tr1","text":"我","stash":"认"}'
    )
    processor.process_message(
        '{"event_id":"3","type":"conversation.item.created","previous_item_id":"src1",'
        '"item":{"id":"tr1"}}'
    )
    processor.process_message(
        '{"event_id":"4","type":"response.text.text","item_id":"tr1","text":"我","stash":"认为"}'
    )
    assert subtitles[-1].source_confirmed + subtitles[-1].source_stash == "I think"
    assert subtitles[-1].translation_confirmed + subtitles[-1].translation_stash == "我认为"


def test_duplicate_audio_event_is_only_written_once() -> None:
    app = _app()
    buffer = PcmRingBuffer(100)
    processor = CloudEventProcessor(
        session=app.local_session,
        expected_model=app.aliyun.model,
        audio_buffer=buffer,
        on_status=lambda _: None,
        on_subtitle=lambda _: None,
        on_configured=lambda: None,
        on_finished=lambda: None,
    )
    pcm = b"\x01\x00" * 4
    encoded = base64.b64encode(pcm).decode()
    event = f'{{"event_id":"same","type":"response.audio.delta","delta":"{encoded}"}}'
    processor.process_message(event)
    processor.process_message(event)
    assert buffer.size == len(pcm)


def test_invalid_base64_is_protocol_error() -> None:
    app = _app()
    processor = CloudEventProcessor(
        session=app.local_session,
        expected_model=app.aliyun.model,
        audio_buffer=PcmRingBuffer(100),
        on_status=lambda _: None,
        on_subtitle=lambda _: None,
        on_configured=lambda: None,
        on_finished=lambda: None,
    )
    with pytest.raises(ProtocolError):
        processor.process_message(
            '{"event_id":"x","type":"response.audio.delta","delta":"not-base64!!!"}'
        )


def test_english_target_guard_holds_audio_until_english_translation_arrives() -> None:
    app = _app()
    buffer = PcmRingBuffer(200)
    statuses = []
    processor = CloudEventProcessor(
        session=app.local_session,
        expected_model=app.aliyun.model,
        audio_buffer=buffer,
        on_status=statuses.append,
        on_subtitle=lambda _: None,
        on_configured=lambda: None,
        on_finished=lambda: None,
        target_language_guard_enabled=True,
    )
    pcm = b"\x01\x00" * 8
    encoded = base64.b64encode(pcm).decode()
    processor.process_message(
        f'{{"event_id":"audio-1","type":"response.audio.delta",'
        f'"item_id":"tr1","delta":"{encoded}"}}'
    )
    assert buffer.size == 0
    processor.process_message(
        '{"event_id":"text-1","type":"response.audio_transcript.text",'
        '"item_id":"tr1","text":"Hello","stash":" everyone"}'
    )
    assert buffer.size == len(pcm)
    assert any(item.state == "approved_english" for item in statuses)


def test_english_target_guard_blocks_final_chinese_audio() -> None:
    app = _app()
    buffer = PcmRingBuffer(200)
    statuses = []
    processor = CloudEventProcessor(
        session=app.local_session,
        expected_model=app.aliyun.model,
        audio_buffer=buffer,
        on_status=statuses.append,
        on_subtitle=lambda _: None,
        on_configured=lambda: None,
        on_finished=lambda: None,
        target_language_guard_enabled=True,
    )
    encoded = base64.b64encode(b"\x01\x00" * 8).decode()
    processor.process_message(
        f'{{"event_id":"audio-zh","type":"response.audio.delta",'
        f'"item_id":"tr-zh","delta":"{encoded}"}}'
    )
    processor.process_message(
        '{"event_id":"text-zh","type":"response.audio_transcript.done",'
        '"item_id":"tr-zh","transcript":"这是中文原声"}'
    )
    assert buffer.size == 0
    blocked = [item for item in statuses if item.state == "blocked_non_english"]
    assert blocked and blocked[-1].counters["discarded_bytes"] == 16


def test_clone_audio_without_output_device_is_classified_and_discarded() -> None:
    app = _app()
    statuses = []
    processor = CloudEventProcessor(
        session=app.local_session,
        expected_model=app.aliyun.model,
        audio_buffer=None,
        on_status=statuses.append,
        on_subtitle=lambda _: None,
        on_configured=lambda: None,
        on_finished=lambda: None,
    )
    encoded = base64.b64encode(b"\x01\x00" * 4).decode()
    processor.process_message(
        f'{{"event_id":"audio-no-device","type":"response.audio.delta","delta":"{encoded}"}}'
    )
    assert statuses[-1].component == "cloud_audio"
    assert statuses[-1].state == "received_no_output"
    assert statuses[-1].counters["discarded_bytes"] == 8


def test_session_updated_is_strictly_validated() -> None:
    app = _app()
    processor = CloudEventProcessor(
        session=app.remote_session,
        expected_model=app.aliyun.model,
        audio_buffer=None,
        on_status=lambda _: None,
        on_subtitle=lambda _: None,
        on_configured=lambda: None,
        on_finished=lambda: None,
    )
    with pytest.raises(ProtocolError, match="采样率"):
        processor.process_message(
            '{"event_id":"x","type":"session.updated","session":'
            '{"model":"qwen3.5-livetranslate-flash-realtime","sample_rate":8000,'
            '"input_audio_format":"pcm","modalities":["text"],'
            '"input_audio_transcription":{"model":"qwen3-asr-flash-realtime","language":"en"},'
            '"translation":{"language":"zh"}}}'
        )
