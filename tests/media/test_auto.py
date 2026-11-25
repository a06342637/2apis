import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from src.api.deps import ApiKeyPrincipal
from src.media.auto import NAMES, MediaAuto, builtin_options
from src.media.context import get_media_principal, set_media_principal
from src.provider.base import Event, EventKind, Usage
from src.request_context import set_api_key_id, set_credential_id


@contextmanager
def identity(env, *, account="a", principal=True, key="key"):
    set_api_key_id(key)
    set_credential_id(account)
    set_media_principal(env.principal if principal else None)
    try:
        yield
    finally:
        set_media_principal(None)
        set_api_key_id(None)
        set_credential_id(None)


def call(name, *, index=0, call_id="call_image", arguments=None):
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                arguments
                or {
                    "model": "raw-image" if name == NAMES["image"] else "raw-video",
                    "prompt": "draw a cat",
                }
            ),
        },
    }


async def run(env, payload=None, calls=None, *, finish=True):
    seen = []

    async def factory(_credential, sent, model):
        seen.append((sent, model))
        yield Event(kind=EventKind.REASONING, content="thinking")
        yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=10))
        yield Event(kind=EventKind.CONTENT, content="introduction")
        if calls is not None:
            yield Event(kind=EventKind.TOOL_CALLS, tool_calls=calls)
        if finish:
            yield Event(kind=EventKind.FINISH, finish_reason="tool_calls" if calls else "stop")

    output = [
        event
        async for event in MediaAuto(env.manager).stream_chat(
            None, payload or {}, "chat-model", factory
        )
    ]
    return output, seen


def test_context_only_uses_current_authenticated_key():
    principal = ApiKeyPrincipal("root", key_id="key")
    set_media_principal(None)
    assert get_media_principal() is None
    set_media_principal(principal)
    set_api_key_id("other")
    assert get_media_principal() is None
    set_api_key_id("key")
    assert get_media_principal() is principal
    set_media_principal(replace(principal, key_id=""))
    assert get_media_principal() is None
    set_media_principal(None)
    set_api_key_id(None)


@pytest.mark.parametrize("kind", ["image", "video"])
async def test_real_resources_without_returning_internal_tool_calls(media_env, kind):
    env = media_env
    with identity(env):
        output, seen = await run(env, calls=[call(NAMES[kind])])
    tools = seen[0][0]["tools"]
    assert {tool["function"]["name"] for tool in tools} == set(NAMES.values())
    assert seen[0][1] == "chat-model"
    assert any("/v1/media/media_" in (event.content or "") for event in output)
    assert ("![生成图片]" if kind == "image" else "[生成视频]") in output[-2].content
    assert not any(event.kind == EventKind.TOOL_CALLS for event in output)
    assert output[-1].finish_reason == "stop"
    assert len([item for item in env.state.calls if item[0] == "submit"]) == 1
    assert ("key_quota",) in env.state.calls and env.services.inflight.count("a") == 0


async def test_preserves_client_functions_and_renumbers_indices(media_env):
    env = media_env
    client_tool = {"type": "function", "function": {"name": "read_file", "parameters": {}}}
    payload = {"tools": [client_tool]}
    outside = call("read_file", index=1, call_id="outside", arguments={"path": "file.py"})
    with identity(env):
        output, seen = await run(env, payload, [call(NAMES["image"]), outside])
    emitted = next(event for event in output if event.kind == EventKind.TOOL_CALLS)
    assert emitted.tool_calls == [{**outside, "index": 0}]
    assert seen[0][0]["tools"][0] is client_tool and payload["tools"] == [client_tool]
    assert output[-1].finish_reason == "tool_calls"


@pytest.mark.parametrize(
    "choice", ["none", "required", {"type": "function", "function": {"name": "read_file"}}]
)
async def test_tool_choice_none_required_forced_client_do_not_inject(media_env, choice):
    env = media_env
    payload = {
        "tool_choice": choice,
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "miora_image_generation": {"size": "1024x1024"},
        "miora_image_generation_forced": True,
    }
    with identity(env):
        output, seen = await run(env, payload, [call("read_file", arguments={"path": "file.py"})])
    assert seen[0][0] == payload and output[-1].finish_reason == "tool_calls"
    assert env.state.calls == []


@pytest.mark.parametrize(
    "reason", ["principal", "credential", "media", "auto", "key_mismatch", "bad_tools"]
)
async def test_disabled_or_untrusted_scope_is_text_only_except_forced(media_env, reason):
    env = media_env
    payload = {"tools": {} if reason == "bad_tools" else []}
    if reason == "media":
        env.settings.media_enabled = False
    if reason == "auto":
        env.settings.miora_auto_media_enabled = False
    with identity(
        env,
        account=None if reason == "credential" else "a",
        principal=reason != "principal",
        key="other" if reason == "key_mismatch" else "key",
    ):
        output, seen = await run(env, payload)
        assert seen[0][0] == payload and output[-1].finish_reason == "stop"
        with pytest.raises(ValueError, match="disabled or unavailable"):
            await run(
                env,
                {**payload, "miora_image_generation": {}, "miora_image_generation_forced": True},
            )
    assert not env.state.calls


async def test_reserved_client_name_is_never_stolen(media_env):
    env = media_env
    tool = {"type": "function", "function": {"name": NAMES["image"]}}
    with identity(env):
        output, seen = await run(env, {"tools": [tool]}, [call(NAMES["image"])])
        assert any(event.kind == EventKind.TOOL_CALLS for event in output)
        assert [item["function"]["name"] for item in seen[0][0]["tools"]].count(NAMES["image"]) == 1
        assert not [item for item in env.state.calls if item[0] == "submit"]
        with pytest.raises(ValueError, match="conflicts"):
            await run(
                env,
                {
                    "tools": [tool],
                    "miora_image_generation": {},
                    "miora_image_generation_forced": True,
                },
            )


@pytest.mark.parametrize("forced", [False, True])
async def test_builtin_marker_only_forces_when_explicitly_requested(media_env, forced):
    env = media_env
    payload = {
        "miora_image_generation": {"size": "1024x1024", "n": 2, "quality": "auto"},
        "miora_image_generation_forced": forced,
    }
    with identity(env):
        output, seen = await run(env, payload, [call(NAMES["image"])])
    assert len(seen[0][0]["tools"]) == 1
    if forced:
        assert seen[0][0]["tool_choice"]["function"]["name"] == NAMES["image"]
    else:
        assert "tool_choice" not in seen[0][0]
    post = next(item for item in env.state.calls if item[0] == "submit")
    assert post[-1] == {"count": 2, "aspect_ratio": "1:1", "resolution": "1K"}
    assert output[-1].finish_reason == "stop"


@pytest.mark.parametrize("mode", ["empty", "failed"])
async def test_optional_catalog_failure_downgrades_but_forced_rejects(media_env, monkeypatch, mode):
    env = media_env
    if mode == "empty":
        env.state.models = []
    else:

        async def fail(*_args):
            raise OSError("catalog unavailable")

        monkeypatch.setattr(env.manager, "models", fail)
    with identity(env):
        output, seen = await run(env, {"miora_image_generation": {}})
        assert seen[0][0]["tools"] == [] and output[-1].finish_reason == "stop"
        output, seen = await run(env)
        assert seen[0][0]["tools"] == [] and output[-1].finish_reason == "stop"
        with pytest.raises(ValueError):
            await run(env, {"miora_image_generation": {}, "miora_image_generation_forced": True})


@pytest.mark.parametrize(
    "marker",
    [
        None,
        [],
        {"quality": "high"},
        {"background": "transparent"},
        {"output_format": "png"},
        {"partial_images": 1},
        {"partial_images": False},
        {"unexpected": 1},
        {"size": "bad"},
    ],
)
def test_builtin_option_validation_is_explicit(marker):
    with pytest.raises(ValueError):
        builtin_options(marker)


def test_valid_builtin_options():
    assert builtin_options(
        {
            "type": "image_generation",
            "model": "raw-image",
            "size": "1024x1024",
            "quality": "auto",
            "background": "auto",
            "output_format": "auto",
            "partial_images": 0,
        }
    ) == {"model": "raw-image", "size": "1024x1024"}


@pytest.mark.parametrize("failure", ["post", "wait", "arguments"])
async def test_media_failures_do_not_raise_to_engine_or_replay(media_env, monkeypatch, failure):
    env = media_env
    item = call(NAMES["image"])
    if failure == "post":
        env.state.submit_error = OSError("private details")
    elif failure == "wait":

        async def fail(*_args):
            raise TimeoutError("timed out")

        monkeypatch.setattr(env.manager, "wait", fail)
    else:
        item["function"]["arguments"] = "not json"
    with identity(env):
        output, _ = await run(env, calls=[item])
    assert output[-2].content == "媒体生成未完成或失败，生成请求没有自动重试。\n"
    assert output[-1].finish_reason == "stop"
    assert len([item for item in env.state.calls if item[0] == "submit"]) <= 1


async def test_duplicate_internal_calls_are_not_replayed_and_missing_finish(media_env):
    env = media_env
    item = call(NAMES["image"])
    with identity(env):
        output, _ = await run(env, calls=[item, item], finish=False)
        assert not any(event.kind == EventKind.FINISH for event in output)
        assert len([item for item in env.state.calls if item[0] == "submit"]) == 1
        output, _ = await run(env, calls=[])
        assert output[-1].finish_reason == "stop"
        output, _ = await run(env, calls=None)
        assert output[-1].finish_reason == "stop"


async def test_empty_tool_calls_and_non_function_entries_are_tolerated(media_env):
    env = media_env

    async def factory(_credential, _payload, _model):
        yield Event(kind=EventKind.TOOL_CALLS, tool_calls=None)
        yield Event(kind=EventKind.FINISH, finish_reason="stop")

    with identity(env):
        output = [
            event
            async for event in MediaAuto(env.manager).stream_chat(
                None, {"tools": [None, {}, {"function": None}]}, "chat-model", factory
            )
        ]
    assert output[-1].finish_reason == "stop"
