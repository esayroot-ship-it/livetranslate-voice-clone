from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse

from ai_interpreter.audio import DeviceResolutionError

from .config import ConfigStore, FormalConfigurationError
from .runtime import EventHub, FormalRuntime
from .testing import FormalTestLab
from .voice_manager import VoiceManager

STATIC = Path(__file__).with_name("web")
PAGES = {
    "": "index.html",
    "index.html": "index.html",
    "config.html": "config.html",
    "audio.html": "audio.html",
    "subtitles.html": "subtitles.html",
    "voices.html": "voices.html",
    "test.html": "test.html",
    "status.html": "status.html",
}
ASSETS = {
    "app.css",
    "dashboard.css",
    "audio.css",
    "common.js",
    "dashboard.js",
    "config.js",
    "config.css",
    "audio.js",
    "subtitles.js",
    "subtitles.css",
    "voices.js",
    "test.js",
    "test.css",
    "status.js",
}


def create_app(
    store: ConfigStore,
    *,
    shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    hub = EventHub()
    runtime = FormalRuntime(store, hub)
    voices = VoiceManager(store)
    lab = FormalTestLab(store, hub.publish)
    raw = store.raw()
    port = int(raw["web"]["port"])
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.bind(asyncio.get_running_loop())
        yield
        if lab.engine.running:
            await asyncio.to_thread(lab.engine.stop_cloud_test)
        if lab.subtitle.running:
            await asyncio.to_thread(lab.subtitle.stop)
        await runtime.shutdown()

    app = FastAPI(title="AI 会议同声传译正式版", docs_url=None, redoc_url=None, lifespan=lifespan)
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
    @app.get("/{page_name}.html")
    async def page(page_name: str = "") -> FileResponse:
        key = f"{page_name}.html" if page_name else ""
        if key not in PAGES:
            raise HTTPException(status_code=404)
        return FileResponse(STATIC / PAGES[key], headers={"Cache-Control": "no-store"})

    @app.get("/assets/{name}")
    async def asset(name: str) -> FileResponse:
        if name not in ASSETS:
            raise HTTPException(status_code=404)
        media = "text/css" if name.endswith(".css") else "application/javascript"
        return FileResponse(STATIC / name, media_type=media, headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "app": "formal_interpreter",
            "version": "1.0.0",
            "running": runtime.running,
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return store.public()

    @app.put("/api/config")
    async def put_config(body: dict[str, Any]) -> dict[str, Any]:
        try:
            allow_pending_restart = body.pop("allow_pending_restart", False) is True
            active_components = []
            if runtime.running:
                active_components.append("正式程序")
            if lab.engine.running:
                active_components.append("克隆译音测试")
            if lab.subtitle.running:
                active_components.append("字幕测试")
            if active_components and not allow_pending_restart:
                raise HTTPException(
                    status_code=409,
                    detail=f"请先停止{'、'.join(active_components)}再修改此配置",
                )
            result = store.save(body)
            if not active_components:
                try:
                    lab.refresh()
                except RuntimeError:
                    active_components.append("测试")
            result["save_status"] = {
                "restart_required": bool(active_components),
                "active_components": active_components,
            }
            return result
        except FormalConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/config/validate")
    async def validate_config() -> dict[str, Any]:
        try:
            store.validate(for_start=False)
            return {"ok": True, "message": "配置结构与官方协议约束检查通过"}
        except FormalConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/devices")
    async def devices() -> dict[str, Any]:
        try:
            return {"devices": await asyncio.to_thread(runtime.list_devices)}
        except BaseException as exc:
            raise HTTPException(
                status_code=503, detail=f"设备枚举失败：{type(exc).__name__}: {str(exc)[:160]}"
            ) from exc

    @app.post("/api/devices/validate")
    async def validate_devices() -> dict[str, Any]:
        try:
            return {"ok": True, "resolved": await asyncio.to_thread(runtime.validate_devices)}
        except (FormalConfigurationError, DeviceResolutionError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/devices/probe-loopback")
    async def probe_loopback(body: dict[str, Any]) -> dict[str, Any]:
        try:
            duration = max(500, min(int(body.get("duration_ms", 2500)), 5000))
            return await asyncio.to_thread(
                runtime.probe_loopback,
                str(body.get("name", "")),
                bool(body.get("use_default", True)),
                duration,
            )
        except BaseException as exc:
            raise HTTPException(
                status_code=422, detail=f"回环检测失败：{type(exc).__name__}: {str(exc)[:160]}"
            ) from exc

    @app.post("/api/devices/probe-microphone")
    async def probe_microphone(body: dict[str, Any]) -> dict[str, Any]:
        if runtime.running or lab.engine.running or lab.subtitle.running:
            raise HTTPException(status_code=409, detail="请先停止正式程序和测试，避免占用麦克风")
        try:
            duration = max(3000, min(int(body.get("duration_ms", 4000)), 6000))
            return await asyncio.to_thread(
                runtime.probe_microphone,
                str(body.get("name", "")),
                bool(body.get("use_default", True)),
                duration,
            )
        except BaseException as exc:
            raise HTTPException(
                status_code=422,
                detail=f"麦克风校准失败：{type(exc).__name__}: {str(exc)[:160]}",
            ) from exc

    @app.post("/api/runtime/start")
    async def start_runtime() -> dict[str, Any]:
        if lab.engine.running or lab.subtitle.running:
            raise HTTPException(status_code=409, detail="请先停止测试台")
        try:
            return await runtime.start()
        except BaseException as exc:
            raise HTTPException(
                status_code=422, detail=f"启动失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    @app.post("/api/runtime/stop")
    async def stop_runtime() -> dict[str, Any]:
        return await runtime.stop()

    @app.get("/api/runtime/status")
    async def runtime_status() -> dict[str, Any]:
        return runtime.snapshot()

    @app.post("/api/runtime/microphone-transmit")
    async def microphone_transmit(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return runtime.set_microphone_transmit(body.get("active") is True)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/overlay/open")
    async def open_overlay() -> dict[str, Any]:
        return runtime.open_overlay()

    @app.post("/api/overlay/close")
    async def close_overlay() -> dict[str, Any]:
        return runtime.close_overlay()

    @app.get("/api/voices/prompt")
    async def voice_prompt() -> dict[str, str]:
        return {"prompt": voices.prompt()}

    @app.get("/api/voices")
    async def list_voices(page_index: int = 0, page_size: int = 10) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                voices.list, max(0, page_index), max(1, min(page_size, 100))
            )
        except BaseException as exc:
            raise HTTPException(
                status_code=422, detail=f"查询音色失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    @app.post("/api/voices")
    async def create_voice(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                voices.create,
                data_url=str(body.get("data_url", "")),
                preferred_name=str(body.get("preferred_name", "")),
                transcript=str(body.get("transcript", "")),
                language=str(body.get("language", "")),
            )
        except BaseException as exc:
            raise HTTPException(
                status_code=422, detail=f"创建音色失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    @app.delete("/api/voices/{voice}")
    async def delete_voice(voice: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(voices.delete, voice)
        except BaseException as exc:
            raise HTTPException(
                status_code=422, detail=f"删除音色失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    @app.post("/api/voices/{voice}/select")
    async def select_voice(voice: str) -> dict[str, Any]:
        if runtime.running or lab.engine.running or lab.subtitle.running:
            raise HTTPException(status_code=409, detail="请先停止程序和测试")
        try:
            result = voices.select(voice)
            lab.refresh()
            return result
        except BaseException as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/test/validate")
    async def test_validate() -> dict[str, Any]:
        try:
            lab.refresh()
            return await asyncio.to_thread(lab.engine.validate)
        except BaseException as exc:
            raise HTTPException(
                status_code=422, detail=f"测试环境校验失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    @app.post("/api/test/route")
    async def test_route() -> dict[str, Any]:
        if runtime.running or lab.subtitle.running:
            raise HTTPException(status_code=409, detail="请先停止正式程序，避免占用音频设备")
        try:
            lab.refresh()
            return await asyncio.to_thread(lab.engine.run_route_test)
        except BaseException as exc:
            raise HTTPException(
                status_code=422, detail=f"路由测试失败：{type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    @app.post("/api/test/cloud/start")
    async def test_cloud_start(body: dict[str, Any]) -> dict[str, Any]:
        if runtime.running or lab.subtitle.running:
            raise HTTPException(status_code=409, detail="请先停止正式程序，避免占用麦克风")
        try:
            lab.refresh()
            duration = body.get("duration_seconds")
            return await asyncio.to_thread(
                lab.engine.start_cloud_test, int(duration) if duration else None
            )
        except BaseException as exc:
            raise HTTPException(
                status_code=422,
                detail=f"实时同传测试启动失败：{type(exc).__name__}: {str(exc)[:180]}",
            ) from exc

    @app.post("/api/test/cloud/stop")
    async def test_cloud_stop() -> dict[str, Any]:
        return await asyncio.to_thread(lab.engine.stop_cloud_test)

    @app.post("/api/test/subtitle/validate")
    async def subtitle_validate() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(lab.subtitle.validate)
        except BaseException as exc:
            raise HTTPException(
                status_code=422,
                detail=(f"字幕测试环境校验失败：{type(exc).__name__}: {str(exc)[:180]}"),
            ) from exc

    @app.post("/api/test/subtitle/start")
    async def subtitle_start(body: dict[str, Any]) -> dict[str, Any]:
        if runtime.running or lab.engine.running:
            raise HTTPException(
                status_code=409,
                detail="请先停止正式程序和克隆译音测试，避免占用音频设备",
            )
        try:
            duration = int(body.get("duration_seconds", 60))
            return await asyncio.to_thread(lab.subtitle.start, duration)
        except BaseException as exc:
            raise HTTPException(
                status_code=422,
                detail=f"字幕效果测试启动失败：{type(exc).__name__}: {str(exc)[:180]}",
            ) from exc

    @app.post("/api/test/subtitle/stop")
    async def subtitle_stop() -> dict[str, Any]:
        return await asyncio.to_thread(lab.subtitle.stop)

    @app.get("/api/test/subtitle/status")
    async def subtitle_status() -> dict[str, Any]:
        return lab.subtitle.snapshot()

    @app.get("/api/test/status")
    async def test_status() -> dict[str, Any]:
        return lab.engine.snapshot()

    @app.get("/api/test/recordings")
    async def recordings() -> dict[str, Any]:
        return {"recordings": lab.engine.recordings()}

    @app.get("/api/test/recordings/{name}")
    async def recording(name: str) -> FileResponse:
        try:
            path = lab.resolve_recording(name)
        except (ValueError, FileNotFoundError):
            raise HTTPException(status_code=404) from None
        return FileResponse(path, media_type="audio/wav", filename=name)

    @app.delete("/api/test/recordings/{name}")
    async def delete_recording(name: str) -> dict[str, Any]:
        if lab.engine.running:
            raise HTTPException(status_code=409, detail="测试运行时不能删除录音")
        try:
            lab.delete_recording(name)
            return {"ok": True, "deleted": name}
        except (ValueError, FileNotFoundError):
            raise HTTPException(status_code=404) from None

    @app.delete("/api/test/recordings")
    async def delete_recordings() -> dict[str, Any]:
        if lab.engine.running:
            raise HTTPException(status_code=409, detail="测试运行时不能删除录音")
        return {"ok": True, "deleted_count": lab.delete_all()}

    @app.post("/api/shutdown")
    async def shutdown() -> dict[str, Any]:
        if lab.engine.running:
            await asyncio.to_thread(lab.engine.stop_cloud_test)
        if lab.subtitle.running:
            await asyncio.to_thread(lab.subtitle.stop)
        await runtime.shutdown()
        if shutdown_callback:
            asyncio.get_running_loop().call_later(0.2, shutdown_callback)
        return {"ok": True, "message": "正式版服务正在关闭"}

    @app.websocket("/ws")
    async def events(socket: WebSocket) -> None:
        await socket.accept()
        queue = hub.subscribe()
        try:
            await socket.send_json(runtime.snapshot())
            while True:
                payload = await queue.get()
                await socket.send_json(payload)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            hub.unsubscribe(queue)

    return app
