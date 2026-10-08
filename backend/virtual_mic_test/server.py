from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest
from urllib.request import urlopen

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ai_interpreter.audio import DeviceResolutionError
from ai_interpreter.config import ConfigurationError, load_settings
from ai_interpreter.web_server import _port_is_listening, _windows_listener_pid

from .config import TestConfigurationError, load_test_settings
from .engine import TestEngineError, VirtualMicTestEngine

STATIC_DIR = Path(__file__).with_name("web")


class DeviceUpdate(BaseModel):
    microphone_name: str = ""
    microphone_use_default: bool = True
    virtual_playback_name: str
    virtual_recording_name: str
    signal_threshold_dbfs: float = Field(ge=-90, le=-6)
    cloud_max_duration_seconds: int = Field(ge=5, le=300)


class CloudStart(BaseModel):
    duration_seconds: int | None = Field(default=None, ge=5, le=300)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise HTTPException(status_code=500, detail=f"无法读取测试台配置：{exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="测试台配置顶层必须是对象")
    return raw


def _atomic_write(path: Path, raw: dict[str, Any]) -> None:
    data = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _public_config(path: Path) -> dict[str, Any]:
    settings = load_test_settings(path)
    main_key_configured = False
    workspace_configured = False
    voice_id = ""
    main_error = None
    try:
        main = load_settings(settings.interpreter_config_path)
        main_key_configured = bool(main.aliyun.api_key)
        workspace_configured = bool(main.aliyun.workspace_id)
        voice_id = main.voice.voice_id
    except ConfigurationError as exc:
        main_error = str(exc)
    return {
        "config_path": str(path),
        "interpreter_config_path": str(settings.interpreter_config_path),
        "api_key_configured": main_key_configured,
        "workspace_configured": workspace_configured,
        "voice_id": voice_id,
        "main_config_error": main_error,
        "microphone": {
            "name": settings.audio.microphone.name,
            "use_default": settings.audio.microphone.use_default,
        },
        "virtual_playback": {"name": settings.audio.virtual_playback.name},
        "virtual_recording": {"name": settings.audio.virtual_recording.name},
        "signal_threshold_dbfs": settings.audio.signal_threshold_dbfs,
        "cloud_max_duration_seconds": settings.audio.cloud_max_duration_seconds,
        "output_directory": str(settings.output_directory),
        "web": {"host": settings.web.host, "port": settings.web.port},
    }


def _save_update(path: Path, update: DeviceUpdate) -> dict[str, Any]:
    raw = _read_yaml(path)
    devices = raw.setdefault("devices", {})
    devices.setdefault("microphone", {}).update(
        {
            "host_api": "WASAPI",
            "name": update.microphone_name.strip(),
            "use_default": update.microphone_use_default,
        }
    )
    devices.setdefault("virtual_playback", {}).update(
        {
            "host_api": "WASAPI",
            "name": update.virtual_playback_name.strip(),
            "use_default": False,
        }
    )
    devices.setdefault("virtual_recording", {}).update(
        {
            "host_api": "WASAPI",
            "name": update.virtual_recording_name.strip(),
            "use_default": False,
        }
    )
    raw.setdefault("audio", {}).update(
        {
            "signal_threshold_dbfs": update.signal_threshold_dbfs,
            "cloud_max_duration_seconds": update.cloud_max_duration_seconds,
        }
    )
    _atomic_write(path, raw)
    validation_error = None
    try:
        load_test_settings(path)
    except TestConfigurationError as exc:
        validation_error = str(exc)
    result = _public_config(path)
    result["validation_error"] = validation_error
    return result


def create_app(
    config_path: str | Path,
    *,
    shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    resolved = Path(config_path).resolve()
    settings = load_test_settings(resolved)
    engine = VirtualMicTestEngine(resolved)
    allowed_origins = {
        f"http://127.0.0.1:{settings.web.port}",
        f"http://localhost:{settings.web.port}",
    }

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if engine.running:
            await asyncio.to_thread(engine.stop_cloud_test)

    app = FastAPI(
        title="虚拟麦克风测试台",
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
            return JSONResponse(status_code=403, content={"detail": "拒绝非本机测试台来源"})
        return await call_next(request)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/assets/{name}")
    async def asset(name: str) -> FileResponse:
        if name not in {"app.css", "app.js"}:
            raise HTTPException(status_code=404)
        media_type = "text/css" if name.endswith(".css") else "application/javascript"
        return FileResponse(
            STATIC_DIR / name,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "app": "virtual_mic_test", "running": engine.running}

    @app.post("/api/shutdown")
    async def shutdown() -> dict[str, Any]:
        if engine.running:
            await asyncio.to_thread(engine.stop_cloud_test)
        if shutdown_callback is not None:
            asyncio.get_running_loop().call_later(0.2, shutdown_callback)
        return {"ok": True, "message": "测试台正在关闭"}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        try:
            return _public_config(resolved)
        except TestConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/config")
    async def save_config(update: DeviceUpdate) -> dict[str, Any]:
        if engine.running:
            raise HTTPException(status_code=409, detail="请先停止云端测试")
        if not update.virtual_playback_name.strip() or not update.virtual_recording_name.strip():
            raise HTTPException(status_code=422, detail="虚拟播放和录音设备不能为空")
        return _save_update(resolved, update)

    @app.get("/api/devices")
    async def devices() -> dict[str, Any]:
        try:
            return {"devices": await asyncio.to_thread(engine.list_devices)}
        except BaseException as exc:
            raise HTTPException(
                status_code=503, detail=f"设备枚举失败：{type(exc).__name__}"
            ) from exc

    @app.post("/api/validate")
    async def validate() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(engine.validate)
        except (
            TestEngineError,
            TestConfigurationError,
            ConfigurationError,
            DeviceResolutionError,
        ) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/route-test")
    async def route_test() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(engine.run_route_test)
        except BaseException as exc:
            raise HTTPException(
                status_code=422,
                detail=f"虚拟路由测试失败：{type(exc).__name__}: {str(exc)[:180]}",
            ) from exc

    @app.post("/api/cloud/start")
    async def start_cloud(request: CloudStart) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(engine.start_cloud_test, request.duration_seconds)
        except BaseException as exc:
            raise HTTPException(
                status_code=422,
                detail=f"云端推流测试启动失败：{type(exc).__name__}: {str(exc)[:180]}",
            ) from exc

    @app.post("/api/cloud/stop")
    async def stop_cloud() -> dict[str, Any]:
        return await asyncio.to_thread(engine.stop_cloud_test)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return engine.snapshot()

    @app.get("/api/recordings")
    async def recordings() -> dict[str, Any]:
        return {"recordings": engine.recordings()}

    @app.get("/recordings/{name}")
    async def recording(name: str) -> FileResponse:
        if Path(name).name != name or not name.lower().endswith(".wav"):
            raise HTTPException(status_code=404)
        output_directory = engine.settings().output_directory.resolve()
        path = (output_directory / name).resolve()
        if path.parent != output_directory or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="audio/wav", filename=name)

    return app


def _existing_station(host: str, port: int) -> bool:
    try:
        with urlopen(f"http://{host}:{port}/api/health", timeout=1.5) as response:  # noqa: S310
            payload = json.load(response)
    except (OSError, HTTPError, URLError, TimeoutError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("app") == "virtual_mic_test"


def run_server(config_path: str | Path, *, no_browser: bool = False) -> int:
    import webbrowser

    import uvicorn

    settings = load_test_settings(config_path)
    host = "127.0.0.1" if settings.web.host == "localhost" else settings.web.host
    url = f"http://{host}:{settings.web.port}"
    if _port_is_listening(host, settings.web.port):
        if _existing_station(host, settings.web.port):
            if settings.web.open_browser and not no_browser:
                webbrowser.open(url)
            print(f"Virtual Mic Test is already running: {url}")
            return 0
        pid = _windows_listener_pid(settings.web.port)
        pid_hint = f" (PID {pid})" if pid is not None else ""
        raise TestConfigurationError(
            f"端口 {host}:{settings.web.port} 已被其他程序占用{pid_hint}"
        )
    if settings.web.open_browser and not no_browser:
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    server_holder: dict[str, uvicorn.Server] = {}

    def request_shutdown() -> None:
        server = server_holder.get("server")
        if server is not None:
            server.should_exit = True

    config = uvicorn.Config(
        create_app(config_path, shutdown_callback=request_shutdown),
        host=host,
        port=settings.web.port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server_holder["server"] = server
    server.run()
    return 0


def stop_running_server(config_path: str | Path) -> int:
    settings = load_test_settings(config_path)
    host = "127.0.0.1" if settings.web.host == "localhost" else settings.web.host
    request = URLRequest(
        f"http://{host}:{settings.web.port}/api/shutdown",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=25) as response:  # noqa: S310 - 仅访问本机配置端口
            payload = json.load(response)
    except (OSError, HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise TestConfigurationError(
            f"无法关闭测试台，服务可能未启动：{type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TestConfigurationError("测试台未确认关闭请求")
    print(f"Virtual Mic Test is stopping: http://{host}:{settings.web.port}")
    return 0
