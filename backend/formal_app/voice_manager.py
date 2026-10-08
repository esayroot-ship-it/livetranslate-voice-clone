from __future__ import annotations

import base64
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import MODEL, ConfigStore

ROOT = Path(__file__).resolve().parents[1]
VOICE_SOURCE = ROOT / "voice_clone" / "src"
if str(VOICE_SOURCE) not in sys.path:
    sys.path.insert(0, str(VOICE_SOURCE))

from bailian_voice_clone.client import (  # noqa: E402
    AudioSample,
    VoiceCloneClient,
    VoiceCloneConfigurationError,
    VoiceCloneSettings,
    build_create_payload,
)


class VoiceManager:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    def _settings(self) -> VoiceCloneSettings:
        raw = self.store.raw()
        aliyun = raw["aliyun"]
        voice = raw["voice"]
        return VoiceCloneSettings(
            config_path=self.store.config_path,
            api_key=os.environ.get("DASHSCOPE_API_KEY", "").strip()
            or str(aliyun.get("api_key", "")),
            workspace_id=str(aliyun.get("workspace_id", "")),
            region=aliyun["region"],
            request_timeout_seconds=float(voice["request_timeout_seconds"]),
            enrollment_model=voice["enrollment_model"],
            target_model=MODEL,
            preferred_name=voice["preferred_name"],
            page_size=int(voice["page_size"]),
            recording_prompt=voice["recording_prompt"],
        )

    @staticmethod
    def _sample(data_url: str) -> AudioSample:
        match = re.fullmatch(r"data:(audio/(?:wav|mpeg|mp4));base64,([A-Za-z0-9+/=]+)", data_url)
        if not match:
            raise VoiceCloneConfigurationError("样本必须是 WAV、MP3 或 M4A 的 data URL")
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except ValueError as exc:
            raise VoiceCloneConfigurationError("声音样本 Base64 无效") from exc
        if not decoded or len(data_url) >= 10_000_000:
            raise VoiceCloneConfigurationError("声音样本为空或编码后超过 10MB")
        return AudioSample(data=data_url, mime_type=match.group(1), source="browser-upload")

    def list(self, page_index: int = 0, page_size: int | None = None) -> dict[str, Any]:
        settings = self._settings()
        result = VoiceCloneClient(settings).list(
            page_index=page_index, page_size=page_size or settings.page_size
        )
        return {"voices": [asdict(item) for item in result.voices], "request_id": result.request_id}

    def create(
        self, *, data_url: str, preferred_name: str, transcript: str, language: str
    ) -> dict[str, Any]:
        settings = self._settings()
        payload = build_create_payload(
            settings,
            self._sample(data_url),
            preferred_name=preferred_name,
            target_model=MODEL,
            transcript=transcript,
            language=language,
        )
        result = VoiceCloneClient(settings).create(payload)
        return asdict(result)

    def delete(self, voice: str) -> dict[str, Any]:
        return asdict(VoiceCloneClient(self._settings()).delete(voice))

    def select(self, voice: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", voice):
            raise VoiceCloneConfigurationError("voice_id 格式无效")
        return self.store.save({"voice": {"voice_id": voice}})

    def prompt(self) -> str:
        return str(self.store.raw()["voice"]["recording_prompt"])
