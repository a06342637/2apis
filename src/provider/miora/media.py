"""Miora Film Studio 文生图/视频协议；生成提交不会自动重放。

目录由官方 media-models 动态下发，按当前账号的可用性与文本能力过滤。
Film 会话独立于 ACP 项目，一个账号复用命名会话，生成任务上下文互相隔离。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..base import InvalidModelName, Model, UpstreamProtocolViolation, ensure_public_host
from .client import MAX_ACCOUNTS, MioraClient, MioraHTTPError
from .credential import MioraCredential

FILM_API = "/api/ai/film-studio"
MEDIA_CATALOG_API = "/api/ai/public/cloud-agent/media-models"
SESSION_TITLE = "2apis Film Studio"
CATALOG_TTL = 300
FILE_HOST = "sign.miorausercontent.com"
ENDPOINTS = {"image": ("/v1/images/generations",), "video": ("/v1/videos",)}
STATUSES = {
    "pending": "queued",
    "processing": "in_progress",
    "completed": "completed",
    "failed": "failed",
}


class MioraMediaRequestError(InvalidModelName):
    """未支持的模型/参数：拒绝本次请求，不把错误归咎于账号。"""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: Any) -> list[str]:
    return [_text(item) for item in value if _text(item)] if isinstance(value, list) else []


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(slots=True)
class MediaModel:
    id: str
    raw_id: str
    name: str
    kind: str
    capabilities: dict

    def model(self) -> Model:
        return Model(
            id=self.id,
            name=self.name,
            supports_images=False,
            supports_tool_call=False,
            supports_reasoning=False,
            gateway_endpoints=ENDPOINTS[self.kind],
            output_modalities=(self.kind,),
        )


def parse_catalog(data: Any) -> dict[str, MediaModel]:
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise UpstreamProtocolViolation("Miora 媒体模型目录格式异常")
    models: dict[str, MediaModel] = {}
    for row in data["data"]:
        if not isinstance(row, dict) or not row.get("visible") or row.get("available") is False:
            continue
        kind, model_id = row.get("contentType"), _text(row.get("modelId") or row.get("modelKey"))
        caps = row.get("capabilities")
        if (
            not isinstance(kind, str)
            or kind not in ENDPOINTS
            or not model_id
            or not isinstance(caps, dict)
        ):
            continue
        if (
            row.get("editOnly")
            or (kind == "image" and "text_to_image" not in _strings(caps.get("supportedTools")))
            or (kind == "video" and caps.get("supportsTextToVideo") is not True)
        ):
            continue
        name = _text(row.get("label")) or model_id
        variants = caps.get("variants")
        choices = (
            [
                variant
                for variant in variants
                if isinstance(variant, dict)
                and _text(variant.get("modelId"))
                and variant.get("available") is not False
            ]
            if isinstance(variants, list)
            else []
        )
        raw_id = model_id
        if choices:
            default = next(
                (
                    variant
                    for variant in choices
                    if variant["modelId"] == caps.get("defaultVariantId")
                ),
                None,
            )
            default = default or next(
                (variant for variant in choices if variant.get("isDefault")), choices[0]
            )
            raw_id = _text(default["modelId"])
        elif isinstance(variants, list) and variants:
            continue
        models[model_id] = MediaModel(model_id, raw_id, name, kind, caps)
        for variant in choices:
            variant_id = _text(variant["modelId"])
            models[variant_id] = MediaModel(
                variant_id, variant_id, _text(variant.get("label")) or variant_id, kind, caps
            )
    return models


def generation_body(
    model: MediaModel,
    prompt: str,
    *,
    count: int = 1,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    duration: int | None = None,
    generate_audio: bool | None = None,
) -> dict:
    if not isinstance(prompt, str) or not prompt.strip():
        raise MioraMediaRequestError("Miora 媒体 prompt 必须是非空文本")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 4:
        raise MioraMediaRequestError("Miora 媒体 count 必须是 1 至 4 的整数")
    caps = model.capabilities
    limit = caps.get("promptLengthLimit")
    if _number(limit) and limit > 0 and len(prompt) > limit:
        raise MioraMediaRequestError("Miora 媒体 prompt 超出该模型长度限制")
    ratios = _strings(caps.get("aspectRatios"))
    ratio = aspect_ratio if aspect_ratio is not None else _text(caps.get("defaultAspectRatio"))
    if not isinstance(ratio, str) or ratio not in ratios:
        raise MioraMediaRequestError("Miora 媒体 aspect_ratio 不在该模型支持列表中")
    resolutions = _strings(
        caps.get("allowedResolutions" if model.kind == "image" else "resolutions")
    )
    quality = resolution if resolution is not None else _text(caps.get("defaultResolution"))
    if not isinstance(quality, str) or quality not in resolutions:
        raise MioraMediaRequestError("Miora 媒体 resolution 不在该模型支持列表中")
    extra: dict = {"aspectRatio": ratio, "resolution": quality}
    params: dict = {
        "type": f"text_to_{model.kind}",
        "model": model.raw_id,
        "prompt": prompt,
        "extraParameters": extra,
    }
    if model.kind == "image":
        if duration is not None or generate_audio is not None:
            raise MioraMediaRequestError("Miora 文生图不接受 duration 或 generate_audio")
    else:
        seconds = duration if duration is not None else caps.get("defaultSeconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise MioraMediaRequestError("Miora 视频 duration 必须是正整数秒")
        allowed = caps.get("allowedSeconds")
        seconds_range = caps.get("secondsRange")
        if isinstance(allowed, list) and any(
            isinstance(item, bool) or not isinstance(item, int) for item in allowed
        ):
            raise UpstreamProtocolViolation("Miora 视频目录时长列表格式异常")
        if isinstance(allowed, list) and seconds not in allowed:
            raise MioraMediaRequestError("Miora 视频 duration 不在该模型支持列表中")
        if isinstance(seconds_range, dict):
            lower, upper = seconds_range.get("min"), seconds_range.get("max")
            if not _number(lower) or not _number(upper) or lower > upper:
                raise UpstreamProtocolViolation("Miora 视频目录时长范围格式异常")
            if seconds < lower or seconds > upper:
                raise MioraMediaRequestError("Miora 视频 duration 超出该模型范围")
        by_resolution = caps.get("allowedSecondsByResolution", {})
        if (
            isinstance(by_resolution, dict)
            and quality in by_resolution
            and seconds not in by_resolution[quality]
        ):
            raise MioraMediaRequestError("Miora 视频 duration 不适用于该分辨率")
        if not isinstance(allowed, list) and not isinstance(seconds_range, dict):
            raise UpstreamProtocolViolation("Miora 视频目录缺少时长限制")
        params["duration"] = seconds
        extra["resolution"] = quality.upper()
        if caps.get("supportsAspectRatioParam") is False:
            if aspect_ratio is not None:
                raise MioraMediaRequestError("Miora 此视频模型不接受 aspect_ratio 参数")
            extra.pop("aspectRatio")
        if generate_audio is not None and (
            not isinstance(generate_audio, bool) or not caps.get("supportsAudioControl")
        ):
            raise MioraMediaRequestError("Miora 此视频模型不支持 generate_audio 参数")
        if caps.get("supportsAudioControl"):
            params["generateAudio"] = generate_audio if generate_audio is not None else False
    return {
        "count": count,
        "prompt": prompt,
        "mediaType": model.kind,
        "taskType": f"{model.kind}_text",
        "model": model.raw_id,
        "editPayload": {f"{model.kind}Params": params},
    }


def normalize_task(task: Any, *, session_id: str = "", task_id: str = "") -> dict:
    if not isinstance(task, dict):
        raise UpstreamProtocolViolation("Miora 媒体任务格式异常")
    sid, tid = _text(task.get("sessionId")), _text(task.get("id"))
    kind, status = task.get("mediaType"), task.get("status")
    slots = task.get("slots")
    if (
        not sid
        or not tid
        or (session_id and sid != session_id)
        or (task_id and tid != task_id)
        or not isinstance(kind, str)
        or kind not in ENDPOINTS
        or not isinstance(status, str)
        or status not in STATUSES
        or not isinstance(slots, list)
    ):
        raise UpstreamProtocolViolation("Miora 媒体任务身份或状态异常")
    files, errors, progress = [], [], []
    for slot in slots:
        if (
            not isinstance(slot, dict)
            or not isinstance(slot.get("status"), str)
            or slot["status"] not in STATUSES
        ):
            raise UpstreamProtocolViolation("Miora 媒体任务结果格式异常")
        raw_progress = slot.get("progress", 0)
        progress.append(
            100
            if slot["status"] == "completed"
            else max(0, min(100, raw_progress))
            if _number(raw_progress)
            else 0
        )
        message = _text(slot.get("errorMessage"))
        if message:
            errors.append(message)
        if slot["status"] == "completed" and not slot.get("assetDeleted"):
            file_key = _text(slot.get("fileKey"))
            file_kind = slot.get("fileType", kind)
            if not file_key or file_kind != kind:
                raise UpstreamProtocolViolation("Miora 已完成任务缺少可下载文件")
            item = {"file_key": file_key, "kind": file_kind}
            for key in ("width", "height", "duration"):
                if _number(slot.get(key)) and slot[key] > 0:
                    item[key] = slot[key]
            files.append(item)
    normalized = STATUSES[status]
    if normalized == "completed" and not files:
        normalized = "failed"
        errors.append("Miora 生成结果已删除或没有可下载文件")
    result = {
        "session_id": sid,
        "task_id": tid,
        "kind": kind,
        "model": _text(task.get("modelName")),
        "status": normalized,
        "progress": 100
        if normalized in ("completed", "failed")
        else round(sum(progress) / len(progress))
        if progress
        else 0,
        "files": files,
    }
    if errors or normalized == "failed":
        result["error"] = "; ".join(errors) or "Miora 媒体生成失败"
    return result


@dataclass(slots=True)
class _Account:
    session_id: str = ""
    models: dict[str, MediaModel] = field(default_factory=dict)
    catalog_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MioraMediaClient:
    def __init__(self, client: MioraClient) -> None:
        self.client = client
        self._accounts: OrderedDict[str, _Account] = OrderedDict()

    def _account(self, credential: MioraCredential) -> _Account:
        identity = credential.identity()
        if identity not in self._accounts:
            self._accounts[identity] = _Account()
        self._accounts.move_to_end(identity)
        for key, account in list(self._accounts.items()):
            if len(self._accounts) <= MAX_ACCOUNTS:
                break
            if key != identity and not account.lock.locked():
                self._accounts.pop(key)
        return self._accounts[identity]

    async def _catalog(
        self, credential: MioraCredential, account: _Account, *, force: bool = False
    ) -> dict[str, MediaModel]:
        if (
            not force
            and account.catalog_at is not None
            and time.monotonic() - account.catalog_at < CATALOG_TTL
        ):
            return account.models
        response = await self.client._request(
            credential, "GET", MEDIA_CATALOG_API, params={"lang": "zh-CN"}
        )
        account.models = parse_catalog(response)
        account.catalog_at = time.monotonic()
        return account.models

    async def catalog(self, credential: MioraCredential, *, force: bool = False) -> list[Model]:
        account = self._account(credential)
        async with account.lock:
            return [
                model.model()
                for model in (await self._catalog(credential, account, force=force)).values()
            ]

    async def submit(
        self, credential: MioraCredential, kind: str, model: str, prompt: str, **options: Any
    ) -> dict:
        account = self._account(credential)
        async with account.lock:
            catalogue = await self._catalog(credential, account)
            choice = catalogue.get(model)
            if choice is None or choice.kind != kind:
                raise MioraMediaRequestError("Miora 模型不可用或不支持此媒体类型")
            if set(options) - {"count", "aspect_ratio", "resolution", "duration", "generate_audio"}:
                raise MioraMediaRequestError("Miora 媒体请求包含不支持的参数")
            payload = generation_body(choice, prompt, **options)
            if not account.session_id:
                sessions = await self.client._request(
                    credential,
                    "GET",
                    f"{FILM_API}/sessions",
                    params={"page": 1, "pageSize": 50, "keyword": SESSION_TITLE},
                )
                if not isinstance(sessions, dict) or not isinstance(sessions.get("items"), list):
                    raise UpstreamProtocolViolation("Miora Film 会话列表格式异常")
                for session in sessions["items"]:
                    if isinstance(session, dict) and session.get("title") == SESSION_TITLE:
                        account.session_id = _text(session.get("id"))
                        if not account.session_id:
                            raise UpstreamProtocolViolation("Miora Film 会话缺少 ID")
                        break
            if account.session_id:
                task = await self.client._request(
                    credential,
                    "POST",
                    f"{FILM_API}/sessions/{quote(account.session_id, safe='')}/tasks",
                    body=payload,
                    timeout=120,
                )
                return normalize_task(task, session_id=account.session_id)
            response = await self.client._request(
                credential,
                "POST",
                f"{FILM_API}/sessions",
                body={**payload, "autoTitle": True},
                timeout=120,
            )
            if not isinstance(response, dict) or not isinstance(response.get("session"), dict):
                raise UpstreamProtocolViolation("Miora 新建 Film 会话响应异常")
            sid = _text(response["session"].get("id"))
            result = normalize_task(response.get("task"), session_id=sid)
            if not sid:
                raise UpstreamProtocolViolation("Miora 新建 Film 会话缺少 ID")
            account.session_id = sid
            # 命名失败不使已提交的付费任务变成失败，调用方不得因此重放生成。
            with suppress(httpx.HTTPError, MioraHTTPError, UpstreamProtocolViolation):
                await self.client._request(
                    credential,
                    "POST",
                    f"{FILM_API}/sessions/{quote(sid, safe='')}/update",
                    body={"title": SESSION_TITLE},
                )
            return result

    async def status(self, credential: MioraCredential, session_id: str, task_id: str) -> dict:
        response = await self.client._request(
            credential, "POST", f"{FILM_API}/tasks/progress", body={"taskIds": [task_id]}
        )
        if not isinstance(response, list):
            raise UpstreamProtocolViolation("Miora 媒体任务进度响应异常")
        task = next(
            (item for item in response if isinstance(item, dict) and item.get("id") == task_id),
            None,
        )
        if task is None:
            raise MioraMediaRequestError("Miora 媒体任务不存在或无权访问")
        return normalize_task(task, session_id=session_id, task_id=task_id)

    async def file_url(self, credential: MioraCredential, file_key: str) -> str:
        if (
            not isinstance(file_key, str)
            or not file_key.startswith("file/")
            or any(part in ("", ".", "..") for part in file_key.split("/"))
            or any(char in file_key for char in "\\?#")
            or any(ord(char) < 32 or ord(char) == 127 for char in file_key)
        ):
            raise MioraMediaRequestError("Miora 媒体 file_key 格式异常")
        response = await self.client._request(
            credential, "POST", "/api/ai/cos/cdn-sign-url", body={"filePaths": [file_key]}
        )
        urls = response.get("signedUrls") if isinstance(response, dict) else None
        url = _text(urls[0]) if isinstance(urls, list) and len(urls) == 1 else _text(urls)
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as error:
            raise UpstreamProtocolViolation("Miora 媒体下载地址格式异常") from error
        if (
            parts.scheme != "https"
            or parts.hostname != FILE_HOST
            or port not in (None, 443)
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise UpstreamProtocolViolation("Miora 返回了不受信任的媒体下载地址")
        await ensure_public_host(FILE_HOST)
        return url
