"""Miora 多账号渠道；复用共享调度、加密存储与 OpenAI/Anthropic 协议出口。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...tasks.pacer import Pacer, paced
from ..base import ErrKind, Event, InvalidModelName, Model, Quota, UpstreamProtocolViolation
from ..model_policy import MIORA_RETIRED_MESSAGE, model_retired
from .client import CHAT_QUEUE_TIMEOUT_SECONDS, MioraClient, MioraHTTPError, MioraQueueTimeout
from .credential import MioraCredential
from .events import classify_status
from .live import CredentialReader, current_credential
from .media import MioraMediaClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MioraProvider:
    client: MioraClient = field(default_factory=MioraClient)
    media_client: MioraMediaClient = field(init=False)
    media_catalog_error: str | None = field(default=None, init=False)
    pacer: Any | None = None
    media_auto: Any | None = None
    reload_credential: CredentialReader | None = None
    before_inference: Callable[[], None] | None = None
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

    def queue_delay(self, credential_data: dict) -> float | None:
        """纯本地容量快照；相同账号的项目别名仍共享运行时锁。"""
        identity = self.login_identity(credential_data)
        if isinstance(self.client, MioraClient):
            lock = self.client._identity_locks.get(identity)
            if lock is not None and lock.locked():
                return None
        return self.pacer.minimum_wait(identity) if isinstance(self.pacer, Pacer) else 0.0

    async def refresh(self, credential_data: dict) -> dict:
        """仅 RefreshTask 持凭证编辑锁调用；调用者须先落盘轮转票据。"""
        return await self.client.refresh_token(self.credential_from(credential_data))

    async def probe_quota(self, credential_data: dict) -> Quota:
        credential = self.credential_from(credential_data)
        quota = await self.client.probe_quota(credential)
        if quota.detail is not None:
            self.media_client.note_subscription(credential, quota.detail)
        return quota

    async def list_models(self, credential_data: dict) -> list[Model]:
        credential = self.credential_from(credential_data)
        text_models = [model for model in await self.client.fetch_models(credential)
                       if not model_retired(self.id, model.id)]
        try:
            media_models = await self.media_client.catalog(credential, include_unavailable=True)
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
        if model_retired(self.id, model):
            raise InvalidModelName(MIORA_RETIRED_MESSAGE)
        credential = MioraCredential.from_import(credential_data)
        # 先同步关闭内层 ACP 流，再归还节流名额，客户端断开不依赖 asyncgen GC。
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(CHAT_QUEUE_TIMEOUT_SECONDS):
                    await stack.enter_async_context(paced(self.pacer, credential.identity()))
            except TimeoutError as error:
                raise MioraQueueTimeout("Miora 调用排队超时，请稍后重试") from error
            if self.before_inference is not None:
                self.before_inference()
            credential = current_credential(credential, self.reload_credential)
            if isinstance(self.client, MioraClient):
                self.client.reload_credential = self.reload_credential
                self.client.before_inference = self.before_inference
            inner = (self.media_auto.stream_chat(credential, payload, model,
                                                self.client.stream_chat)
                     if self.media_auto is not None
                     else self.client.stream_chat(credential, payload, model))
            async with aclosing(inner) as stream:
                async for event in stream:
                    yield event

    async def aclose(self) -> None:
        await self.client.aclose()


__all__ = ["MioraProvider"]
