"""Miora REST 控制面与 ACP 模型通路。

每个账号复用一个专用项目、运行时与目录会话。推理另建隔离会话，
完整客户端历史由 prompt 携带，避免两个客户端在同一云端上下文串话。
长期 token 仅通过 RefreshTask 轮转；运行时短票每次连接现取。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..base import (
    ErrKind,
    Event,
    EventKind,
    InvalidModelName,
    Model,
    Quota,
    UpstreamHTTPError,
    UpstreamProtocolViolation,
    UpstreamReloginRequired,
    ensure_public_host,
    read_body_bounded,
    retry_hints,
)
from ..http_json import json_or_raise
from ..lazy_clients import LazyClients
from .acp import AcpConnection, AcpHTTPError
from .credential import MioraCredential
from .events import classify_status, parse_update
from .tools import MioraToolRequestError, parse_response, prepare_prompt

API = "/api/ai"
PROJECT_TITLE = "2apis API workspace"
MAX_ACCOUNTS = 128
MAX_TOOL_RESPONSE_CHARS = 8 * 1024 * 1024


class MioraHTTPError(UpstreamHTTPError):
    classify_status = staticmethod(classify_status)


class MioraCapabilityError(ValueError):
    """当前通路只接受文本，不把图片或文件内容静默变成文本转发。"""

    def kind(self) -> ErrKind:
        return ErrKind.CAPABILITY


@dataclass(slots=True)
class _Account:
    identity: str = ""
    project_id: str = ""
    runtime_id: str = ""
    catalog_session_id: str = ""
    models: list[Model] = field(default_factory=list)
    selections: dict[str, tuple[str, str]] = field(default_factory=dict)
    default_tier: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def validate_payload(payload: dict) -> None:
    """ACP 没有采样参数入口，显式拒绝不能兑现的参数。"""
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        raise MioraToolRequestError("Miora messages 必须是对话列表")
    for message in messages:
        if not isinstance(message, dict):
            raise MioraToolRequestError("Miora 每条消息必须是 JSON 对象")
        content = message.get("content")
        if content is None or isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise MioraToolRequestError("Miora 消息 content 必须是文本或文本块列表")
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise MioraToolRequestError("Miora 内容块必须声明文本类型")
            if block["type"] not in ("text", "input_text"):
                raise MioraCapabilityError("Miora 当前仅支持文本消息，不支持图片、音频或文件")
            if not isinstance(block.get("text"), str):
                raise MioraToolRequestError("Miora 文本块 text 必须是字符串")
    modalities = payload.get("modalities")
    if modalities is not None:
        if (not isinstance(modalities, list)
                or not all(isinstance(item, str) for item in modalities)):
            raise MioraToolRequestError("Miora modalities 必须是文本模态列表")
        if modalities != ["text"]:
            raise MioraCapabilityError("Miora 输出 modalities 仅支持 ['text']")
    if payload.get("audio") is not None:
        raise MioraCapabilityError("Miora 当前通路不支持音频输出")
    if payload.get("prediction") is not None:
        raise MioraToolRequestError("Miora ACP 不支持 prediction 参数")
    for key, default in (("temperature", 1), ("top_p", 1),
                         ("presence_penalty", 0), ("frequency_penalty", 0)):
        if key in payload and payload[key] not in (None, default):
            raise InvalidModelName(f"Miora ACP 不支持 {key} 参数")
    for key in ("stop", "logprobs", "top_logprobs", "logit_bias", "seed", "response_format"):
        if payload.get(key) is not None:
            raise InvalidModelName(f"Miora ACP 不支持 {key} 参数")
    if payload.get("reasoning_effort") not in (None, "low", "medium", "high"):
        raise InvalidModelName("Miora reasoning_effort 只支持 low / medium / high")


async def validate_acp_link(link: Any) -> tuple[str, str]:
    """仅信任官方响应中的 HTTPS CloudStudio 沙箱地址，拒绝重定向与内网。"""
    if not isinstance(link, dict):
        raise UpstreamProtocolViolation("Miora 运行时缺少 ACP 通路")
    url, token = _text(link.get("url")), _text(link.get("token"))
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise UpstreamProtocolViolation("Miora 返回了无效 ACP 地址") from error
    host = parts.hostname or ""
    if (parts.scheme != "https" or parts.username or parts.password
            or port not in (None, 443) or parts.path != "/acp"
            or not host.endswith(".sandbox.cloudstudio.club") or not token
            or parts.query or parts.fragment):
        raise UpstreamProtocolViolation("Miora 返回了不受信任的 ACP 通路")
    await ensure_public_host(host)
    return url, token


class MioraClient(LazyClients):
    def __init__(self, host: str = "https://miora.design", *, proxy: str | None = None,
                 stream_client: httpx.AsyncClient | None = None,
                 short_client: httpx.AsyncClient | None = None) -> None:
        self.host = host.rstrip("/")
        self._proxy = proxy
        self._stream_client = stream_client
        self._short_client = short_client
        self._accounts: OrderedDict[str, _Account] = OrderedDict()
        self._identity_locks: dict[str, asyncio.Lock] = {}

    def _account(self, credential: MioraCredential) -> _Account:
        identity = credential.identity()
        key = f"{identity}|{credential.project_id}"
        account = self._accounts.get(key)
        if account is None:
            lock = self._identity_locks.get(identity)
            if lock is None:
                lock = asyncio.Lock()
                self._identity_locks[identity] = lock
            account = _Account(identity=identity, project_id=credential.project_id, lock=lock)
            self._accounts[key] = account
        else:
            self._accounts.move_to_end(key)
        return account

    def _trim_accounts(self) -> None:
        for key, account in list(self._accounts.items()):
            if len(self._accounts) <= MAX_ACCOUNTS:
                break
            if account.users == 0 and not account.lock.locked():
                self._accounts.pop(key)
                if not any(other.identity == account.identity for other in self._accounts.values()):
                    self._identity_locks.pop(account.identity)

    @asynccontextmanager
    async def _using_account(self, credential: MioraCredential) -> AsyncIterator[_Account]:
        account = self._account(credential)
        account.users += 1
        self._trim_accounts()
        try:
            yield account
        finally:
            account.users -= 1
            self._trim_accounts()

    @staticmethod
    def _headers(credential: MioraCredential) -> dict[str, str]:
        return {"Authorization": f"Bearer {credential.access_token}",
                "X-Requested-With": "XMLHttpRequest", "x-Source": "web",
                "X-Product": "miora", "X-Miora-Locale": "zh-CN"}

    async def _request(self, credential: MioraCredential, method: str,
                       path: str, *, body: dict | None = None, params: dict | None = None,
                       timeout: float | None = None) -> Any:
        response = await self._short.request(
            method, f"{self.host}{path}", headers=self._headers(credential),
            json=body, params=params,
            timeout=timeout if timeout is not None else (
                150 if method == "POST" and path == f"{API}/cloud-agent/runtimes" else 30))
        if response.status_code == 429:
            body_bytes = await read_body_bounded(response)
            raise MioraHTTPError(429, body_bytes,
                                 **retry_hints(response.headers, body=body_bytes))
        data = await json_or_raise(response, MioraHTTPError, path, with_retry_hints=True)
        if not isinstance(data, dict):
            raise UpstreamProtocolViolation("Miora 返回了无效 REST 响应")
        if data.get("code", 0) != 0:
            # Miora 可用 HTTP 200 传业务错误（新项目尚无 runtime 时 httpStatus=404）。
            status = data.get("httpStatus")
            if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
                status = 400
            body_bytes = json.dumps(data, ensure_ascii=False).encode()
            raise MioraHTTPError(status, body_bytes,
                                 **retry_hints(response.headers, body=body_bytes))
        return data.get("data", data)

    @asynccontextmanager
    async def _connection(self, credential: MioraCredential, account: _Account,
                          endpoint: str, token: str) -> AsyncIterator[AcpConnection]:
        connection = AcpConnection(self._stream, endpoint, token)
        try:
            await connection.__aenter__()
        except AcpHTTPError as error:
            if error.status != 401:
                raise
            # 仅重开尚未发 prompt 的连接；不重放可能已计费的推理。
            endpoint, token = await self._runtime(credential, account)
            connection = AcpConnection(self._stream, endpoint, token)
            await connection.__aenter__()
        try:
            yield connection
        finally:
            await connection.__aexit__(None, None, None)

    async def _project(self, credential: MioraCredential, account: _Account) -> str:
        if account.project_id:
            return account.project_id
        listing = await self._request(credential, "GET", f"{API}/workflow/list", params={
            "type": "my", "page": 1, "pageSize": 100, "keyword": PROJECT_TITLE})
        if not isinstance(listing, dict) or not isinstance(listing.get("list"), list):
            raise UpstreamProtocolViolation("Miora 项目列表格式异常")
        items = listing["list"]
        for item in items:
            if isinstance(item, dict) and item.get("title") == PROJECT_TITLE:
                project = _text(item.get("id"))
                if project:
                    account.project_id = project
                    return project
        project = await self._request(credential, "POST", f"{API}/workflow/create",
                                      body={"title": PROJECT_TITLE})
        project_id = _text(project)
        if not project_id:
            raise UpstreamProtocolViolation("Miora 创建项目未返回项目标识")
        account.project_id = project_id
        return project_id

    async def _runtime(self, credential: MioraCredential, account: _Account) -> tuple[str, str]:
        project = await self._project(credential, account)
        if not account.runtime_id:
            try:
                existing = await self._request(
                    credential, "GET", f"{API}/cloud-agent/runtimes/by-project/{project}")
            except MioraHTTPError as error:
                if error.status != 404:
                    raise
                existing = {}
            if not isinstance(existing, dict):
                raise UpstreamProtocolViolation("Miora 运行时查询格式异常")
            account.runtime_id = _text(existing.get("runtimeId"))
        if account.runtime_id:
            runtime = await self._request(
                credential, "GET", f"{API}/cloud-agent/runtimes/{account.runtime_id}")
        else:
            runtime = await self._request(credential, "POST", f"{API}/cloud-agent/runtimes",
                                          body={"projectId": project})
            account.runtime_id = _text(runtime.get("id")) if isinstance(runtime, dict) else ""
        if not account.runtime_id:
            raise UpstreamProtocolViolation("Miora 运行时缺少标识")
        if not isinstance(runtime, dict):
            raise UpstreamProtocolViolation("Miora 运行时通路格式异常")
        links = runtime.get("links")
        link = links.get("acpLink") if isinstance(links, dict) else None
        return await validate_acp_link(link)

    async def _session(self, credential: MioraCredential, account: _Account) -> str:
        data = await self._request(credential, "POST", f"{API}/cloud-agent/sessions", body={
            "projectId": account.project_id, "runtimeId": account.runtime_id,
            "clientInstanceId": str(uuid.uuid4()), "title": "2apis API request"})
        session = _text(data.get("sessionId")) if isinstance(data, dict) else ""
        if not session:
            raise UpstreamProtocolViolation("Miora 创建会话未返回标识")
        return session

    @staticmethod
    async def _initialize(connection: AcpConnection, session: str) -> dict:
        await connection.call("initialize", {"protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                   "terminal": False}})
        return await connection.call("session/load", {
            "sessionId": session, "cwd": "/workspace", "mcpServers": []})

    async def fetch_models(self, credential: MioraCredential) -> list[Model]:
        async with self._using_account(credential) as account, account.lock:
            tiers = await self._request(credential, "GET", f"{API}/public/cloud-agent/tiers")
            endpoint, token = await self._runtime(credential, account)
            if not account.catalog_session_id:
                account.catalog_session_id = await self._session(credential, account)
            async with self._connection(credential, account, endpoint, token) as connection:
                loaded = await self._initialize(connection, account.catalog_session_id)
            models, selections = catalog_models(loaded, tiers)
            account.models, account.selections = models, selections
            account.default_tier = default_tier(tiers)
            return list(models)

    async def probe_quota(self, credential: MioraCredential) -> Quota:
        data = await self._request(credential, "POST", "/api/miora-billing/credit", body={})
        try:
            total = float(data["totalAmount"])
            used = float(data["usedAmount"])
        except (KeyError, TypeError, ValueError) as error:
            raise UpstreamProtocolViolation("Miora 积分响应缺少有效余额") from error
        if not math.isfinite(total) or not math.isfinite(used) or total < 0 or used < 0:
            raise UpstreamProtocolViolation("Miora 积分响应包含无效余额")
        return Quota(remaining=max(0, total - used), total=total, probed_at=int(time.time()))

    async def refresh_token(self, credential: MioraCredential) -> dict:
        if not credential.refresh_token:
            raise UpstreamReloginRequired("Miora 缺少刷新令牌，需重新登录")
        response = await self._short.post(f"{self.host}/api/auth/refresh-token",
                                          json={"refresh_token": credential.refresh_token})
        if response.status_code == 429:
            body_bytes = await read_body_bounded(response)
            raise MioraHTTPError(429, body_bytes,
                                 **retry_hints(response.headers, body=body_bytes))
        if response.status_code in (400, 401):
            raise UpstreamReloginRequired("Miora 刷新令牌已失效，需重新登录")
        data = await json_or_raise(response, MioraHTTPError, "Miora token refresh",
                                   with_retry_hints=True)
        if not isinstance(data, dict) or not _text(data.get("access_token")):
            raise UpstreamProtocolViolation("Miora 刷新响应缺少 access_token")
        if not _text(data.get("refresh_token")):
            raise UpstreamProtocolViolation("Miora 刷新响应缺少 refresh_token")
        old = credential.to_dict()
        old.pop("expires_at", None)
        return MioraCredential.from_dict({**old, **data}).to_dict()

    async def stream_chat(self, credential: MioraCredential, payload: dict,
                          model: str) -> AsyncIterator[Event]:
        validate_payload(payload)
        prompt = prepare_prompt(payload)
        async with self._using_account(credential) as account:
            if not account.models:
                await self.fetch_models(credential)
            selected = account.selections.get(model)
            if selected is None:
                raise InvalidModelName("Miora 不提供指定模型")
            # tier/effort 属于运行时全局设置，整轮按账号串行；不同账号仍并行。
            async with account.lock, aclosing(self._stream_session(
                    credential, account, payload, prompt, selected)) as stream:
                async for event in stream:
                    yield event

    async def _stream_session(self, credential: MioraCredential, account: _Account,
                              payload: dict, prompt: list[dict],
                              selected: tuple[str, str]) -> AsyncIterator[Event]:
        raw_model, tier = selected
        tier = tier or account.default_tier
        endpoint, token = await self._runtime(credential, account)
        session = await self._session(credential, account)
        if tier:
            await self._request(credential, "POST",
                f"{API}/cloud-agent/runtimes/{account.runtime_id}/update-subagent-models",
                body={"tierKey": tier})
        # runtime effort 是账号共享状态，每轮明确写入，不能继承上一调用的档位。
        await self._request(credential, "POST",
            f"{API}/cloud-agent/runtimes/{account.runtime_id}/update-effort",
            body={"effort": payload.get("reasoning_effort") or "medium"})
        tools = bool(payload.get("tools"))
        text: list[str] = []
        text_length = 0
        finish: Event | None = None
        async with self._connection(credential, account, endpoint, token) as connection:
            await self._initialize(connection, session)
            await connection.call("session/set_model", {"sessionId": session, "modelId": raw_model})
            try:
                async with aclosing(connection.rpc_frames("session/prompt", {
                        "sessionId": session, "prompt": prompt})) as frames:
                    async for frame in frames:
                        params = frame.get("params")
                        if (isinstance(params, dict)
                                and params.get("sessionId") not in (None, session)):
                            continue
                        if cloud_tool_requested(frame):
                            raise UpstreamProtocolViolation(
                                "Miora 尝试执行云端内置工具；该通路仅允许客户端工具桥接")
                        for event in parse_update(frame):
                            if tools and event.kind == EventKind.CONTENT:
                                text_length += len(event.content or "")
                                if text_length > MAX_TOOL_RESPONSE_CHARS:
                                    raise UpstreamProtocolViolation("Miora 工具响应超过大小上限")
                                text.append(event.content or "")
                            elif event.kind == EventKind.FINISH:
                                finish = event
                            else:
                                yield event
            finally:
                if finish is None:
                    await connection.notify("session/cancel", {"sessionId": session})
        if tools:
            events = parse_response("".join(text), payload)
            for event in events:
                yield event
            if any(event.kind == EventKind.TOOL_CALLS for event in events):
                finish = Event(kind=EventKind.FINISH, finish_reason="tool_calls")
        if finish is None:
            raise UpstreamProtocolViolation("Miora 对话缺少终态")
        yield finish


def catalog_models(loaded: Any, tiers: Any) -> tuple[list[Model], dict[str, tuple[str, str]]]:
    """ACP 权威目录与网页模式目录合并，全部映射回真实 modelId。"""
    root = loaded.get("models") if isinstance(loaded, dict) else None
    meta = root.get("_meta") if isinstance(root, dict) else None
    namespace = meta.get("codebuddy.ai") if isinstance(meta, dict) else None
    raw = namespace.get("availableModels") if isinstance(namespace, dict) else None
    if not isinstance(raw, list) or not raw:
        raise UpstreamProtocolViolation("Miora ACP 缺少模型目录")
    tier_items = tiers if isinstance(tiers, list) else []
    by_model = {_text(item.get("modelId")): item for item in tier_items if isinstance(item, dict)}
    models: list[Model] = []
    selections: dict[str, tuple[str, str]] = {}
    raw_ids = {_text(item.get("id")) for item in raw if isinstance(item, dict)}
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_id = _text(item.get("id"))
        if not model_id or model_id in selections:
            continue
        tier = by_model.get(model_id, {})
        name = _text(item.get("name")) or model_id
        # 同渠道标准/免费路由不是可互换的原始 ID；通用命名会剥 free，须保留路由区别。
        if f"{model_id}-free" in raw_ids:
            name += " 标准路由"
        elif model_id.endswith("-free") and model_id[:-5] in raw_ids:
            name += " 免费路由"
        models.append(Model(id=model_id, name=name,
            credit_rate=_rate(tier.get("billingFactor")),
            max_input_tokens=_positive_int(item.get("maxInputTokens")),
            max_output_tokens=_positive_int(item.get("maxOutputTokens")),
            supports_images=False,
            supports_tool_call=True, supports_reasoning=item.get("supportsReasoning") is True))
        selections[model_id] = (model_id, _text(tier.get("tierKey")))
    for item in tier_items:
        if not isinstance(item, dict) or item.get("category") != "tier":
            continue
        tier_key, raw_id = _text(item.get("tierKey")), _text(item.get("modelId"))
        if not tier_key or raw_id not in selections:
            continue
        model_id = f"miora-{tier_key}"
        locales = item.get("locales")
        chinese = locales.get("zh-CN") if isinstance(locales, dict) else None
        name = _text(chinese.get("name")) if isinstance(chinese, dict) else ""
        models.append(Model(id=model_id, name=name or tier_key,
                            credit_rate=_rate(item.get("billingFactor")), supports_tool_call=True))
        selections[model_id] = (raw_id, tier_key)
    if not models:
        raise UpstreamProtocolViolation("Miora ACP 返回了空模型目录")
    return models, selections


def default_tier(tiers: Any) -> str:
    """仅恢复目录明确标记的真实默认 tier，不推测固定上游值。"""
    if not isinstance(tiers, list):
        return ""
    for item in tiers:
        if isinstance(item, dict) and item.get("isDefault") is True:
            tier = _text(item.get("tierKey"))
            if tier:
                return tier
    return ""


def cloud_tool_requested(frame: dict) -> bool:
    params = frame.get("params")
    if frame.get("method") != "session/update" or not isinstance(params, dict):
        return False
    meta = params.get("_meta")
    namespace = meta.get("codebuddy.ai") if isinstance(meta, dict) else None
    if isinstance(namespace, dict) and namespace.get("mode") == "history":
        return False
    update = params.get("update")
    return isinstance(update, dict) and update.get("sessionUpdate") == "tool_call"
