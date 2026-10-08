from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, SecretStr

from .audio import DeviceManager
from .config import ConfigurationError, DeviceSelector, load_settings
from .controller import InterpreterController
from .logging_setup import configure_logging
from .microphone_test import MicrophoneTestError, MicrophoneTestManager
from .models import StatusEvent, SubtitleUpdate

STATIC_DIR = Path(__file__).with_name("web") / "static"


def _port_is_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _existing_interpreter_is_healthy(host: str, port: int) -> bool:
    try:
        with urlopen(f"http://{host}:{port}/api/health", timeout=1.5) as response:  # noqa: S310
            payload = json.load(response)
    except (OSError, HTTPError, URLError, TimeoutError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def _windows_listener_pid(port: int) -> int | None:
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    suffix = f":{port}"
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) >= 5 and columns[1].endswith(suffix) and columns[3] == "LISTENING":
            try:
                return int(columns[4])
            except ValueError:
                return None
    return None


class ConfigUpdate(BaseModel):
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    region: str
    workspace_id: str = ""
    mode: str = "full_duplex"
    language_preset: str
    voice_id: str = ""
    microphone_name: str = ""
    microphone_use_default: bool = True
    remote_playback_name: str = ""
    remote_playback_use_default: bool = True
    virtual_output_name: str = "CABLE Input"
    virtual_output_enabled: bool = False
    input_chunk_ms: int = Field(ge=20, le=500)
    silence_dbfs: float = Field(ge=-90, le=-10)
    silence_report_after_ms: int = Field(ge=500, le=30000)
    vad_threshold: float = Field(ge=-1, le=1)
    vad_silence_ms: int = Field(ge=200, le=6000)
    input_queue_chunks: int = Field(ge=5, le=100)
    subtitle_show_source: bool = True
    subtitle_show_translation: bool = True
    subtitle_font_size_px: int = Field(ge=18, le=48)
    subtitle_max_segments: int = Field(ge=10, le=200)


class LoopbackProbeRequest(BaseModel):
    remote_playback_name: str = ""
    remote_playback_use_default: bool = True
    duration_ms: int = Field(default=2500, ge=500, le=5000)
    detection_dbfs: float = Field(default=-48.0, ge=-90, le=-10)


class MicrophoneTestRequest(BaseModel):
    duration_seconds: int = Field(default=20, ge=5, le=60)


class EventHub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def publish(self, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._publish_on_loop, payload)

    def call_soon(self, callback: Callable[[], None]) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(callback)

    def _publish_on_loop(self, payload: dict[str, Any]) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(payload)


class RuntimeManager:
    def __init__(self, config_path: Path, hub: EventHub) -> None:
        self.config_path = config_path
        self.hub = hub
        self.controller: InterpreterController | None = None
        self._lock = asyncio.Lock()
        self.latest_status: dict[str, dict[str, Any]] = {}
        self.latest_subtitle: dict[str, dict[str, Any]] = {}
        self.overlay_process: subprocess.Popen[bytes] | None = None

    @property
    def running(self) -> bool:
        return self.controller is not None

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self.controller is not None:
                raise HTTPException(status_code=409, detail="同声传译会话已经在运行")
            try:
                settings = load_settings(self.config_path)
            except ConfigurationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            configure_logging(settings.logging)
            controller = InterpreterController(settings)
            controller.subscribe_status(self._on_status)
            controller.subscribe_subtitle(self._on_subtitle)
            try:
                await asyncio.to_thread(controller.start)
            except BaseException as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"启动失败：{type(exc).__name__}: {str(exc)[:180]}",
                ) from exc
            self.controller = controller
            return {"running": True, "mode": settings.mode}

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            controller = self.controller
            self.controller = None
            if controller is not None:
                await asyncio.to_thread(controller.stop, 20.0)
            return {"running": False}

    def open_overlay(self) -> dict[str, Any]:
        if self.overlay_process is not None and self.overlay_process.poll() is None:
            return {"opened": False, "already_running": True}
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--config", str(self.config_path), "--overlay"]
        else:
            command = [
                sys.executable,
                "-m",
                "ai_interpreter.app",
                "--config",
                str(self.config_path),
                "--overlay",
            ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.overlay_process = subprocess.Popen(command, creationflags=creationflags)
        return {"opened": True, "already_running": False, "pid": self.overlay_process.pid}

    async def close_overlay(self) -> dict[str, Any]:
        self.hub.publish({"type": "overlay_command", "command": "close"})
        process = self.overlay_process
        if process is not None and process.poll() is None:
            await asyncio.sleep(0.35)
            if process.poll() is None:
                process.terminate()
            self.overlay_process = None
        return {"closed": True}

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "running": self.running,
            "statuses": self.latest_status,
            "subtitles": self.latest_subtitle,
        }

    def _on_status(self, event: StatusEvent) -> None:
        key = event.component if event.channel is None else f"{event.channel}:{event.component}"
        payload = {
            "type": "status",
            "key": key,
            "component": event.component,
            "channel": event.channel,
            "state": event.state,
            "category": event.category,
            "message": event.message[:180],
            "counters": event.counters,
            "at_ns": event.at_ns,
        }
        self.latest_status[key] = payload
        if event.component == "controller" and event.state == "stopped":
            self.controller = None
        self.hub.publish(payload)
        # 双路会话独立重连和失败；任一路 ERROR 不主动停止另一路。

    def _on_subtitle(self, update: SubtitleUpdate) -> None:
        payload = {
            "type": "subtitle",
            "channel": update.channel,
            "source_item_id": update.source_item_id,
            "translation_item_id": update.translation_item_id,
            "source_confirmed": update.source_confirmed,
            "source_stash": update.source_stash,
            "translation_confirmed": update.translation_confirmed,
            "translation_stash": update.translation_stash,
            "source_final": update.source_final,
            "translation_final": update.translation_final,
        }
        self.latest_subtitle[update.channel] = payload
        self.hub.publish(payload)


def _read_raw_config(config_path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=500, detail=f"无法读取配置文件：{exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="配置文件顶层不是对象")
    return raw


def _public_config(config_path: Path) -> dict[str, Any]:
    raw = _read_raw_config(config_path)
    aliyun = raw.get("aliyun", {})
    app = raw.get("app", {})
    audio = raw.get("audio", {})
    mic = audio.get("microphone", {})
    remote = audio.get("remote_playback", {})
    virtual = audio.get("virtual_output", {})
    vad = raw.get("vad", {})
    queues = raw.get("queues", {})
    subtitle = raw.get("subtitle", {})
    voice = raw.get("voice", {})
    configured_key = str(aliyun.get("api_key", "")).strip()
    env_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    source = "environment" if env_key else ("file" if configured_key else "none")
    return {
        "mode": app.get("mode", "local_subtitle"),
        "language_preset": app.get("language_preset", "local_zh_remote_en"),
        "region": aliyun.get("region", "cn-beijing"),
        "workspace_id": aliyun.get("workspace_id", ""),
        "model": aliyun.get("model", "qwen3.5-livetranslate-flash-realtime"),
        "api_key_configured": bool(env_key or configured_key),
        "api_key_source": source,
        "microphone": {
            "name": mic.get("name", ""),
            "use_default": bool(mic.get("use_default", True)),
        },
        "remote_playback": {
            "name": remote.get("name", ""),
            "use_default": bool(remote.get("use_default", True)),
        },
        "voice_id": voice.get("voice_id", ""),
        "virtual_output": {
            "name": virtual.get("name", "CABLE Input"),
            "enabled": bool(virtual.get("enabled", True)),
        },
        "audio": {
            "input_sample_rate_hz": audio.get("input_sample_rate_hz", 16000),
            "output_sample_rate_hz": audio.get("output_sample_rate_hz", 24000),
            "chunk_ms": audio.get("chunk_ms", 100),
            "silence_dbfs": audio.get("silence_dbfs", -48),
            "silence_report_after_ms": audio.get("silence_report_after_ms", 3000),
        },
        "vad": {
            "threshold": vad.get("threshold", 0.2),
            "silence_duration_ms": vad.get("silence_duration_ms", 500),
        },
        "queues": {"input_chunks": queues.get("input_chunks", 20)},
        "subtitle": {
            "show_source": subtitle.get("show_source", True),
            "show_translation": subtitle.get("show_translation", True),
            "font_size_px": subtitle.get("font_size_px", 28),
            "max_segments": subtitle.get("max_segments", 50),
        },
        "config_path": str(config_path),
    }


def _atomic_write_yaml(config_path: Path, raw: dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent, text=True
    )
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, config_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _update_config(config_path: Path, update: ConfigUpdate) -> dict[str, Any]:
    raw = _read_raw_config(config_path)
    raw.setdefault("app", {})["mode"] = update.mode
    raw["app"]["language_preset"] = update.language_preset
    raw.setdefault("voice", {})["voice_id"] = update.voice_id.strip()
    aliyun = raw.setdefault("aliyun", {})
    aliyun["region"] = update.region
    aliyun["workspace_id"] = update.workspace_id.strip()
    if update.clear_api_key:
        aliyun["api_key"] = ""
    elif update.api_key is not None and update.api_key.get_secret_value().strip():
        aliyun["api_key"] = update.api_key.get_secret_value().strip()

    audio = raw.setdefault("audio", {})
    audio.setdefault("microphone", {}).update(
        {
            "host_api": "WASAPI",
            "name": update.microphone_name.strip(),
            "use_default": update.microphone_use_default,
        }
    )
    audio.setdefault("remote_playback", {}).update(
        {
            "host_api": "WASAPI",
            "name": update.remote_playback_name.strip(),
            "use_default": update.remote_playback_use_default,
        }
    )
    audio.setdefault("virtual_output", {}).update(
        {
            "host_api": "WASAPI",
            "name": update.virtual_output_name.strip() or "CABLE Input",
            "use_default": False,
            "enabled": update.virtual_output_enabled,
        }
    )
    audio["chunk_ms"] = update.input_chunk_ms
    audio["silence_dbfs"] = update.silence_dbfs
    audio["silence_report_after_ms"] = update.silence_report_after_ms
    raw.setdefault("vad", {}).update(
        {
            "type": "server_vad",
            "threshold": update.vad_threshold,
            "silence_duration_ms": update.vad_silence_ms,
        }
    )
    raw.setdefault("queues", {})["input_chunks"] = update.input_queue_chunks
    raw.setdefault("subtitle", {}).update(
        {
            "show_source": update.subtitle_show_source,
            "show_translation": update.subtitle_show_translation,
            "font_size_px": update.subtitle_font_size_px,
            "max_segments": update.subtitle_max_segments,
        }
    )
    _atomic_write_yaml(config_path, raw)
    validation_error = None
    try:
        load_settings(config_path)
    except ConfigurationError as exc:
        validation_error = str(exc)
    result = _public_config(config_path)
    result["validation_error"] = validation_error
    return result


def create_web_app(config_path: str | Path) -> FastAPI:
    resolved_config = Path(config_path).expanduser().resolve()
    raw = _read_raw_config(resolved_config)
    web = raw.get("web", {})
    port = int(web.get("port", 8765))
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    hub = EventHub()
    runtime = RuntimeManager(resolved_config, hub)
    microphone_test = MicrophoneTestManager(resolved_config, hub.publish)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.bind_loop(asyncio.get_running_loop())
        yield
        await runtime.stop()
        await asyncio.to_thread(microphone_test.stop)
        await runtime.close_overlay()

    app = FastAPI(
        title="AI 会议同声传译本地控制台",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def local_origin_only(request: Request, call_next: Callable[..., Any]) -> Any:
        origin = request.headers.get("origin")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and origin
            and origin not in allowed_origins
        ):
            return JSONResponse(status_code=403, content={"detail": "拒绝非本机控制台来源"})
        return await call_next(request)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/assets/{asset_name}")
    async def asset(asset_name: str) -> FileResponse:
        if asset_name not in {"app.css", "app.js"}:
            raise HTTPException(status_code=404)
        media_type = "text/css" if asset_name.endswith(".css") else "application/javascript"
        return FileResponse(
            STATIC_DIR / asset_name,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "running": runtime.running,
            "microphone_test_running": microphone_test.running,
            "mode": _public_config(resolved_config)["mode"],
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return _public_config(resolved_config)

    @app.put("/api/config")
    async def put_config(update: ConfigUpdate) -> dict[str, Any]:
        if runtime.running or microphone_test.running:
            raise HTTPException(status_code=409, detail="请先停止同传或麦克风测试再修改配置")
        if update.region not in {"cn-beijing", "ap-southeast-1"}:
            raise HTTPException(status_code=422, detail="不支持的阿里云地域")
        if update.language_preset not in {"local_zh_remote_en", "local_en_remote_zh"}:
            raise HTTPException(status_code=422, detail="不支持的语言方向")
        if update.mode not in {"local_subtitle", "full_duplex"}:
            raise HTTPException(status_code=422, detail="不支持的运行模式")
        return _update_config(resolved_config, update)

    @app.get("/api/devices")
    async def devices() -> dict[str, Any]:
        try:
            items = await asyncio.to_thread(DeviceManager().list_devices)
        except BaseException as exc:
            raise HTTPException(
                status_code=503,
                detail=f"音频设备枚举失败：{type(exc).__name__}: {str(exc)[:160]}",
            ) from exc
        return {
            "devices": [
                {
                    "index": item.index,
                    "name": item.name,
                    "host_api": item.host_api,
                    "inputs": item.max_input_channels,
                    "outputs": item.max_output_channels,
                    "sample_rate": item.default_sample_rate,
                    "loopback": item.is_loopback,
                }
                for item in items
            ]
        }

    @app.post("/api/devices/probe-remote")
    async def probe_remote(request: LoopbackProbeRequest) -> dict[str, Any]:
        if runtime.running or microphone_test.running:
            raise HTTPException(status_code=409, detail="请先停止同传会话再单独测试媒体捕获")
        selector = DeviceSelector(
            host_api="WASAPI",
            name=request.remote_playback_name.strip(),
            use_default=request.remote_playback_use_default,
        )
        if not selector.use_default and not selector.name:
            raise HTTPException(status_code=422, detail="请选择一个会议播放设备")
        try:
            return await asyncio.to_thread(
                DeviceManager().probe_remote_loopback,
                selector,
                duration_seconds=request.duration_ms / 1000,
                detection_dbfs=request.detection_dbfs,
            )
        except BaseException as exc:
            raise HTTPException(
                status_code=503,
                detail=f"媒体回环测试失败：{type(exc).__name__}: {str(exc)[:160]}",
            ) from exc

    @app.post("/api/session/start")
    async def start_session() -> dict[str, Any]:
        if microphone_test.running:
            raise HTTPException(status_code=409, detail="请先停止麦克风翻译测试")
        return await runtime.start()

    @app.post("/api/session/stop")
    async def stop_session() -> dict[str, Any]:
        return await runtime.stop()

    @app.post("/api/microphone-test/start")
    async def start_microphone_test(request: MicrophoneTestRequest) -> dict[str, Any]:
        if runtime.running:
            raise HTTPException(status_code=409, detail="请先停止双路同传会话")
        try:
            return await asyncio.to_thread(microphone_test.start, request.duration_seconds)
        except MicrophoneTestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except BaseException as exc:
            raise HTTPException(
                status_code=500,
                detail=f"麦克风翻译测试启动失败：{type(exc).__name__}: {str(exc)[:160]}",
            ) from exc

    @app.post("/api/microphone-test/stop")
    async def stop_microphone_test() -> dict[str, Any]:
        return await asyncio.to_thread(microphone_test.stop)

    @app.get("/api/microphone-test/status")
    async def microphone_test_status() -> dict[str, Any]:
        return microphone_test.snapshot()

    @app.get("/api/microphone-test/audio/{name}")
    async def microphone_test_audio(name: str) -> FileResponse:
        path = microphone_test.resolve_recording(name)
        if path is None:
            raise HTTPException(status_code=404)
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=path.name,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/overlay/open")
    async def open_overlay() -> dict[str, Any]:
        try:
            return runtime.open_overlay()
        except BaseException as exc:
            raise HTTPException(
                status_code=500,
                detail=f"悬浮字幕启动失败：{type(exc).__name__}: {str(exc)[:160]}",
            ) from exc

    @app.post("/api/overlay/close")
    async def close_overlay() -> dict[str, Any]:
        return await runtime.close_overlay()

    @app.get("/api/session/status")
    async def session_status() -> dict[str, Any]:
        return runtime.snapshot()

    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin and origin not in allowed_origins:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = hub.subscribe()
        try:
            snapshot = runtime.snapshot()
            snapshot["microphone_test"] = microphone_test.snapshot()
            await websocket.send_json(snapshot)
            while True:
                await websocket.send_json(await queue.get())
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            hub.unsubscribe(queue)

    return app


def run_web_server(config_path: str | Path, *, no_browser: bool = False) -> int:
    import webbrowser

    import uvicorn

    resolved = Path(config_path).expanduser().resolve()
    raw = _read_raw_config(resolved)
    web = raw.get("web", {})
    host = str(web.get("host", "127.0.0.1"))
    port = int(web.get("port", 8765))
    if host not in {"127.0.0.1", "localhost"}:
        raise ConfigurationError("Web 控制台只能绑定 127.0.0.1 或 localhost")
    should_open = bool(web.get("open_browser", True)) and not no_browser
    probe_host = "127.0.0.1" if host == "localhost" else host
    if _port_is_listening(probe_host, port):
        if _existing_interpreter_is_healthy(probe_host, port):
            url = f"http://{probe_host}:{port}"
            if should_open:
                webbrowser.open(url)
            print(f"AI Interpreter is already running: {url}")
            return 0
        listener_pid = _windows_listener_pid(port)
        pid_hint = f" (PID {listener_pid})" if listener_pid is not None else ""
        raise ConfigurationError(
            f"端口 {probe_host}:{port} 已被其他程序占用{pid_hint}，请关闭该程序或修改 web.port"
        )
    if should_open:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(create_web_app(resolved), host=host, port=port, log_level="warning")
    return 0
