from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_interpreter.audio import DeviceResolutionError

from .config import TestConfigurationError, load_test_settings
from .engine import TestEngineError, VirtualMicTestEngine
from .server import run_server, stop_running_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="虚拟麦克风测试台")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("virtual_mic_test/config/settings.local.yaml"),
    )
    parser.add_argument("--check", action="store_true", help="校验云端与三个音频设备")
    parser.add_argument("--list-devices", action="store_true", help="列出本机音频设备")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--stop", action="store_true", help="关闭正在运行的测试台")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        load_test_settings(args.config)
        if args.stop:
            return stop_running_server(args.config)
        engine = VirtualMicTestEngine(args.config)
        if args.list_devices:
            print(json.dumps(engine.list_devices(), ensure_ascii=True, indent=2))
            return 0
        if args.check:
            print(json.dumps(engine.validate(), ensure_ascii=True, indent=2))
            return 0
        return run_server(args.config, no_browser=args.no_browser)
    except (TestConfigurationError, TestEngineError, DeviceResolutionError) as exc:
        print(f"配置/设备错误：{exc}", file=sys.stderr)
        return 2
    except BaseException as exc:
        print(f"启动失败：{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
