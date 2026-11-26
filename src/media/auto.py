"""只消费本适配器注入的媒体工具，客户端函数与 tool_choice 保持原有语义。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import aclosing

from ..compat.openai.request import InvalidRequest
from ..provider.base import Event, EventKind
from ..request_context import get_credential_id
from .context import get_media_principal
from .request import parse_media_request

NAMES = {"image": "miora_generate_image", "video": "miora_generate_video"}


def builtin_options(marker: dict) -> dict:
    if not isinstance(marker, dict):
        raise InvalidRequest("Miora image_generation options must be an object")
    ignored = {"type", "quality", "background", "output_format", "partial_images"}
    if set(marker) - ignored - {"model", "n", "size", "aspect_ratio", "resolution"}:
        raise InvalidRequest("unsupported Miora image_generation options")
    if marker.get("quality", "auto") != "auto" or marker.get("background", "auto") != "auto":
        raise InvalidRequest("Miora image_generation only accepts automatic quality/background")
    partial = marker.get("partial_images", 0)
    if marker.get("output_format", "auto") != "auto" or type(partial) is not int or partial != 0:
        raise InvalidRequest(
            "Miora returns its original file format and does not support partial_images"
        )
    options = {key: value for key, value in marker.items() if key not in ignored}
    parse_media_request({"model": "validation", "prompt": "validation", **options}, "image")
    return options


class MediaAuto:
    def __init__(self, service):
        self.service = service

    async def stream_chat(
        self, credential, payload: dict, model: str, stream_factory
    ) -> AsyncIterator[Event]:
        principal = get_media_principal()
        account_id = get_credential_id()
        choice = payload.get("tool_choice", "auto")
        marker = payload.get("miora_image_generation")
        tools = payload.get("tools", [])
        enabled = (
            principal is not None
            and account_id is not None
            and getattr(self.service.settings, "media_enabled", True)
            and getattr(self.service.settings, "miora_auto_media_enabled", True)
            and choice == "auto"
            and isinstance(tools, list)
        )
        forced = payload.get("miora_image_generation_forced") is True
        if forced and choice == "auto" and not enabled:
            raise InvalidRequest(
                "Miora image_generation is disabled or unavailable for this request"
            )
        internal: dict[str, str] = {}
        augmented = dict(payload)
        if enabled:
            external_names = {
                tool.get("function", {}).get("name")
                for tool in tools
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
            }
            options = builtin_options(marker) if marker is not None else {}
            appended = list(tools)
            for kind, name in NAMES.items():
                if name in external_names or marker is not None and kind != "image":
                    continue
                try:
                    models = await self.service.models(principal, account_id, kind)
                except Exception:
                    if forced:
                        raise InvalidRequest(
                            "selected account has no available image_generation model"
                        ) from None
                    continue
                ids = [entry.id for entry in models]
                if not ids:
                    continue
                internal[name] = kind
                properties = {
                    "model": {"type": "string", "enum": ids},
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 100000},
                    "size": {"type": "string"},
                    "aspect_ratio": {"type": "string"},
                    "resolution": {"type": "string"},
                }
                if kind == "image":
                    properties["n"] = {"type": "integer", "minimum": 1, "maximum": 4}
                else:
                    properties["seconds"] = {"type": "integer", "minimum": 1, "maximum": 60}
                    properties["generate_audio"] = {"type": "boolean"}
                appended.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": (
                                f"Generate a real {kind} only for an explicit user request. "
                                "The gateway executes this server tool and returns real resources."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": properties,
                                "required": ["model", "prompt"],
                                "additionalProperties": False,
                            },
                        },
                    }
                )
            if marker is not None:
                if forced and NAMES["image"] not in internal:
                    raise InvalidRequest(
                        "image_generation is unavailable or conflicts with a client function"
                    )
                if forced:
                    augmented["tool_choice"] = {
                        "type": "function",
                        "function": {"name": NAMES["image"]},
                    }
            augmented["tools"] = appended
        finish = None
        external_calls = False
        executed: set[str] = set()
        async with aclosing(stream_factory(credential, augmented, model)) as stream:
            async for event in stream:
                if event.kind == EventKind.FINISH:
                    finish = event
                    continue
                if event.kind != EventKind.TOOL_CALLS or not internal:
                    yield event
                    continue
                remaining = []
                for call in event.tool_calls or []:
                    name = call.get("function", {}).get("name")
                    if name not in internal:
                        remaining.append(call)
                        continue
                    call_id = call.get("id", "")
                    if call_id in executed:
                        continue
                    executed.add(call_id)
                    try:
                        arguments = json.loads(call["function"]["arguments"])
                        if marker is not None:
                            # 固定 size 的派生比例/分辨率优先；模型不能偷偷提升输出规格。
                            if options.get("size") not in (None, "auto"):
                                for key in ("aspect_ratio", "resolution"):
                                    if key not in options:
                                        arguments.pop(key, None)
                            elif "aspect_ratio" in options or "resolution" in options:
                                arguments.pop("size", None)
                        arguments = {**arguments, **(options if marker is not None else {})}
                        request = parse_media_request(arguments, internal[name])
                        job = await self.service.submit(
                            principal, request, account_id=account_id, already_paced=True
                        )
                        job = await self.service.wait(job, principal)
                        links = [
                            self.service.store.signed_url(
                                job, self.service.settings.public_base_url, index
                            )
                            for index, _asset in enumerate(job["files"])
                        ]
                        text = "\n".join(
                            f"![生成图片]({url})"
                            if internal[name] == "image"
                            else f"[生成视频]({url})"
                            for url in links
                        )
                        yield Event(kind=EventKind.CONTENT, content=text + "\n")
                    except Exception:
                        # 不把已提交的媒体失败抛回 engine，避免上层换号重放整段对话。
                        yield Event(
                            kind=EventKind.CONTENT,
                            content="媒体生成未完成或失败，生成请求没有自动重试。\n",
                        )
                if remaining:
                    external_calls = True
                    yield Event(
                        kind=EventKind.TOOL_CALLS,
                        tool_calls=[
                            {**call, "index": index} for index, call in enumerate(remaining)
                        ],
                    )
        if finish is not None:
            if internal:
                finish = Event(
                    kind=EventKind.FINISH, finish_reason="tool_calls" if external_calls else "stop"
                )
            yield finish
