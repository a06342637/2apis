"""Miora 多账号渠道；复用共享调度、加密存储与 OpenAI/Anthropic 协议出口。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...tasks.pacer import paced
from ..base import ErrKind, Event, Model, Quota, UpstreamProtocolViolation
from .client import MioraClient, MioraHTTPError
from .credential import MioraCredential
from .events import classify_status
from .media import MioraMediaClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MioraProvider:
    client: MioraClient = field(default_factory=MioraClient)
    media_client: MioraMediaClient = field(init=False)
    media_catalog_error: str | None = field(default=None, init=False)
    pacer: Any | None = None
    media_auto: Any | None = None
    id: str = "miora"

    def __post_init__(self) -> None:
        self.media_client = MioraMediaClient(self.client)

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
        credential = self.credential_from(credential_data)
        text_models = await self.client.fetch_models(credential)
        try:
            media_models = await self.media_client.catalog(credential)
        except (httpx.HTTPError, MioraHTTPError, UpstreamProtocolViolation) as failure:
            self.media_catalog_error = "Miora 媒体目录暂不可用，已保留文本模型目录"
            logger.warning("%s (%s)", self.media_catalog_error, type(failure).__name__)
            return text_models
        self.media_catalog_error = None
        return text_models + media_models

    def model_list_source(self) -> tuple[bool, str]:
        return self.media_catalog_error is not None, self.media_catalog_error or ""

    async def stream_chat(
        self, credential_data: dict, payload: dict, model: str
    ) -> AsyncIterator[Event]:
        credential = MioraCredential.from_import(credential_data)
        # 先同步关闭内层 ACP 流，再归还节流名额，客户端断开不依赖 asyncgen GC。
        inner = (self.media_auto.stream_chat(credential, payload, model,
                                            self.client.stream_chat)
                 if self.media_auto is not None
                 else self.client.stream_chat(credential, payload, model))
        async with paced(self.pacer, credential.identity()), aclosing(inner) as stream:
            async for event in stream:
                yield event

    async def aclose(self) -> None:
        await self.client.aclose()


__all__ = ["MioraProvider"]
