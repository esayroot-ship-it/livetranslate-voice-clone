from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from collections.abc import Callable
from time import monotonic
from typing import Any

from .buffers import DropOldestQueue, PcmRingBuffer
from .classification import classify_push_exception
from .config import AppSettings, SessionSettings
from .models import AudioFrame, PushIssue, PushState, StatusEvent, SubtitleUpdate
from .protocol import (
    CloudEventProcessor,
    CloudServiceError,
    ProtocolError,
    build_audio_append,
    build_session_finish,
    build_session_update,
)


class LiveTranslateSession:
    def __init__(
        self,
        *,
        app: AppSettings,
        session: SessionSettings,
        input_queue: DropOldestQueue[AudioFrame],
        audio_buffer: PcmRingBuffer | None,
        stop_event: threading.Event,
        on_status: Callable[[StatusEvent], None],
        on_subtitle: Callable[[SubtitleUpdate], None],
    ) -> None:
        self.app = app
        self.session = session
        self.input_queue = input_queue
        self.audio_buffer = audio_buffer
        self.stop_event = stop_event
        self.on_status = on_status
        self.on_subtitle = on_subtitle
        self._configured: asyncio.Event | None = None
        self._finished: asyncio.Event | None = None
        self._logger = logging.getLogger(f"session.{session.channel}")
        self._stream_started_at: float | None = None

    async def run(self) -> None:
        delays = self.app.reconnect_delays_seconds
        attempt = 0
        while not self.stop_event.is_set():
            try:
                await self._run_once()
                return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if (
                    self._stream_started_at is not None
                    and monotonic() - self._stream_started_at >= 30
                ):
                    attempt = 0
                issue = (
                    exc.issue
                    if isinstance(exc, CloudServiceError)
                    else classify_push_exception(exc)
                )
                state = self._state_for_issue(issue)
                self._emit(state, issue, type(exc).__name__)
                self.input_queue.clear()
                if self.audio_buffer is not None:
                    self.audio_buffer.clear()
                if not self._retryable(issue) or attempt >= len(delays) or self.stop_event.is_set():
                    self._emit(PushState.ERROR, issue, "session stopped after non-retryable error")
                    return
                delay = delays[attempt]
                attempt += 1
                self._emit(
                    PushState.RECONNECT_WAIT,
                    issue,
                    "waiting before reconnect",
                    reconnect_attempt=attempt,
                    reconnect_delay_seconds=delay,
                )
                await self._wait_or_stop(delay)
        self._emit(PushState.STOPPED)

    async def _run_once(self) -> None:
        import websockets

        self._configured = asyncio.Event()
        self._finished = asyncio.Event()
        self.input_queue.clear()
        processor = CloudEventProcessor(
            session=self.session,
            expected_model=self.app.aliyun.model,
            audio_buffer=self.audio_buffer,
            on_status=self.on_status,
            on_subtitle=self.on_subtitle,
            on_configured=self._configured.set,
            on_finished=self._finished.set,
            target_language_guard_enabled=self.app.audio.target_language_guard_enabled,
        )
        self._emit(PushState.CONNECTING)
        api_key = self._api_key()
        ws = await self._connect(websockets, api_key)
        receiver: asyncio.Task[None] | None = None
        sender: asyncio.Task[None] | None = None
        stop_watcher: asyncio.Task[None] | None = None
        try:
            self._emit(PushState.CONFIGURING)
            await ws.send(
                json.dumps(build_session_update(self.app, self.session), ensure_ascii=False)
            )
            receiver = asyncio.create_task(self._receive(ws, processor))
            await asyncio.wait_for(
                self._configured.wait(), timeout=self.app.aliyun.configure_timeout_seconds
            )
            self._emit(PushState.CONFIGURED)
            self.input_queue.clear()
            if self.audio_buffer is not None:
                self.audio_buffer.clear()
            self._emit(PushState.STREAMING)
            self._stream_started_at = monotonic()
            sender = asyncio.create_task(self._send_audio(ws))
            stop_watcher = asyncio.create_task(self._watch_stop())
            done, _ = await asyncio.wait(
                {receiver, sender, stop_watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_watcher in done:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                self._emit(PushState.FINISHING)
                await ws.send(json.dumps(build_session_finish()))
                try:
                    await asyncio.wait_for(
                        self._finished.wait(), timeout=self.app.aliyun.finish_timeout_seconds
                    )
                except TimeoutError:
                    self._emit(
                        PushState.FINISHING,
                        PushIssue.FINISH_TIMEOUT,
                        "waiting for session.finished timed out",
                    )
                self._emit(PushState.FINISHED)
                return
            if receiver in done:
                await receiver
                raise ConnectionError("cloud receive loop ended unexpectedly")
            if sender in done:
                await sender
                raise ConnectionError("audio send loop ended unexpectedly")
        finally:
            for task in (receiver, sender, stop_watcher):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (receiver, sender, stop_watcher) if task is not None),
                return_exceptions=True,
            )
            await ws.close()

    async def _connect(self, websockets: Any, api_key: str) -> Any:
        kwargs = {
            "open_timeout": self.app.aliyun.connect_timeout_seconds,
            "close_timeout": 5,
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": self.app.aliyun.max_message_bytes,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            return await websockets.connect(
                self.app.aliyun.websocket_url, additional_headers=headers, **kwargs
            )
        except TypeError:
            return await websockets.connect(
                self.app.aliyun.websocket_url, extra_headers=headers, **kwargs
            )

    async def _receive(self, ws: Any, processor: CloudEventProcessor) -> None:
        protocol_errors = 0
        async for message in ws:
            try:
                processor.process_message(message)
                protocol_errors = 0
            except ProtocolError:
                protocol_errors += 1
                self._emit(
                    PushState.PROTOCOL_ERROR,
                    PushIssue.PROTOCOL_INVALID,
                    "invalid server event",
                    consecutive_protocol_errors=protocol_errors,
                )
                if protocol_errors >= 3:
                    raise

    async def _send_audio(self, ws: Any) -> None:
        while not self.stop_event.is_set():
            try:
                frame = await asyncio.to_thread(self.input_queue.get, 0.2)
            except queue.Empty:
                continue
            if frame.format.sample_rate_hz != 16000 or frame.format.channels != 1:
                raise ProtocolError("capture queue contains unsupported audio format")
            await ws.send(json.dumps(build_audio_append(frame.pcm)))

    async def _watch_stop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(0.05)

    async def _wait_or_stop(self, seconds: float) -> None:
        elapsed = 0.0
        while elapsed < seconds and not self.stop_event.is_set():
            step = min(0.1, seconds - elapsed)
            await asyncio.sleep(step)
            elapsed += step

    @staticmethod
    def _retryable(issue: PushIssue) -> bool:
        return issue in {
            PushIssue.RATE_LIMIT,
            PushIssue.NETWORK_TIMEOUT,
            PushIssue.NETWORK_DISCONNECTED,
            PushIssue.PROTOCOL_INVALID,
            PushIssue.SERVICE_UNAVAILABLE,
            PushIssue.INTERNAL_ERROR,
        }

    @staticmethod
    def _state_for_issue(issue: PushIssue) -> PushState:
        return {
            PushIssue.AUTHENTICATION: PushState.AUTH_FAILED,
            PushIssue.PERMISSION: PushState.AUTH_FAILED,
            PushIssue.RATE_LIMIT: PushState.RATE_LIMITED,
            PushIssue.NETWORK_TIMEOUT: PushState.NETWORK_ERROR,
            PushIssue.NETWORK_DISCONNECTED: PushState.NETWORK_ERROR,
            PushIssue.PROTOCOL_INVALID: PushState.PROTOCOL_ERROR,
            PushIssue.INVALID_CONFIGURATION: PushState.PROTOCOL_ERROR,
            PushIssue.SERVICE_UNAVAILABLE: PushState.SERVICE_ERROR,
        }.get(issue, PushState.ERROR)

    def _emit(
        self,
        state: PushState,
        issue: PushIssue = PushIssue.NONE,
        message: str = "",
        **counters: int | float,
    ) -> None:
        self.on_status(
            StatusEvent(
                component="cloud_push",
                channel=self.session.channel,
                state=state.value,
                category=issue.value,
                message=message,
                counters=counters,
            )
        )

    def _api_key(self) -> str:
        value = self.app.aliyun.api_key.strip()
        if not value:
            raise RuntimeError("Aliyun API key is not configured")
        return value


class DualSessionThread(threading.Thread):
    def __init__(self, remote: LiveTranslateSession, local: LiveTranslateSession) -> None:
        super().__init__(name="cloud-asyncio", daemon=False)
        self.remote = remote
        self.local = local
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:
            self.error = exc

    async def _main(self) -> None:
        await asyncio.gather(self.remote.run(), self.local.run())


class SingleSessionThread(threading.Thread):
    def __init__(self, session: LiveTranslateSession) -> None:
        super().__init__(name="cloud-asyncio", daemon=False)
        self.session = session
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            asyncio.run(self.session.run())
        except BaseException as exc:
            self.error = exc
