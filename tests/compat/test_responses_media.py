"""Responses 内置生图配置与客户端函数工具可同时进入 Miora 自动工具层。"""

import pytest

from src.compat.openai.request import InvalidRequest
from src.compat.responses.request import parse_responses_request


def test_image_generation_preserves_options_and_external_function():
    tool = {"type": "image_generation", "size": "1024x1024", "quality": "low"}
    request = parse_responses_request({"model": "glm-5.3@miora", "input": "draw a cat",
        "tools": [tool, {"type": "function", "name": "read_file",
                         "parameters": {"type": "object"}}]})
    assert request.raw["miora_image_generation"] == tool
    assert request.raw["miora_image_generation"] is not tool
    assert request.raw["tools"][0]["function"]["name"] == "read_file"


def test_duplicate_image_generation_rejected():
    with pytest.raises(InvalidRequest, match="only one image_generation"):
        parse_responses_request({"input": "draw", "tools": [
            {"type": "image_generation"}, {"type": "image_generation"}]})


def test_empty_tools_do_not_enable_media():
    assert "miora_image_generation" not in parse_responses_request(
        {"input": "hello", "tools": []}).raw


def test_forced_builtin_requires_a_definition():
    with pytest.raises(InvalidRequest, match="requires the tool definition"):
        parse_responses_request({"input": "draw", "tool_choice": {"type": "image_generation"}})
    request = parse_responses_request({"input": "draw", "tools": [{"type": "image_generation"}],
                                     "tool_choice": {"type": "image_generation"}})
    assert request.raw["tool_choice"] == "auto"
    assert request.raw["miora_image_generation_forced"] is True
