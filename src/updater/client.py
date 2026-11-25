"""非 root 应用通过 Unix socket 调用固定主机动作，不挂载 Docker socket。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx


class UpdateClient:
    def __init__(self, socket_path: str, secret_file: str):
        self.socket_path = socket_path
        self.secret_file = secret_file

    @property
    def enabled(self) -> bool:
        return bool(self.socket_path and self.secret_file)

    async def call(self, action: str, payload: dict | None = None) -> dict:
        if not self.enabled:
            return {"enabled": False}
        try:
            secret = await asyncio.to_thread(Path(self.secret_file).read_text)
            transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
            async with httpx.AsyncClient(transport=transport, timeout=45) as client:
                response = await client.post("http://localhost/action", json={
                    "secret": secret.strip(), "action": action, "payload": payload or {},
                })
                response.raise_for_status()
                result = response.json()
            if not result["ok"]:
                raise ValueError(result["error"])
            return result["result"]
        except (OSError, httpx.HTTPError, KeyError) as error:
            raise ValueError("更新主机助手不可用，请检查安装与 socket/密钥挂载") from error
