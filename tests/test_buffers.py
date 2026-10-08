from ai_interpreter.buffers import DropOldestQueue, PcmRingBuffer


def test_drop_oldest_queue_is_bounded() -> None:
    q = DropOldestQueue[int](2)
    assert q.put_latest(1) is False
    assert q.put_latest(2) is False
    assert q.put_latest(3) is True
    assert q.qsize() == 2
    assert q.dropped == 1
    assert q.get(0) == 2
    assert q.get(0) == 3


def test_pcm_ring_buffer_drops_oldest_and_adds_silence() -> None:
    buffer = PcmRingBuffer(8)
    assert buffer.write(b"\x01\x00\x02\x00\x03\x00") == 0
    assert buffer.write(b"\x04\x00\x05\x00") == 2
    data, missing = buffer.read_or_silence(8)
    assert data == b"\x02\x00\x03\x00\x04\x00\x05\x00"
    assert missing == 0
    data, missing = buffer.read_or_silence(4)
    assert data == b"\x00" * 4
    assert missing == 4


def test_pcm_ring_buffer_rejects_odd_pcm() -> None:
    buffer = PcmRingBuffer(8)
    try:
        buffer.write(b"x")
    except ValueError as exc:
        assert "PCM16" in str(exc)
    else:
        raise AssertionError("odd PCM must be rejected")

