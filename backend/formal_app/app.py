from __future__ import annotations

import argparse
import json
import socket
import threading
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import uvicorn

from .config import ConfigStore, FormalConfigurationError
from .server import create_app

BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE / "config" / "settings.yaml"
DEFAULT_SECRET = BASE / "config" / "settings.local.yaml"


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _healthy(host: str, port: int) -> bool:
    try:
        with urlopen(f"http://{host}:{port}/api/health", timeout=1.2) as response:  # noqa: S310
            value = json.load(response)
        return value.get("app") == "formal_interpreter"
    except (OSError, HTTPError, URLError, ValueError):
        return False


def run(*, no_browser: bool = False) -> int:
    store = ConfigStore(DEFAULT_CONFIG, DEFAULT_SECRET)
    raw = store.raw()
    host = "127.0.0.1" if raw["web"]["host"] == "localhost" else raw["web"]["host"]
    port = int(raw["web"]["port"])
    url = f"http://{host}:{port}"
    if _listening(host, port):
        if _healthy(host, port):
            if not no_browser:
                webbrowser.open(url)
            print(f"正式版已经运行：{url}")
            return 0
        raise FormalConfigurationError(f"端口 {host}:{port} 已被其他程序占用")
    holder: dict[str, uvicorn.Server] = {}

    def shutdown() -> None:
        if "server" in holder:
            holder["server"].should_exit = True

    if raw["web"].get("open_browser", True) and not no_browser:
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    config = uvicorn.Config(
        create_app(store, shutdown_callback=shutdown), host=host, port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    holder["server"] = server
    print(f"AI 会议同声传译正式版：{url}")
    server.run()
    return 0


def stop() -> int:
    store = ConfigStore(DEFAULT_CONFIG, DEFAULT_SECRET)
    web = store.raw()["web"]
    host = "127.0.0.1" if web["host"] == "localhost" else web["host"]
    request = Request(
        f"http://{host}:{web['port']}/api/shutdown",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            result = json.load(response)
    except (OSError, HTTPError, URLError, ValueError) as exc:
        raise FormalConfigurationError(
            f"无法关闭程序，服务可能未启动：{type(exc).__name__}"
        ) from exc
    print(result.get("message", "关闭请求已发送"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 会议同声传译正式版")
    parser.add_argument("command", nargs="?", choices=["start", "stop"], default="start")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        return stop() if args.command == "stop" else run(no_browser=args.no_browser)
    except FormalConfigurationError as exc:
        print(f"启动失败：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
