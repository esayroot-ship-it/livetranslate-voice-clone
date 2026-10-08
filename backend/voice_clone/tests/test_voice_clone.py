from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
import yaml

from bailian_voice_clone.cli import main
from bailian_voice_clone.client import (
    AudioSample,
    AudioSampleError,
    VoiceCloneApiError,
    VoiceCloneClient,
    VoiceCloneConfigurationError,
    build_create_payload,
    load_voice_clone_settings,
    prepare_audio_sample,
)


def _write_wav(path: Path, *, channels: int = 1, rate: int = 24_000, seconds: int = 12) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * rate * seconds * channels)


def _write_config(tmp_path: Path, *, audio_file: Path | None = None) -> Path:
    path = tmp_path / "voice_clone.yaml"
    raw = {
        "schema_version": 1,
        "aliyun": {
            "api_key": "file-secret",
            "workspace_id": "ws-test123",
            "region": "cn-beijing",
            "request_timeout_seconds": 30,
        },
        "service": {
            "enrollment_model": "qwen-voice-enrollment",
            "target_model": "qwen3.5-livetranslate-flash-realtime",
            "preferred_name": "meeting_voice",
        },
        "audio": {
            "file": str(audio_file or (tmp_path / "voice.wav")),
            "url": "",
            "mime_type": "auto",
            "text": "这是一段测试录音。",
            "language": "zh",
            "max_encoded_bytes": 10_000_000,
            "validate_wav_metadata": True,
        },
        "listing": {"page_index": 0, "page_size": 10},
        "output": {"format": "table", "show_request_id": True},
        "recording": {"prompt": "这是一段测试录音。"},
    }
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


class FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def test_config_key_is_secret_and_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "environment-secret")
    settings = load_voice_clone_settings(_write_config(tmp_path))
    assert settings.api_key == "environment-secret"
    assert "environment-secret" not in repr(settings)
    assert settings.endpoint.endswith("/api/v1/services/audio/tts/customization")


def test_prepare_valid_wav_as_official_data_url(tmp_path: Path) -> None:
    audio = tmp_path / "voice.wav"
    _write_wav(audio)
    settings = load_voice_clone_settings(_write_config(tmp_path, audio_file=audio))
    sample = prepare_audio_sample(settings)
    assert sample.mime_type == "audio/wav"
    assert sample.data.startswith("data:audio/wav;base64,")
    assert sample.warnings == ()


def test_stereo_wav_is_rejected(tmp_path: Path) -> None:
    audio = tmp_path / "stereo.wav"
    _write_wav(audio, channels=2)
    settings = load_voice_clone_settings(_write_config(tmp_path, audio_file=audio))
    with pytest.raises(AudioSampleError, match="单声道"):
        prepare_audio_sample(settings)


def test_wav_shorter_than_three_seconds_is_rejected(tmp_path: Path) -> None:
    audio = tmp_path / "short.wav"
    _write_wav(audio, seconds=2)
    settings = load_voice_clone_settings(_write_config(tmp_path, audio_file=audio))
    with pytest.raises(AudioSampleError, match="至少包含 3 秒"):
        prepare_audio_sample(settings)


def test_explicit_mime_must_match_file_extension(tmp_path: Path) -> None:
    audio = tmp_path / "voice.wav"
    _write_wav(audio)
    settings = load_voice_clone_settings(_write_config(tmp_path, audio_file=audio))
    with pytest.raises(AudioSampleError, match="扩展名不一致"):
        prepare_audio_sample(settings, mime_type="audio/mpeg")


def test_cli_audio_url_overrides_configured_file(tmp_path: Path) -> None:
    settings = load_voice_clone_settings(_write_config(tmp_path))
    sample = prepare_audio_sample(settings, audio_url="https://example.com/sample.wav")
    assert sample.data == "https://example.com/sample.wav"
    assert sample.mime_type == "url"


def test_create_payload_matches_livetranslate_voice_contract(tmp_path: Path) -> None:
    settings = load_voice_clone_settings(_write_config(tmp_path))
    sample = AudioSample("https://example.com/voice.wav", "url", "remote")
    payload = build_create_payload(settings, sample)
    assert payload == {
        "model": "qwen-voice-enrollment",
        "input": {
            "action": "create",
            "target_model": "qwen3.5-livetranslate-flash-realtime",
            "preferred_name": "meeting_voice",
            "audio": {"data": "https://example.com/voice.wav"},
            "text": "这是一段测试录音。",
            "language": "zh",
        },
    }


def test_omni_model_is_not_accepted_as_livetranslate_voice_target(tmp_path: Path) -> None:
    settings = load_voice_clone_settings(_write_config(tmp_path))
    sample = AudioSample("https://example.com/voice.wav", "url", "remote")
    with pytest.raises(
        VoiceCloneConfigurationError,
        match="qwen3.5-livetranslate-flash-realtime",
    ):
        build_create_payload(
            settings,
            sample,
            target_model="qwen3.5-omni-plus-realtime",
        )


def test_client_create_uses_bearer_header_and_parses_voice(tmp_path: Path) -> None:
    settings = load_voice_clone_settings(_write_config(tmp_path))
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "output": {
                        "voice": "qwen-translate-vc-meeting_voice-123",
                        "target_model": "qwen3.5-livetranslate-flash-realtime",
                    },
                    "usage": {"count": 1},
                    "request_id": "req-create",
                },
            )
        ]
    )
    result = VoiceCloneClient(settings, session=session).create(
        {"model": "qwen-voice-enrollment", "input": {"action": "create"}}
    )
    assert result.voice == "qwen-translate-vc-meeting_voice-123"
    assert result.billed_count == 1
    assert session.calls[0]["headers"] == {
        "Authorization": "Bearer file-secret",
        "Content-Type": "application/json",
    }


def test_client_list_and_delete_use_official_actions(tmp_path: Path) -> None:
    settings = load_voice_clone_settings(_write_config(tmp_path))
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "output": {
                        "voice_list": [
                            {
                                "voice": "qwen-translate-vc-test-123",
                                "gmt_create": "2026-08-15 10:00:00",
                                "target_model": "qwen3.5-livetranslate-flash-realtime",
                            }
                        ]
                    },
                    "usage": {"count": 0},
                    "request_id": "req-list",
                },
            ),
            FakeResponse(200, {"usage": {"count": 0}, "request_id": "req-delete"}),
        ]
    )
    client = VoiceCloneClient(settings, session=session)
    listed = client.list(page_index=0, page_size=10)
    deleted = client.delete("qwen-translate-vc-test-123")
    assert listed.voices[0].voice == "qwen-translate-vc-test-123"
    assert deleted.request_id == "req-delete"
    assert session.calls[0]["json"] == {
        "model": "qwen-voice-enrollment",
        "input": {"action": "list", "page_size": 10, "page_index": 0},
    }
    assert session.calls[1]["json"] == {
        "model": "qwen-voice-enrollment",
        "input": {"action": "delete", "voice": "qwen-translate-vc-test-123"},
    }


def test_api_error_is_structured_without_raw_payload(tmp_path: Path) -> None:
    settings = load_voice_clone_settings(_write_config(tmp_path))
    session = FakeSession(
        [
            FakeResponse(
                401,
                {
                    "code": "InvalidApiKey",
                    "message": (
                        "Bearer file-secret "
                        "data:audio/wav;base64,U0VDUkVUX0FVRElP"
                    ),
                    "request_id": "r",
                },
            )
        ]
    )
    with pytest.raises(VoiceCloneApiError) as raised:
        VoiceCloneClient(settings, session=session).list(page_index=0, page_size=10)
    assert raised.value.status_code == 401
    assert raised.value.error_code == "InvalidApiKey"
    assert settings.api_key not in str(raised.value)
    assert "U0VDUkVUX0FVRElP" not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_prompt_command_does_not_require_runtime_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["aliyun"]["api_key"] = ""
    raw["aliyun"]["workspace_id"] = "请替换为百炼业务空间ID"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    assert main(["--config", str(path), "prompt"]) == 0
    assert capsys.readouterr().out.strip() == "这是一段测试录音。"


def test_json_serializable_result_shape(tmp_path: Path) -> None:
    settings = load_voice_clone_settings(_write_config(tmp_path))
    public = {
        "endpoint": settings.endpoint,
        "target_model": settings.target_model,
    }
    assert "file-secret" not in json.dumps(public)
