from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .client import (
    AudioSampleError,
    VoiceCloneApiError,
    VoiceCloneClient,
    VoiceCloneConfigurationError,
    VoiceCloneSettings,
    build_create_payload,
    load_recording_prompt,
    load_voice_clone_settings,
    prepare_audio_sample,
)


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-clone",
        description="阿里云百炼 Qwen3.5 LiveTranslate 固定音色管理 CLI",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/voice_clone.yaml"),
        help="独立的声音复刻 YAML 配置文件",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="上传声音样本并创建音色")
    source = create.add_mutually_exclusive_group()
    source.add_argument("--audio", type=Path, help="本地 WAV/MP3/M4A 文件")
    source.add_argument("--audio-url", help="无需鉴权且公网可访问的 HTTPS 音频 URL")
    create.add_argument("--name", help="音色标识：数字、字母、下划线，最多 16 字符")
    create.add_argument(
        "--target-model",
        help="固定为 qwen3.5-livetranslate-flash-realtime",
    )
    create.add_argument("--text", help="与声音样本完全一致的朗读文本")
    create.add_argument("--language", help="声音样本语种，例如 zh 或 en")
    create.add_argument("--yes", action="store_true", help="跳过创建费用确认")
    create.add_argument("--json", dest="command_json", action="store_true", help="JSON 输出")

    listing = commands.add_parser("list", help="分页查询已创建的音色")
    listing.add_argument("--page-index", type=int, help="从 0 开始的页码")
    listing.add_argument("--page-size", type=int, help="每页条数")
    listing.add_argument("--json", dest="command_json", action="store_true", help="JSON 输出")

    delete = commands.add_parser("delete", help="删除指定音色")
    delete.add_argument("voice", help="待删除的完整 voice 值")
    delete.add_argument("--yes", action="store_true", help="跳过删除确认")
    delete.add_argument("--json", dest="command_json", action="store_true", help="JSON 输出")

    check = commands.add_parser("check", help="离线检查配置和本地声音样本")
    check.add_argument("--json", dest="command_json", action="store_true", help="JSON 输出")
    commands.add_parser("prompt", help="显示配置文件中的录音文案")
    return parser


def _confirm(message: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"需要确认：{message}；非交互模式请添加 --yes", file=sys.stderr)
        return False
    answer = input(f"{message} [y/N]: ").strip().casefold()
    return answer in {"y", "yes"}


def _json_output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _wants_json(args: argparse.Namespace, settings: VoiceCloneSettings) -> bool:
    return bool(
        args.json
        or getattr(args, "command_json", False)
        or settings.output_format == "json"
    )


def _show_request_id(settings: VoiceCloneSettings, request_id: str) -> None:
    if settings.show_request_id and request_id:
        print(f"Request ID: {request_id}")


def _load(args: argparse.Namespace, *, require_api_key: bool = True) -> VoiceCloneSettings:
    return load_voice_clone_settings(args.config, require_api_key=require_api_key)


def _create(args: argparse.Namespace) -> int:
    settings = _load(args)
    sample = prepare_audio_sample(
        settings,
        file_path=args.audio,
        audio_url=args.audio_url,
    )
    for warning in sample.warnings:
        print(f"提示：{warning}", file=sys.stderr)
    payload = build_create_payload(
        settings,
        sample,
        preferred_name=args.name,
        target_model=args.target_model,
        transcript=args.text,
        language=args.language,
    )
    if not _confirm("创建音色可能消耗免费额度或产生费用，是否继续", assume_yes=args.yes):
        return 5
    result = VoiceCloneClient(settings).create(payload)
    if _wants_json(args, settings):
        _json_output(asdict(result))
    else:
        print("音色创建成功")
        print(f"Voice: {result.voice}")
        print(f"Target model: {result.target_model}")
        print(f"本次计费创建次数: {result.billed_count}")
        _show_request_id(settings, result.request_id)
    return 0


def _list(args: argparse.Namespace) -> int:
    settings = _load(args)
    page_index = settings.page_index if args.page_index is None else args.page_index
    page_size = settings.page_size if args.page_size is None else args.page_size
    if not 0 <= page_index <= 1_000_000 or not 0 <= page_size <= 1_000_000:
        raise VoiceCloneConfigurationError("page-index/page-size 必须在 0-1000000")
    result = VoiceCloneClient(settings).list(page_index=page_index, page_size=page_size)
    if _wants_json(args, settings):
        _json_output(
            {"voices": [asdict(item) for item in result.voices], "request_id": result.request_id}
        )
        return 0
    if not result.voices:
        print("当前页没有音色。")
    else:
        widths = {
            "voice": max(5, *(len(item.voice) for item in result.voices)),
            "created": max(8, *(len(item.gmt_create) for item in result.voices)),
        }
        print(
            f"{'VOICE':<{widths['voice']}}  "
            f"{'CREATED':<{widths['created']}}  TARGET MODEL"
        )
        print("-" * (widths["voice"] + widths["created"] + 16))
        for item in result.voices:
            print(
                f"{item.voice:<{widths['voice']}}  "
                f"{item.gmt_create:<{widths['created']}}  {item.target_model}"
            )
    _show_request_id(settings, result.request_id)
    return 0


def _delete(args: argparse.Namespace) -> int:
    settings = _load(args)
    if not _confirm(f"确定永久删除音色 {args.voice}", assume_yes=args.yes):
        return 5
    result = VoiceCloneClient(settings).delete(args.voice)
    if _wants_json(args, settings):
        _json_output(asdict(result))
    else:
        print(f"音色已删除：{result.voice}")
        _show_request_id(settings, result.request_id)
    return 0


def _check(args: argparse.Namespace) -> int:
    settings = _load(args)
    sample = prepare_audio_sample(settings)
    result = {
        "config": "ok",
        "endpoint": settings.endpoint,
        "target_model": settings.target_model,
        "audio_source": sample.source,
        "mime_type": sample.mime_type,
        "warnings": list(sample.warnings),
    }
    if _wants_json(args, settings):
        _json_output(result)
    else:
        print("配置与声音样本检查通过。")
        print(f"Endpoint: {settings.endpoint}")
        print(f"Target model: {settings.target_model}")
        print(f"Audio: {sample.source}")
        for warning in sample.warnings:
            print(f"提示：{warning}")
    return 0


def _prompt(args: argparse.Namespace) -> int:
    print(load_recording_prompt(args.config))
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = _parser().parse_args(argv)
    try:
        handlers = {
            "create": _create,
            "list": _list,
            "delete": _delete,
            "check": _check,
            "prompt": _prompt,
        }
        return handlers[args.command](args)
    except VoiceCloneConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except AudioSampleError as exc:
        print(f"声音样本错误：{exc}", file=sys.stderr)
        return 3
    except VoiceCloneApiError as exc:
        details = []
        if exc.status_code is not None:
            details.append(f"HTTP {exc.status_code}")
        if exc.request_id:
            details.append(f"Request ID {exc.request_id}")
        suffix = f" ({', '.join(details)})" if details else ""
        print(f"百炼接口错误：{exc}{suffix}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
