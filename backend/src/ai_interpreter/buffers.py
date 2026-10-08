from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Generic, TypeVar

T = TypeVar("T")


class DropOldestQueue(Generic[T]):
    """固定容量队列；满时丢最旧项，音频生产者永不阻塞。"""

    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._queue: queue.Queue[T] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self.dropped = 0

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    def qsize(self) -> int:
        return self._queue.qsize()

    def put_latest(self, item: T) -> bool:
        dropped = False
        with self._lock:
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                    dropped = True
                    self.dropped += 1
                except queue.Empty:
                    pass
            self._queue.put_nowait(item)
        return dropped

    def get(self, timeout: float | None = None) -> T:
        return self._queue.get(timeout=timeout)

    def clear(self) -> int:
        count = 0
        with self._lock:
            while True:
                try:
                    self._queue.get_nowait()
                    count += 1
                except queue.Empty:
                    break
        return count


class PcmRingBuffer:
    """线程安全字节 RingBuffer；满时丢最旧字节，欠载时补静音。"""

    def __init__(self, capacity_bytes: int) -> None:
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        self.capacity_bytes = capacity_bytes
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()
        self.dropped_bytes = 0
        self.underrun_bytes = 0

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        if len(data) % 2:
            raise ValueError("PCM16 data length must be even")
        with self._lock:
            if len(data) >= self.capacity_bytes:
                dropped = self._size + len(data) - self.capacity_bytes
                self._chunks.clear()
                kept = data[-self.capacity_bytes :]
                self._chunks.append(kept)
                self._size = len(kept)
                self.dropped_bytes += dropped
                return dropped
            needed = self._size + len(data) - self.capacity_bytes
            dropped = 0
            while needed > 0 and self._chunks:
                head = self._chunks.popleft()
                if len(head) <= needed:
                    needed -= len(head)
                    dropped += len(head)
                    self._size -= len(head)
                else:
                    self._chunks.appendleft(head[needed:])
                    dropped += needed
                    self._size -= needed
                    needed = 0
            self._chunks.append(data)
            self._size += len(data)
            self.dropped_bytes += dropped
            return dropped

    def read_or_silence(self, size: int) -> tuple[bytes, int]:
        if size <= 0 or size % 2:
            raise ValueError("size must be a positive even number")
        output = bytearray()
        with self._lock:
            while len(output) < size and self._chunks:
                head = self._chunks.popleft()
                needed = size - len(output)
                if len(head) <= needed:
                    output.extend(head)
                    self._size -= len(head)
                else:
                    output.extend(head[:needed])
                    self._chunks.appendleft(head[needed:])
                    self._size -= needed
            missing = size - len(output)
            if missing:
                output.extend(b"\x00" * missing)
                self.underrun_bytes += missing
        return bytes(output), missing

    def clear(self) -> int:
        with self._lock:
            size = self._size
            self._chunks.clear()
            self._size = 0
            return size

