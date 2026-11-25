"""OpenAI 媒体请求映射，只接受 Miora 网页真实支持的选项。"""

from __future__ import annotations

import math
import re

from ..compat.openai.request import InvalidRequest, require_positive_int


def parse_media_request(body: object, kind: str) -> dict:
    if not isinstance(body, dict):
        raise InvalidRequest("media request must be a JSON object")
    allowed = {
        "model",
        "prompt",
        "n",
        "size",
        "response_format",
        "user",
        "aspect_ratio",
        "resolution",
        "seconds",
        "duration",
        "generate_audio",
    }
    if set(body) - allowed:
        raise InvalidRequest("Miora 不支持这些媒体请求选项")
    prompt, model = body.get("prompt"), body.get("model")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
        raise InvalidRequest("prompt must be a non-empty string of at most 100000 characters")
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise InvalidRequest("model must be a non-empty media model id")
    count = require_positive_int(body.get("n", 1), "n", maximum=4)
    response_format = body.get("response_format", "url")
    if response_format not in ("url", "b64_json"):
        raise InvalidRequest("response_format must be url or b64_json")
    options = {"count": count}
    for name in ("aspect_ratio", "resolution"):
        if name in body:
            value = body[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 32:
                raise InvalidRequest(f"{name} must be a short string")
            options[name] = value
    size = body.get("size")
    if size not in (None, "auto"):
        if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", size):
            raise InvalidRequest("size must be WIDTHxHEIGHT or auto")
        width, height = (int(piece) for piece in size.split("x"))
        divisor = math.gcd(width, height)
        options.setdefault("aspect_ratio", f"{width // divisor}:{height // divisor}")
        if kind == "image":
            resolution = {1024: "1K", 1536: "1.5K", 2048: "2K", 4096: "4K"}.get(max(width, height))
        else:
            resolution = f"{min(width, height)}P"
        if resolution is None:
            raise InvalidRequest("Miora 不支持该输出分辨率")
        options.setdefault("resolution", resolution)
    duration = body.get("seconds", body.get("duration"))
    if duration is not None:
        if isinstance(duration, str) and re.fullmatch(r"[0-9]{1,2}", duration):
            duration = int(duration)
        options["duration"] = require_positive_int(duration, "seconds", maximum=60)
    if "generate_audio" in body:
        if not isinstance(body["generate_audio"], bool):
            raise InvalidRequest("generate_audio must be a boolean")
        options["generate_audio"] = body["generate_audio"]
    if kind == "image" and ("duration" in options or "generate_audio" in options):
        raise InvalidRequest("image generation does not accept video options")
    if kind == "video" and (count != 1 or response_format != "url"):
        raise InvalidRequest("video generation accepts one asynchronous video per request")
    return {
        "model": model.strip(),
        "prompt": prompt,
        "kind": kind,
        "options": options,
        "response_format": response_format,
    }
