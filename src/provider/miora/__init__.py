"""Miora 多账号渠道；复用共享调度、加密存储与 OpenAI/Anthropic 协议出口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from ...tasks.pacer import paced
from ..base import ErrKind, Event, Model, Quota
from .client import MioraClient
from .credential import MioraCredential
from .events import classify_status


@dataclass(slots=True)
class MioraProvider:
    client: MioraClient = field(default_factory=MioraClient)
    pacer: Any | None = None
    id: str = "miora"

    def import_credential(self, raw: dict) -> dict:
        return MioraCredential.from_import(raw).to_dict()

    def credential_from(self, credential_data: dict) -> MioraCredential:
        return MioraCredential.from_dict(credential_data)

    def credential_expires_at(self, credential_data: dict) -> int:
        return self.credential_from(credential_data).expires_at

    def login_identity(self, credential_data: dict) -> str:
        return self.credential_from(credential_data).identity()

    def classify(self, status: int, body: bytes) -> ErrKind:
        return classify_status(status, body)

    async def refresh(self, credential_data: dict) -> dict:
        """仅 RefreshTask 持凭证编辑锁调用；调用者须先落盘轮转票据。"""
        return await self.client.refresh_token(self.credential_from(credential_data))

    async def probe_quota(self, credential_data: dict) -> Quota:
        return await self.client.probe_quota(self.credential_from(credential_data))

    async def list_models(self, credential_data: dict) -> list[Model]:
        return await self.client.fetch_models(self.credential_from(credential_data))

    async def stream_chat(
        self, credential_data: dict, payload: dict, model: str
    ) -> AsyncIterator[Event]:
        credential = MioraCredential.from_import(credential_data)
        # 先同步关闭内层 ACP 流，再归还节流名额，客户端断开不依赖 asyncgen GC。
        async with paced(self.pacer, credential.identity()), aclosing(
            self.client.stream_chat(credential, payload, model)
        ) as stream:
            async for event in stream:
                yield event

    async def aclose(self) -> None:
        await self.client.aclose()


__all__ = ["MioraProvider"]
