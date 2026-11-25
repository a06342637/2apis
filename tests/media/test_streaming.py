import asyncio
import base64
import io
import json
import tracemalloc
from pathlib import Path

import pytest
from anyio import CancelScope, create_task_group, sleep

from src.media import streaming
from src.media.streaming import MediaJSONStream, encode_media_json
from src.provider.base import UpstreamProtocolViolation


def descriptor(paths, *, maximum=64 * 1024 * 1024, slots=None, keepalive=0):
    return MediaJSONStream(
        created=123,
        paths=paths,
        maximum=maximum,
        slots=slots if slots is not None else asyncio.Semaphore(1),
        keepalive=keepalive,
    )


async def collect(frames):
    return b"".join([frame async for frame in frames])


async def test_multiple_images_form_standard_json_and_release_slot(tmp_path):
    blobs = [b"x", b"hello", bytes(range(256)) * 400]
    paths = []
    for index, blob in enumerate(blobs):
        path = tmp_path / str(index)
        path.write_bytes(blob)
        paths.append(path)
    payload = descriptor(paths)
    result = json.loads(await collect(encode_media_json(payload)))
    assert result["created"] == 123
    assert [base64.b64decode(item["b64_json"]) for item in result["data"]] == blobs
    assert not payload.slots.locked()


async def test_short_reads_keep_three_byte_boundary(monkeypatch, tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"abcdefghijk")

    class ShortReads(io.BytesIO):
        def read(self, size):
            assert size == streaming.RAW_CHUNK_BYTES
            return super().read(1)

    handle = ShortReads(path.read_bytes())
    monkeypatch.setattr(Path, "open", lambda *_args: handle)
    result = json.loads(await collect(descriptor([path]).frames()))
    assert base64.b64decode(result["data"][0]["b64_json"]) == b"abcdefghijk"
    assert handle.closed


async def test_large_sparse_image_has_bounded_frames_and_memory(tmp_path):
    path = tmp_path / "large"
    raw_size = 32 * 1024 * 1024
    with path.open("wb") as handle:
        handle.truncate(raw_size)
    await asyncio.to_thread(lambda: None)
    total = 0
    largest = 0
    tracemalloc.start()
    try:
        async for frame in descriptor([path]).frames():
            total += len(frame)
            largest = max(largest, len(frame))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert total > raw_size * 4 // 3
    assert largest <= 65536
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "symlink"])
async def test_unavailable_files_fail_before_json_and_release_slot(tmp_path, monkeypatch, kind):
    path = tmp_path / "file"
    if kind == "directory":
        path.mkdir()
    elif kind == "empty":
        path.touch()
    elif kind == "symlink":
        path.write_bytes(b"image")
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == path)
    payload = descriptor([path])
    with pytest.raises(UpstreamProtocolViolation, match="unavailable"):
        await collect(payload.frames())
    assert not payload.slots.locked()


async def test_aggregate_and_growing_files_cannot_exceed_budget(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.write_bytes(b"abc")
    two.write_bytes(b"def")
    payload = descriptor([one, two], maximum=5)
    with pytest.raises(UpstreamProtocolViolation, match="aggregate"):
        await collect(payload.frames())
    assert not payload.slots.locked()

    payload = descriptor([one], maximum=5)
    frames = payload.frames()
    assert await anext(frames) == b'{"created":123,"data":['
    assert await anext(frames) == b'{"b64_json":"'
    one.write_bytes(b"abcdef")
    with pytest.raises(UpstreamProtocolViolation, match="aggregate"):
        await anext(frames)
    assert not payload.slots.locked()


async def test_waiting_for_io_emits_only_json_whitespace_and_cleans_up(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"image")
    slots = asyncio.Semaphore(0)
    frames = descriptor([path], slots=slots, keepalive=0.001).frames()
    assert await anext(frames) == b" "
    await frames.aclose()
    assert slots.locked()
    assert not slots._waiters
    slots.release()
    result = json.loads(await collect(descriptor([path], slots=slots, keepalive=0.001).frames()))
    assert base64.b64decode(result["data"][0]["b64_json"]) == b"image"
    assert not slots.locked()


async def test_cancel_while_waiting_does_not_release_unacquired_slot(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"image")
    slots = asyncio.Semaphore(0)
    task = asyncio.create_task(collect(descriptor([path], slots=slots).frames()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.locked()
    assert not slots._waiters


async def test_cancel_after_acquisition_returns_raced_slot(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"image")

    class RacingSemaphore(asyncio.Semaphore):
        async def acquire(self):
            await super().acquire()
            consumer.cancel()
            return True

    slots = RacingSemaphore(1)
    consumer = asyncio.create_task(collect(descriptor([path], slots=slots).frames()))
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert not slots.locked()


async def test_acquire_failure_does_not_release_slot(tmp_path):
    class FailedSemaphore:
        async def acquire(self):
            raise RuntimeError("semaphore failed")

        def release(self):
            pytest.fail("unacquired slot must not be released")

    with pytest.raises(RuntimeError, match="semaphore failed"):
        await collect(descriptor([tmp_path / "unused"], slots=FailedSemaphore()).frames())


async def test_asgi_cancel_closes_open_file_and_releases_io(monkeypatch, tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"abcdef")
    opened = []
    real_open = Path.open

    def track_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", track_open)
    slots = asyncio.Semaphore(1)
    with CancelScope() as scope:
        frames = encode_media_json(descriptor([path], slots=slots))
        assert await anext(frames) == b'{"created":123,"data":['
        assert await anext(frames) == b'{"b64_json":"'
        assert await anext(frames) == b"YWJjZGVm"
        assert slots.locked()
        scope.cancel()
        await frames.aclose()
    assert opened[0].closed
    assert not slots.locked()
    assert json.loads(await collect(descriptor([path], slots=slots).frames()))["data"]


async def test_plain_small_response_keeps_unicode_json():
    assert await collect(encode_media_json({"error": "图片不可用"})) == (
        '{"error":"图片不可用"}'.encode()
    )


async def test_download_and_encoding_share_slots_without_blocking_text(media_env):
    env = media_env
    env.manager.io_slots = asyncio.Semaphore(1)
    first = await env.manager.submit(
        env.principal,
        {"kind": "image", "model": "pretty-picture", "prompt": "first", "options": {}},
    )
    first = await env.manager.wait(first, env.principal)
    second = await env.manager.submit(
        env.principal,
        {"kind": "image", "model": "pretty-picture", "prompt": "second", "options": {}},
    )
    frames = (await env.manager.image_response(first, "b64_json")).frames()
    await anext(frames)
    pending = asyncio.create_task(env.manager.refresh(second, env.principal))
    text_completed = asyncio.Event()

    async def text_work():
        await sleep(0)
        text_completed.set()

    async with create_task_group() as group:
        group.start_soon(text_work)
        await text_completed.wait()
        assert not pending.done()
        assert len([call for call in env.state.calls if call[0] == "download"]) == 1
        await frames.aclose()
        assert (await pending)["status"] == "completed"
    assert not env.manager.io_slots.locked()
    assert len([call for call in env.state.calls if call[0] == "download"]) == 2
