"""按块编码标准图片 JSON，内存不随媒体文件大小增长。"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path

from anyio import CancelScope

from ..provider.base import UpstreamProtocolViolation

RAW_CHUNK_BYTES = 48 * 1024


class MediaJSONStream:
    def __init__(
        self,
        *,
        created: int,
        paths: list[Path],
        maximum: int,
        slots: asyncio.Semaphore,
        keepalive: float,
    ):
        self.created = created
        self.paths = paths
        self.maximum = maximum
        self.slots = slots
        self.keepalive = keepalive

    async def frames(self) -> AsyncIterator[bytes]:
        acquired = False

        async def acquire_slot():
            nonlocal acquired
            await self.slots.acquire()
            acquired = True

        acquire = asyncio.create_task(acquire_slot())
        try:
            # 等 IO 名额时允许输出 JSON 空白；不会在 Base64 字符串中插入空白。
            if self.keepalive > 0:
                while not acquire.done():
                    await asyncio.wait({acquire}, timeout=self.keepalive)
                    if not acquire.done():
                        yield b" "
            await acquire
            total = 0
            for path in self.paths:
                if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
                    raise UpstreamProtocolViolation("cached media file is unavailable")
                total += path.stat().st_size
            if total > self.maximum:
                raise UpstreamProtocolViolation("media response exceeds aggregate size limit")
            yield b'{"created":' + str(self.created).encode() + b',"data":['
            actual_size = 0
            for index, path in enumerate(self.paths):
                if index:
                    yield b","
                yield b'{"b64_json":"'
                handle = path.open("rb")
                try:
                    carry = b""
                    while True:
                        chunk = await asyncio.to_thread(handle.read, RAW_CHUNK_BYTES)
                        if not chunk:
                            break
                        actual_size += len(chunk)
                        if actual_size > self.maximum:
                            raise UpstreamProtocolViolation(
                                "media response exceeds aggregate size limit"
                            )
                        block = carry + chunk
                        end = len(block) // 3 * 3
                        if end:
                            yield base64.b64encode(block[:end])
                        carry = block[end:]
                    if carry:
                        yield base64.b64encode(carry)
                finally:
                    # ASGI disconnect 会反复取消 await；关闭 FD 后才归还 IO 名额。
                    with CancelScope(shield=True):
                        await asyncio.to_thread(handle.close)
                yield b'"}'
            yield b"]}"
        finally:
            with CancelScope(shield=True):
                if not acquire.done():
                    acquire.cancel()
                    await asyncio.gather(acquire, return_exceptions=True)
                if acquired:
                    self.slots.release()


async def encode_media_json(payload) -> AsyncIterator[bytes]:
    if isinstance(payload, MediaJSONStream):
        frames = payload.frames()
        try:
            async for frame in frames:
                yield frame
        finally:
            with CancelScope(shield=True):
                await frames.aclose()
    else:
        # URL/错误/测试替身响应很小，沿用正常 JSON 编码。
        yield json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
