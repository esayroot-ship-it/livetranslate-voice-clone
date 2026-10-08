from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass


def subtitle_channels(source_mode: str) -> tuple[str, ...]:
    return {
        "remote": ("remote",),
        "local": ("local",),
        "both": ("remote", "local"),
    }.get(source_mode, ("remote",))


@dataclass(slots=True)
class OverlayMessage:
    channel: str
    source: str
    translation: str
    close: bool = False


class SubtitleOverlay:
    """按配置显示远端、麦克风或双路字幕的本机置顶窗口。"""

    def __init__(
        self,
        websocket_url: str,
        font_size: int = 30,
        *,
        source_mode: str = "remote",
        show_source: bool = True,
        show_translation: bool = True,
        show_channel_labels: bool = True,
        local_translation_enabled: bool = True,
        compact_background: bool = True,
        window_width_percent: int = 72,
        horizontal_padding_px: int = 16,
        vertical_padding_px: int = 8,
        source_font_size_px: int = 16,
        source_font_color: str = "#c7d2cc",
        channel_label_color: str = "#ddef7e",
        font_color: str = "#f4f7f5",
        background_color: str = "#101915",
        opacity: float = 0.92,
        position: str = "bottom",
    ) -> None:
        self.websocket_url = websocket_url
        self.allowed_channels = subtitle_channels(source_mode)
        self.messages: queue.Queue[OverlayMessage] = queue.Queue(maxsize=20)
        self.stop_event = threading.Event()
        self.root = tk.Tk()
        self.root.title("实时字幕")
        self.show_source = show_source
        self.show_translation = show_translation
        self.local_translation_enabled = local_translation_enabled
        self.root.configure(bg=background_color)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", max(0.3, min(float(opacity), 1.0)))
        self.root.overrideredirect(True)
        width = min(
            1400,
            max(480, int(self.root.winfo_screenwidth() * window_width_percent / 100)),
        )
        if compact_background:
            height = 105 if len(self.allowed_channels) == 1 else 195
        else:
            height = 150 if len(self.allowed_channels) == 1 else 270
        x = (self.root.winfo_screenwidth() - width) // 2
        y = 70 if position == "top" else self.root.winfo_screenheight() - height - 100
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        self.source_labels: dict[str, tk.Label] = {}
        self.translation_labels: dict[str, tk.Label] = {}
        draggable: list[tk.Misc] = [self.root]
        names = {"remote": "对方 / 系统媒体", "local": "我 / 本地麦克风"}
        waiting = {"remote": "等待系统媒体声音…", "local": "等待麦克风声音…"}
        for index, channel in enumerate(self.allowed_channels):
            frame = tk.Frame(self.root, bg=background_color)
            frame.pack(
                fill="both",
                expand=True,
                padx=horizontal_padding_px,
                pady=(vertical_padding_px if index == 0 else 2, vertical_padding_px),
            )
            draggable.append(frame)
            if show_channel_labels:
                label = tk.Label(
                    frame,
                    text=names[channel],
                    bg=background_color,
                    fg=channel_label_color,
                    font=("Microsoft YaHei UI", 10, "bold"),
                    anchor="w",
                )
                label.pack(fill="x", padx=4)
                draggable.append(label)
            source_label = tk.Label(
                frame,
                text=waiting[channel],
                bg=background_color,
                fg=source_font_color,
                font=("Microsoft YaHei UI", source_font_size_px),
                anchor="center",
            )
            self.source_labels[channel] = source_label
            if show_source:
                source_label.pack(fill="x", padx=4, pady=(2, 1))
                draggable.append(source_label)
            translation_label = tk.Label(
                frame,
                text="翻译结果将在这里显示",
                bg=background_color,
                fg=font_color,
                font=("Microsoft YaHei UI", font_size, "bold"),
                wraplength=width - 60,
                justify="center",
                anchor="center",
            )
            self.translation_labels[channel] = translation_label
            translation_visible = show_translation and (
                channel == "remote" or local_translation_enabled
            )
            if translation_visible:
                translation_label.pack(fill="both", expand=True, padx=4, pady=(1, 4))
                draggable.append(translation_label)

        self.close_button = tk.Button(
            self.root,
            text="×",
            command=self.close,
            bg=background_color,
            fg="#d6ddd9",
            activebackground="#26362e",
            activeforeground="#ffffff",
            borderwidth=0,
            font=("Segoe UI", 16),
            cursor="hand2",
        )
        self.close_button.place(relx=1.0, x=-10, y=7, anchor="ne", width=30, height=30)

        self._drag_x = 0
        self._drag_y = 0
        for widget in draggable:
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<Button-3>", lambda _event: self.close())
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.after(50, self._drain_messages)

    def run(self) -> None:
        threading.Thread(target=self._receive_loop, name="overlay-websocket", daemon=True).start()
        self.root.mainloop()

    def close(self) -> None:
        self.stop_event.set()
        self.root.destroy()

    def _receive_loop(self) -> None:
        from websockets.sync.client import connect

        unavailable_since: float | None = None
        while not self.stop_event.is_set():
            try:
                with connect(self.websocket_url, open_timeout=3, close_timeout=2) as websocket:
                    unavailable_since = None
                    for message in websocket:
                        if self.stop_event.is_set():
                            return
                        self._accept_payload(json.loads(message))
            except (OSError, TimeoutError, ValueError):
                unavailable_since = unavailable_since or time.monotonic()
                if time.monotonic() - unavailable_since > 15:
                    self.messages.put(OverlayMessage("remote", "", "本地翻译服务已断开"))
                    return
                self.stop_event.wait(1)

    def _accept_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") == "snapshot":
            subtitles = payload.get("subtitles", {})
            if isinstance(subtitles, dict):
                for channel in self.allowed_channels:
                    item = subtitles.get(channel)
                    if isinstance(item, dict):
                        self._accept_payload(item)
            return
        if payload.get("type") == "overlay_command" and payload.get("command") == "close":
            self.messages.put(OverlayMessage("remote", "", "", close=True))
            return
        channel = str(payload.get("channel", ""))
        if payload.get("type") != "subtitle" or channel not in self.allowed_channels:
            return
        source = str(payload.get("source_confirmed", "")) + str(payload.get("source_stash", ""))
        translation = str(payload.get("translation_confirmed", "")) + str(
            payload.get("translation_stash", "")
        )
        if not source and not translation:
            return
        if self.messages.full():
            with contextlib.suppress(queue.Empty):
                self.messages.get_nowait()
        self.messages.put_nowait(OverlayMessage(channel, source, translation))

    def _drain_messages(self) -> None:
        try:
            while True:
                message = self.messages.get_nowait()
                if message.close:
                    self.close()
                    return
                if message.source and self.show_source:
                    self.source_labels[message.channel].configure(text=message.source)
                if self.show_translation and (
                    message.channel == "remote" or self.local_translation_enabled
                ):
                    self.translation_labels[message.channel].configure(
                        text=message.translation or "正在翻译…"
                    )
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(50, self._drain_messages)

    def _drag_start(self, event: tk.Event[tk.Misc]) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_move(self, event: tk.Event[tk.Misc]) -> None:
        self.root.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")


def run_overlay(
    host: str,
    port: int,
    font_size: int,
    *,
    source_mode: str = "remote",
    show_source: bool = True,
    show_translation: bool = True,
    show_channel_labels: bool = True,
    local_translation_enabled: bool = True,
    compact_background: bool = True,
    window_width_percent: int = 72,
    horizontal_padding_px: int = 16,
    vertical_padding_px: int = 8,
    source_font_size_px: int = 16,
    source_font_color: str = "#c7d2cc",
    channel_label_color: str = "#ddef7e",
    font_color: str = "#f4f7f5",
    background_color: str = "#101915",
    opacity: float = 0.92,
    position: str = "bottom",
) -> int:
    SubtitleOverlay(
        f"ws://{host}:{port}/ws",
        font_size,
        source_mode=source_mode,
        show_source=show_source,
        show_translation=show_translation,
        show_channel_labels=show_channel_labels,
        local_translation_enabled=local_translation_enabled,
        compact_background=compact_background,
        window_width_percent=window_width_percent,
        horizontal_padding_px=horizontal_padding_px,
        vertical_padding_px=vertical_padding_px,
        source_font_size_px=source_font_size_px,
        source_font_color=source_font_color,
        channel_label_color=channel_label_color,
        font_color=font_color,
        background_color=background_color,
        opacity=opacity,
        position=position,
    ).run()
    return 0
