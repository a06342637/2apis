"""只下载官方公共 HTTPS CDN，禁用重定向，不携带账号或 API Key 请求头。"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from anyio import CancelScope

from ..provider.base import UpstreamProtocolViolation, ensure_public_host
from ..security.private_files import secure_file

MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "video/mp4", "video/webm"}


class MediaDownloader:
    def __init__(self, hosts: tuple[str, ...], max_bytes: int = 64 * 1024 * 1024, client=None):
        self.hosts = hosts
        self.max_bytes = max_bytes
        self.client = client

    async def validate(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname in self.hosts
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise UpstreamProtocolViolation("Miora 媒体地址不在官方 HTTPS CDN 白名单")
        await ensure_public_host(parsed.hostname)

    async def download(
        self, url: str, target: Path, kind: str, *, max_bytes: int | None = None
    ) -> dict:
        await self.validate(url)
        client = self.client or httpx.AsyncClient(
            follow_redirects=False, trust_env=False, timeout=120
        )
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        handle = None
        try:
            stream = client.stream("GET", url, headers={"Accept-Encoding": "identity"})
            async with stream as response:
                if response.is_redirect:
                    raise UpstreamProtocolViolation("Miora 媒体下载不允许重定向")
                response.raise_for_status()
                if (
                    response.headers.get("content-encoding", "identity").lower().strip()
                    != "identity"
                ):
                    raise UpstreamProtocolViolation(
                        "Miora media download requires identity encoding"
                    )
                mime = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                if mime not in MEDIA_TYPES or not mime.startswith(kind + "/"):
                    raise UpstreamProtocolViolation("Miora 媒体响应类型不符合请求")
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                handle = os.fdopen(fd, "wb")
                size = 0
                # 编码已限定 identity，HTTPX 不会膨胀解压；兼容已缓存的测试响应。
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    size += len(chunk)
                    limit = self.max_bytes if max_bytes is None else min(self.max_bytes, max_bytes)
                    if size > limit:
                        raise UpstreamProtocolViolation("Miora 媒体文件超过大小限制")
                    await asyncio.to_thread(handle.write, chunk)
                if not size:
                    raise UpstreamProtocolViolation("Miora 媒体文件为空")
                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())
                await asyncio.to_thread(handle.close)
                handle = None
                secure_file(temp)
                os.replace(temp, target)
                return {"mime_type": mime, "bytes": size}
        finally:
            with CancelScope(shield=True):
                if handle is not None:
                    await asyncio.to_thread(handle.close)
                temp.unlink(missing_ok=True)
                if self.client is None:
                    await client.aclose()
