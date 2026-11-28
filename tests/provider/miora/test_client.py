from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from src.provider.base import (
    ErrKind,
    EventKind,
    InvalidModelName,
    Model,
    UpstreamProtocolViolation,
    UpstreamReloginRequired,
)
from src.provider.miora import client as mod
from src.provider.miora.acp import AcpHTTPError
from src.provider.miora.client import (
    MioraCapabilityError,
    MioraClient,
    MioraHTTPError,
    catalog_models,
    cloud_tool_requested,
    default_tier,
    validate_acp_link,
    validate_payload,
)
from src.provider.miora.credential import MioraCredential
from src.provider.miora.tools import MioraToolRequestError

HOST = "https://miora.design"
LINK = {"url": "https://test.sandbox.cloudstudio.club/acp", "token": "short-ticket"}
RAW = {
    "models": {
        "_meta": {
            "codebuddy.ai": {
                "availableModels": [
                    {
                        "id": "hy4flash-free",
                        "name": "Hy4 flash",
                        "maxInputTokens": 100,
                        "maxOutputTokens": 20,
                        "supportsImages": True,
                        "supportsReasoning": True,
                    },
                    {"id": "primary-model"},
                ]
            }
        }
    }
}
TIERS = [
    {
        "category": "recommend",
        "modelId": "hy4flash-free",
        "tierKey": "hy4flash-free",
        "billingFactor": 0,
    },
    {
        "category": "tier",
        "modelId": "primary-model",
        "tierKey": "pro",
        "billingFactor": 3,
        "locales": {"zh-CN": {"name": "进阶"}},
    },
]


def update(kind, content=None, *, session="session", meta=None):
    item = {"sessionUpdate": kind}
    if content is not None:
        item["content"] = {"type": "text", "text": content}
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session, "_meta": meta, "update": item},
    }


class FakeAcp:
    entered = []
    calls = []
    frames = [update("agent_message_chunk", "answer"), {"result": {"stopReason": "end_turn"}}]
    failures = []

    def __init__(self, client, endpoint, token, *, post_client=None, control_client=None):
        self.endpoint = endpoint
        self.token = token

    async def __aenter__(self):
        if self.failures:
            raise self.failures.pop(0)
        self.entered.append((self.endpoint, self.token))
        return self

    async def __aexit__(self, *args):
        self.calls.append(("close", {}))

    async def call(self, method, params):
        self.calls.append((method, params))
        return RAW if method == "session/load" else {}

    async def notify(self, method, params):
        self.calls.append((method, params))

    async def rpc_frames(self, method, params):
        self.calls.append((method, params))
        for frame in self.frames:
            yield frame


@pytest.fixture()
def acp(monkeypatch):
    FakeAcp.entered = []
    FakeAcp.calls = []
    FakeAcp.failures = []
    FakeAcp.frames = [
        update("agent_message_chunk", "answer"),
        {"result": {"stopReason": "end_turn"}},
    ]
    monkeypatch.setattr(mod, "AcpConnection", FakeAcp)

    async def public(host):
        assert host.endswith(".sandbox.cloudstudio.club")

    monkeypatch.setattr(mod, "ensure_public_host", public)
    return FakeAcp


@pytest.fixture()
def api():
    requests = []

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/tiers"):
            data = TIERS
        elif path.endswith("/workflow/list"):
            data = {"list": [{"title": mod.PROJECT_TITLE, "id": "project"}]}
        elif "/runtimes/by-project/" in path:
            data = {"runtimeId": "runtime"}
        elif path.endswith("/runtimes/runtime"):
            data = {"links": {"acpLink": LINK}}
        elif path.endswith("/cloud-agent/sessions"):
            data = {"sessionId": "session"}
        else:
            data = {}
        return httpx.Response(200, json={"code": 0, "data": data})

    return requests, httpx.MockTransport(handle)


async def test_models_and_text_stream_reuse_project_runtime_catalog(acp, api):
    requests, transport = api
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        cred = MioraCredential("long", uid="account")
        assert len(await client.fetch_models(cred)) == 3
        assert len(await client.fetch_models(cred)) == 3
        events = [
            e
            async for e in client.stream_chat(
                cred, {"messages": [{"role": "user", "content": "hi"}]}, "hy4flash-free"
            )
        ]
        assert [e.kind for e in events] == [EventKind.CONTENT, EventKind.FINISH]
        assert events[0].content == "answer"
        assert len([r for r in requests if r.url.path.endswith("/workflow/list")]) == 1
        assert len([r for r in requests if "/by-project/" in r.url.path]) == 1
        assert len([r for r in requests if r.url.path.endswith("/sessions")]) == 2
        assert all(r.headers["Authorization"] == "Bearer long" for r in requests)
        assert any(
            c[0] == "session/set_model" and c[1]["modelId"] == "hy4flash-free" for c in acp.calls
        )
        await client.aclose()


async def test_stream_cold_models_tier_effort_and_other_session_filter(acp, api):
    _, transport = api
    acp.frames = [
        update("agent_message_chunk", "wrong", session="other"),
        update("agent_thought_chunk", "think"),
        update("agent_message_chunk", "answer"),
        {"result": {"stopReason": "end_turn"}},
    ]
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        cred = MioraCredential("long", uid="account", project_id="fixed-project")
        events = [
            e
            async for e in client.stream_chat(
                cred,
                {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"},
                "miora-pro",
            )
        ]
        assert [e.content for e in events if e.content] == ["think", "answer"]
        assert events[-1].finish_reason == "stop"
        with pytest.raises(InvalidModelName):
            _ = [e async for e in client.stream_chat(cred, {"messages": []}, "unknown")]


@pytest.mark.parametrize(
    "reply,tool_call",
    [
        ('{"content":null,"tool_calls":[{"name":"read_file","arguments":{"path":"a"}}]}', True),
        ('{"content":"answer","tool_calls":[]}', False),
    ],
)
async def test_tool_response_bridge(acp, api, reply, tool_call):
    _, transport = api
    acp.frames = [update("agent_message_chunk", reply), {"result": {"stopReason": "end_turn"}}]
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        events = [
            e
            async for e in client.stream_chat(
                MioraCredential("long"),
                {
                    "messages": [{"role": "user", "content": "read a"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"path": {"type": "string"}},
                                },
                            },
                        }
                    ],
                },
                "hy4flash-free",
            )
        ]
        assert events[-1].finish_reason == ("tool_calls" if tool_call else "stop")
        assert any(e.kind == EventKind.TOOL_CALLS for e in events) == tool_call


@pytest.mark.parametrize("failure", ["cloud_tool", "missing_finish", "oversized"])
async def test_fail_closed_and_cancel(acp, api, monkeypatch, failure):
    _, transport = api
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    if failure == "cloud_tool":
        acp.frames = [update("tool_call")]
    elif failure == "missing_finish":
        acp.frames = [update("agent_message_chunk", "partial")]
    else:
        payload["tools"] = [{"type": "function", "function": {"name": "read_file"}}]
        acp.frames = [update("agent_message_chunk", "too big")]
        monkeypatch.setattr(mod, "MAX_TOOL_RESPONSE_CHARS", 1)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        with pytest.raises(UpstreamProtocolViolation):
            _ = [
                e
                async for e in client.stream_chat(MioraCredential("long"), payload, "hy4flash-free")
            ]
        assert any(method == "session/cancel" for method, _ in acp.calls)


async def test_generator_close_cancels_upstream(acp, api):
    _, transport = api
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        stream = client.stream_chat(MioraCredential("long"), {"messages": []}, "hy4flash-free")
        assert (await anext(stream)).kind == EventKind.CONTENT
        await stream.aclose()
        await asyncio.sleep(0)
    assert any(method == "session/cancel" for method, _ in acp.calls)


@pytest.mark.parametrize("reason", ["cancelled", "canceled"])
@pytest.mark.parametrize("partial", [False, True])
async def test_upstream_cancel_preserves_partial_output_but_never_reports_success(
    acp, api, reason, partial
):
    _, transport = api
    acp.frames = ([update("agent_message_chunk", "partial answer")] if partial else []) + [
        {"result": {"stopReason": reason}},
    ]
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        credential = MioraCredential("long", uid="account")
        seen = []
        with pytest.raises(UpstreamProtocolViolation, match="未成功结束"):
            async for event in client.stream_chat(credential, {"messages": []}, "hy4flash-free"):
                seen.append(event)
        assert [event.content for event in seen if event.kind == EventKind.CONTENT] == (
            ["partial answer"] if partial else [])
        assert [event.kind for event in seen if event.kind != EventKind.CONTENT] == [
            EventKind.ERROR,
        ]
        assert seen[-1].error_kind == ErrKind.UPSTREAM_PROTOCOL
        assert not any(event.kind == EventKind.FINISH for event in seen)
        assert any(method == "session/cancel" for method, _ in acp.calls)
        assert client._account(credential).users == 0
        assert not client._account(credential).lock.locked()
        await client.aclose()


@pytest.mark.parametrize("failure", [
    {"result": {"stopReason": "cancelled"}},
    {"error": {"code": -32000, "message": "Bearer private-token"}},
])
async def test_upstream_error_never_releases_buffered_tool_proposals(acp, api, failure):
    _, transport = api
    acp.frames = [update("agent_message_chunk",
        '{"content":null,"tool_calls":[{"name":"constant","arguments":{}}]}'), failure]
    payload = {"messages": [], "tool_choice": "required", "tools": [
        {"type": "function", "function": {"name": "constant", "parameters": {"type": "object"}}},
    ]}
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        seen = []
        with pytest.raises(UpstreamProtocolViolation, match="未成功结束"):
            async for event in client.stream_chat(
                MioraCredential("long"), payload, "hy4flash-free"
            ):
                seen.append(event)
        assert [event.kind for event in seen] == [EventKind.ERROR]
        assert "private-token" not in seen[0].error_message
        assert any(method == "session/cancel" for method, _ in acp.calls)
        await client.aclose()


@pytest.mark.parametrize("current_mode", ["bypassPermissions", "acceptEdits", "default"])
async def test_inference_disables_native_tool_auto_approval(acp, api, monkeypatch, current_mode):
    _, transport = api
    call = acp.call

    async def loaded(self, method, params):
        result = await call(self, method, params)
        if method == "session/load":
            return {**result, "modes": {"currentModeId": current_mode}}
        return result

    monkeypatch.setattr(acp, "call", loaded)
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        events = [e async for e in client.stream_chat(MioraCredential("long"), {
            "messages": [], "tools": [{"type": "function", "function": {"name": "inspect"}}],
        }, "hy4flash-free")]
    assert [(event.kind, event.content) for event in events] == [
        (EventKind.CONTENT, "answer"), (EventKind.FINISH, None),
    ]
    methods = [method for method, _ in acp.calls]
    if current_mode == "default":
        assert "session/set_mode" not in methods
    else:
        assert methods.count("session/set_mode") == 1
        assert methods.index("session/set_mode") < methods.index("session/prompt")
        assert ("session/set_mode", {"sessionId": "session", "modeId": "default"}) in acp.calls


async def test_native_model_without_subagent_tier(acp, api):
    _, transport = api
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        cred = MioraCredential("long")
        account = client._account(cred)
        account.models = [Model("gpt-6.1-sol")]
        account.selections = {"gpt-6.1-sol": ("gpt-6.1-sol", "")}
        events = [e async for e in client.stream_chat(cred, {"messages": []}, "gpt-6.1-sol")]
        assert events[-1].finish_reason == "stop"


async def test_same_account_project_aliases_share_lock_and_effort_is_reset(acp, api):
    requests, transport = api
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http, stream_client=http)
        automatic = MioraCredential("long", uid="same")
        explicit = MioraCredential("long", uid="same", project_id="project")
        different = MioraCredential("long", uid="different", project_id="project")
        account = client._account(automatic)
        alias = client._account(explicit)
        assert account.lock is alias.lock
        assert account.lock is not client._account(different).lock
        account.models = alias.models = [Model("native")]
        account.selections = alias.selections = {"native": ("native", "")}
        account.default_tier = alias.default_tier = "observed-default"
        for credential, effort in ((automatic, "high"), (explicit, None)):
            payload = {"messages": [], "reasoning_effort": effort}
            _ = [event async for event in client.stream_chat(credential, payload, "native")]
        effort_bodies = [
            json.loads(request.content)
            for request in requests
            if request.url.path.endswith("/update-effort")
        ]
        assert effort_bodies == [{"effort": "high"}, {"effort": "medium"}]
        tier_bodies = [
            json.loads(request.content)
            for request in requests
            if request.url.path.endswith("/update-subagent-models")
        ]
        assert tier_bodies == [{"tierKey": "observed-default"}] * 2


async def test_idle_project_alias_eviction_keeps_shared_identity_lock(monkeypatch):
    monkeypatch.setattr(mod, "MAX_ACCOUNTS", 1)
    client = MioraClient()
    first = MioraCredential("long", uid="same")
    alias = MioraCredential("long", uid="same", project_id="project")
    first_account = client._account(first)
    async with client._using_account(alias) as alias_account:
        assert alias_account.lock is first_account.lock
        assert client._identity_locks["same"] is alias_account.lock
        assert len(client._accounts) == 1
    await client.aclose()


@pytest.mark.parametrize(
    "tiers,expected",
    [
        (None, ""),
        ([], ""),
        ([None, {}], ""),
        ([{"isDefault": True, "tierKey": ""}], ""),
        ([{"isDefault": True, "tierKey": "observed"}], "observed"),
    ],
)
def test_default_tier_uses_only_observed_catalog(tiers, expected):
    assert default_tier(tiers) == expected


async def test_account_lru_pins_active_users_and_locks(monkeypatch):
    monkeypatch.setattr(mod, "MAX_ACCOUNTS", 1)
    client = MioraClient()
    a, b, c = (MioraCredential("token", uid=uid) for uid in ("a", "b", "c"))
    async with client._using_account(a) as first:
        async with client._using_account(b):
            assert len(client._accounts) == 2
            assert client._account(a) is first
        assert len(client._accounts) == 1
    old = client._account(b)
    await old.lock.acquire()
    async with client._using_account(c):
        assert client._account(b) is old
    old.lock.release()
    client._trim_accounts()
    assert len(client._accounts) == 1
    await client.aclose()


@pytest.mark.parametrize(
    "key,value",
    [
        ("temperature", 0.5),
        ("top_p", 0.2),
        ("presence_penalty", 1),
        ("frequency_penalty", 1),
        ("stop", []),
        ("logprobs", False),
        ("top_logprobs", 0),
        ("logit_bias", {}),
        ("seed", 0),
        ("response_format", {}),
        ("reasoning_effort", "ultra"),
    ],
)
def test_unsupported_payload_is_rejected(key, value):
    with pytest.raises(InvalidModelName):
        validate_payload({key: value})


def test_default_payload_parameters():
    validate_payload(
        {
            "temperature": 1,
            "top_p": 1,
            "presence_penalty": 0,
            "frequency_penalty": None,
            "stop": None,
            "reasoning_effort": "low",
            "modalities": ["text"],
            "audio": None,
            "prediction": None,
            "metadata": {},
            "user": "client-user",
            "store": False,
        }
    )


@pytest.mark.parametrize(
    "extra,kind",
    [
        ({"modalities": ["audio"]}, ErrKind.CAPABILITY),
        ({"modalities": ["text", "audio"]}, ErrKind.CAPABILITY),
        ({"modalities": []}, ErrKind.CAPABILITY),
        ({"modalities": "text"}, ErrKind.INVALID),
        ({"modalities": [None]}, ErrKind.INVALID),
        ({"audio": {}}, ErrKind.CAPABILITY),
        ({"prediction": {"type": "content", "content": "x"}}, ErrKind.INVALID),
    ],
)
async def test_unsupported_outputs_are_rejected_before_network(extra, kind):
    calls = []

    def unexpected(request):
        calls.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
        client = MioraClient(short_client=http, stream_client=http)
        with pytest.raises((MioraCapabilityError, MioraToolRequestError)) as caught:
            _ = [
                event
                async for event in client.stream_chat(
                    MioraCredential("long"), {"messages": [], **extra}, "hy4flash-free"
                )
            ]
        assert caught.value.kind() == kind
    assert calls == []


@pytest.mark.parametrize(
    "content",
    [None, "plain", [], [{"type": "text", "text": "a"}, {"type": "input_text", "text": "b"}]],
)
def test_text_content_is_supported_for_user_and_tool_results(content):
    validate_payload(
        {"messages": [{"role": "user", "content": content}, {"role": "tool", "content": content}]}
    )


@pytest.mark.parametrize("kind", ["image_url", "image", "input_image", "input_audio", "file"])
async def test_non_text_content_rejected_before_any_network(kind):
    calls = []

    def unexpected(request):
        calls.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
        client = MioraClient(short_client=http, stream_client=http)
        payload = {"messages": [{"role": "user", "content": [{"type": kind}]}]}
        with pytest.raises(MioraCapabilityError) as caught:
            _ = [
                event
                async for event in client.stream_chat(
                    MioraCredential("long"), payload, "hy4flash-free"
                )
            ]
        assert caught.value.kind() == ErrKind.CAPABILITY
    assert calls == []


@pytest.mark.parametrize(
    "messages",
    [
        None,
        {},
        [None],
        ["invalid"],
        [{"content": True}],
        [{"content": {"type": "text", "text": "x"}}],
        [{"content": [None]}],
        [{"content": [{}]}],
        [{"content": [{"type": 1}]}],
        [{"content": [{"type": "text"}]}],
        [{"content": [{"type": "input_text", "text": 1}]}],
    ],
)
def test_malformed_content_is_invalid_request(messages):
    with pytest.raises(MioraToolRequestError) as caught:
        validate_payload({"messages": messages})
    assert caught.value.kind() == ErrKind.INVALID


@pytest.mark.parametrize(
    "link",
    [
        None,
        {},
        {"url": "https://x:invalid/acp", "token": "x"},
        {"url": "https://[invalid/acp", "token": "x"},
        {"url": "http://x.sandbox.cloudstudio.club/acp", "token": "x"},
        {"url": "https://evil.example/acp", "token": "x"},
        {"url": "https://user:password@x.sandbox.cloudstudio.club/acp", "token": "x"},
        {"url": "https://x.sandbox.cloudstudio.club:443/wrong", "token": "x"},
        {"url": "https://x.sandbox.cloudstudio.club:444/acp", "token": "x"},
        {"url": "https://x.sandbox.cloudstudio.club/acp?x=1", "token": "x"},
        {"url": "https://x.sandbox.cloudstudio.club/acp#x", "token": "x"},
        {"url": "https://x.sandbox.cloudstudio.club/acp", "token": ""},
    ],
)
async def test_untrusted_runtime_link(link):
    with pytest.raises(UpstreamProtocolViolation):
        await validate_acp_link(link)


async def test_official_runtime_link(acp):
    assert await validate_acp_link(LINK) == (LINK["url"], LINK["token"])


@pytest.mark.parametrize(
    "data,expect",
    [
        ({"totalAmount": "10", "usedAmount": "3"}, (7, 10)),
        ({"totalAmount": "10", "usedAmount": "20"}, (0, 10)),
        ({}, None),
        ({"totalAmount": None, "usedAmount": "3"}, None),
        ({"totalAmount": "bad", "usedAmount": "3"}, None),
        ({"totalAmount": "nan", "usedAmount": "3"}, None),
        ({"totalAmount": "10", "usedAmount": "inf"}, None),
        ({"totalAmount": "-1", "usedAmount": "0"}, None),
        ({"totalAmount": "10", "usedAmount": "-1"}, None),
    ],
)
async def test_quota(data, expect):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": data}))
    ) as http:
        client = MioraClient(short_client=http)
        if expect is None:
            with pytest.raises(UpstreamProtocolViolation):
                await client.probe_quota(MioraCredential("long"))
        else:
            quota = await client.probe_quota(MioraCredential("long"))
            assert (quota.remaining, quota.total) == expect
            assert quota.probed_at


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"code": 1, "message": "not ok"}),
        httpx.Response(500),
    ],
)
async def test_rest_errors(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as http:
        client = MioraClient(short_client=http)
        with pytest.raises((MioraHTTPError, UpstreamProtocolViolation)):
            await client._request(MioraCredential("long"), "GET", "/test")


@pytest.mark.parametrize(
    "data", [{}, {"access_token": "new"}, [], {"access_token": "", "refresh_token": "new-refresh"}]
)
async def test_refresh_requires_complete_rotation(data):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    ) as http:
        client = MioraClient(short_client=http)
        with pytest.raises(UpstreamProtocolViolation):
            await client.refresh_token(MioraCredential("long", refresh_token="old-refresh"))


@pytest.mark.parametrize("status", [400, 401, 503])
async def test_refresh_terminal_and_retryable_errors(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status))
    ) as http:
        client = MioraClient(short_client=http)
        with pytest.raises(UpstreamReloginRequired if status < 500 else MioraHTTPError):
            await client.refresh_token(MioraCredential("long", refresh_token="old-refresh"))
        with pytest.raises(UpstreamReloginRequired):
            await client.refresh_token(MioraCredential("long"))


async def test_refresh_expiry_rotation_preserves_identity_and_project():
    claims = base64.urlsafe_b64encode(json.dumps({"sub": "same", "exp": 2000}).encode())
    token = "header." + claims.decode().rstrip("=") + ".signature"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"access_token": token, "refresh_token": "new-refresh"}
            )
        )
    ) as http:
        client = MioraClient(short_client=http)
        data = await client.refresh_token(
            MioraCredential("old", "old-refresh", "same", 100, "project")
        )
        assert data["expires_at"] == 2000
        assert data["project_id"] == "project"
        assert data["uid"] == "same"
        assert data["refresh_token"] == "new-refresh"


@pytest.mark.parametrize("failures,status", [([401], None), ([403], 403), ([401, 401], 401)])
async def test_short_token_reconnect_once(acp, failures, status):
    acp.failures = [AcpHTTPError(code, b"") for code in failures]
    client = MioraClient()
    cred = MioraCredential("long")
    account = client._account(cred)
    refreshed = []

    async def runtime(c, a):
        refreshed.append(True)
        return LINK["url"], "fresh"

    client._runtime = runtime
    try:
        if status is None:
            async with client._connection(cred, account, LINK["url"], "stale"):
                pass
            assert acp.entered[-1][1] == "fresh"
        else:
            with pytest.raises(AcpHTTPError) as caught:
                async with client._connection(cred, account, LINK["url"], "stale"):
                    pytest.fail("must fail")
            assert caught.value.status == status
        assert len(refreshed) == (1 if failures[0] == 401 else 0)
    finally:
        await client.aclose()


@pytest.mark.parametrize("logical_error", [False, True])
async def test_create_project_and_runtime_after_lookup_404(acp, logical_error):
    requests = []

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/workflow/list"):
            data = {"list": [None, {"title": "other"}, {"title": mod.PROJECT_TITLE, "id": ""}]}
        elif path.endswith("/workflow/create"):
            data = "new-project"
        elif "/by-project/" in path:
            if logical_error:
                return httpx.Response(
                    200,
                    json={
                        "code": 10005,
                        "failed": True,
                        "data": None,
                        "message": "Not Found",
                        "httpStatus": 404,
                    },
                )
            return httpx.Response(404)
        elif path.endswith("/cloud-agent/runtimes"):
            assert json.loads(request.content)["projectId"] == "new-project"
            data = {"id": "new-runtime", "links": {"acpLink": LINK}}
        else:
            pytest.fail(f"Unexpected request {path}")
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MioraClient(short_client=http)
        account = client._account(MioraCredential("long"))
        result = await client._runtime(MioraCredential("long"), account)
        assert result == (LINK["url"], LINK["token"])
        assert account.project_id == "new-project"
        assert account.runtime_id == "new-runtime"


@pytest.mark.parametrize("which", ["project", "runtime", "session", "lookup-error"])
async def test_required_control_identifiers(acp, which):
    def handle(request):
        path = request.url.path
        if which == "lookup-error":
            return httpx.Response(503)
        if path.endswith("/workflow/list"):
            data = {"list": []}
        elif path.endswith("/workflow/create"):
            data = "" if which == "project" else "project"
        elif "/by-project/" in path:
            data = {}
        elif path.endswith("/cloud-agent/runtimes"):
            data = {"links": {"acpLink": LINK}}
        else:
            data = {}
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MioraClient(short_client=http)
        cred = MioraCredential("long", project_id="p" if which != "project" else "")
        account = client._account(cred)
        with pytest.raises((MioraHTTPError, UpstreamProtocolViolation)):
            if which == "session":
                await client._session(cred, account)
            else:
                await client._runtime(cred, account)


@pytest.mark.parametrize(
    "loaded",
    [
        None,
        {},
        {"models": {}},
        {"models": {"_meta": {}}},
        {"models": {"_meta": {"codebuddy.ai": {"availableModels": []}}}},
        {"models": {"_meta": {"codebuddy.ai": {"availableModels": [None, {}]}}}},
    ],
)
def test_invalid_catalog(loaded):
    with pytest.raises(UpstreamProtocolViolation):
        catalog_models(loaded, None)


def test_catalog_metadata_and_alias_validation():
    loaded = json.loads(json.dumps(RAW))
    items = loaded["models"]["_meta"]["codebuddy.ai"]["availableModels"]
    items.extend(
        [None, {}, items[0], {"id": "extra", "maxInputTokens": False, "maxOutputTokens": -1}]
    )
    tiers = TIERS + [
        None,
        {"category": "tier", "modelId": "unknown", "tierKey": "missing"},
        {"category": "tier", "modelId": "hy4flash-free", "tierKey": ""},
        {
            "category": "tier",
            "modelId": "extra",
            "tierKey": "extra",
            "billingFactor": -1,
            "locales": {"zh-CN": {}},
        },
    ]
    models, selections = catalog_models(loaded, tiers)
    assert len(models) == 5
    assert models[0].supports_images is False
    assert models[0].max_input_tokens == 100
    assert models[-1].name == "extra"
    assert selections["miora-pro"] == ("primary-model", "pro")
    assert catalog_models(RAW, None)[0][0].credit_rate is None
    assert mod._positive_int(None) is None
    for rate in (True, None, float("nan"), -1):
        assert mod._rate(rate) is None
    assert mod._rate(0) == 0


def test_public_catalog_keeps_all_routes_and_raw_ids():
    from src.api.model_merge import _build_aliases, _merge_provider, _sort_key, _visible

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/public-catalog.json").read_text(encoding="utf-8")
    )
    models, selections = catalog_models(fixture["loaded"], fixture["tiers"])
    assert len(models) == 35
    patterns = ("custom_model_*", "*sub*agent*", "summary", "browser_use_*", "file_search_agent")
    grouped = {}
    _merge_provider(
        grouped, "miora", _visible({model.id.lower(): model for model in models}, patterns)
    )
    entries = sorted(grouped.values(), key=_sort_key)
    aliases = _build_aliases(entries)
    assert len(entries) == 35
    assert len({entry["id"] for entry in entries}) == 35
    assert aliases["miora"]["hy4flash"] == "hy4flash"
    assert aliases["miora"]["hy4flash-free"] == "hy4flash-free"
    assert selections["hy4flash"][0] == "hy4flash"
    assert selections["hy4flash-free"][0] == "hy4flash-free"
    assert all(aliases["miora"][model.id.lower()] == model.id for model in models)


@pytest.mark.parametrize("logical,header", [
    (False, True), (True, True), (False, False), (True, False)])
async def test_429_preserves_account_retry_hints(logical, header, monkeypatch):
    monkeypatch.setattr("src.provider.base.time.time", lambda: 1000)
    body = {"code": 10005, "httpStatus": 429,
            "message": "usage reset at 1970-01-01 00:17:10 UTC+0"}
    def handle(request):
        return httpx.Response(200 if logical else 429, json=body,
                              headers={"Retry-After": "20"} if header else {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MioraClient(short_client=http)
        with pytest.raises(MioraHTTPError) as caught:
            await client._request(MioraCredential("synthetic"), "GET", "/test")
        assert caught.value.kind() == ErrKind.SOFT
        assert caught.value.retry_after_seconds == (20 if header else 30)
        assert caught.value.retry_at == (1020 if header else 1030)


async def test_token_refresh_429_preserves_hint_without_rotating_credentials(monkeypatch):
    monkeypatch.setattr("src.provider.base.time.time", lambda: 1000)
    def handle(request):
        return httpx.Response(429, headers={"Retry-After": "35"}, content=b"rate limited")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MioraClient(short_client=http)
        credential = MioraCredential("synthetic", "synthetic-refresh")
        with pytest.raises(MioraHTTPError) as caught:
            await client.refresh_token(credential)
        assert caught.value.kind() == ErrKind.SOFT
        assert caught.value.retry_at == 1035 and caught.value.retry_after_seconds == 35
        assert credential.access_token == "synthetic"
        assert credential.refresh_token == "synthetic-refresh"


@pytest.mark.parametrize(
    "hint,expected",
    [
        (None, 400),
        (True, 400),
        ("404", 400),
        (200, 400),
        (399, 400),
        (400, 400),
        (404, 404),
        (500, 500),
        (599, 599),
        (600, 400),
    ],
)
async def test_logical_rest_errors_preserve_valid_http_status(hint, expected):
    def handle(request):
        return httpx.Response(
            200,
            json={
                "code": 10005,
                "failed": True,
                "data": None,
                "message": "Not Found",
                "httpStatus": hint,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MioraClient(short_client=http)
        with pytest.raises(MioraHTTPError) as error:
            await client._request(MioraCredential("synthetic-token"), "GET", "/test")
        assert error.value.status == expected


@pytest.mark.parametrize("data", [[], {}, {"list": None}])
async def test_bad_project_listing_does_not_create_project(data):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MioraClient(short_client=http)
        cred = MioraCredential("long")
        with pytest.raises(UpstreamProtocolViolation, match="项目列表"):
            await client._project(cred, client._account(cred))
    assert len(calls) == 1 and calls[0].method == "GET"


@pytest.mark.parametrize("mode", ["lookup", "create", "runtime", "session"])
async def test_malformed_control_object(mode, acp):
    def handle(request):
        if mode == "create" and "/by-project/" in request.url.path:
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(200, json={"data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MioraClient(short_client=http)
        cred = MioraCredential("long", project_id="project")
        account = client._account(cred)
        if mode == "runtime":
            account.runtime_id = "runtime"
        with pytest.raises(UpstreamProtocolViolation):
            if mode == "session":
                await client._session(cred, account)
            else:
                await client._runtime(cred, account)


@pytest.mark.parametrize("locales", [None, {"zh-CN": None}])
def test_catalog_bad_locale_falls_back(locales):
    tiers = [
        {
            "category": "tier",
            "modelId": "primary-model",
            "tierKey": "pro",
            "locales": locales,
            "billingFactor": 1,
        }
    ]
    assert catalog_models(RAW, tiers)[0][-1].name == "pro"


@pytest.mark.parametrize(
    "frame,expected",
    [
        ({}, False),
        ({"method": "session/update", "params": None}, False),
        (update("tool_call"), True),
        (update("agent_message_chunk", "x"), False),
        (update("tool_call", meta={"codebuddy.ai": {"mode": "history"}}), False),
        ({"method": "session/update", "params": {"update": None}}, False),
    ],
)
def test_cloud_tool_is_not_client_tool(frame, expected):
    assert cloud_tool_requested(frame) == expected
