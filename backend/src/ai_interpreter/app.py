from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audio import DeviceManager
from .config import ConfigurationError, load_settings
from .controller import InterpreterController
from .logging_setup import configure_logging
from .overlay import run_overlay
from .web_server import run_web_server


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI 会议同声传译 V1")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/settings.local.yaml"),
        help="独立 YAML 配置文件路径",
    )
    parser.add_argument("--check", action="store_true", help="检查配置和音频设备后退出")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备后退出")
    parser.add_argument("--no-browser", action="store_true", help="启动本地服务但不自动打开浏览器")
    parser.add_argument("--overlay", action="store_true", help="只启动远端中文置顶字幕窗口")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    args = _parser().parse_args(argv)
    if args.overlay:
        settings = load_settings(args.config, require_runtime_secrets=False)
        return run_overlay(settings.web.host, settings.web.port, settings.subtitle.font_size_px)
    if args.list_devices:
        try:
            devices = DeviceManager().list_devices()
        except BaseException as exc:
            print(f"设备枚举失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                [
                    {
                        "index": d.index,
                        "name": d.name,
                        "host_api": d.host_api,
                        "inputs": d.max_input_channels,
                        "outputs": d.max_output_channels,
                        "sample_rate": d.default_sample_rate,
                        "loopback": d.is_loopback,
                    }
                    for d in devices
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    try:
        if args.check:
            settings = load_settings(args.config)
            configure_logging(settings.logging)
            resolved = InterpreterController(settings).validate_devices()
            print(json.dumps({"config": "ok", "devices": resolved}, ensure_ascii=False, indent=2))
            return 0
        return run_web_server(args.config, no_browser=args.no_browser)
    except ConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except BaseException as exc:
        print(f"启动失败：{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
