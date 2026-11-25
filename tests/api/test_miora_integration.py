"""Miora 多账号与两套客户端出口的离线端到端契约。"""

from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from src.provider.base import Event, EventKind, Model, Quota, UpstreamReloginRequired, Usage
from src.provider.miora import MioraProvider
from src.provider.miora.client import MioraHTTPError, validate_payload
from src.provider.miora.credential import MioraCredential
from src.provider.miora.tools import parse_response, prepare_prompt
from src.tasks import TaskReport
from src.tasks.quota_probe import QuotaProbeTask
from src.tasks.refresh import RefreshTask
from tests.helpers.chat import jwt_token
from tests.helpers.constants import SECRET
from tests.helpers.startup import wait_for_startup


class LocalMioraClient:
    """替换网络边界，保留真实 provider、凭证与工具 JSON 桥接。"""

    def __init__(self):
        self.calls = []
        self.refreshes = []
        self.closed = False

    async def fetch_models(self, credential):
        assert credential.access_token
        return [
            Model(id="glm-5.3", name="GLM-5.3", supports_tool_call=True),
            Model(id="gpt-6-astra", name="GPT-6 Astra", supports_tool_call=True),
        ]

    async def probe_quota(self, credential):
        assert credential.access_token
        return Quota(remaining=250, total=250, probed_at=int(time.time()))

    async def refresh_token(self, credential):
        self.refreshes.append(credential)
        return MioraCredential.from_dict(
            {
                **credential.to_dict(),
                "access_token": token(credential.uid, "refreshed"),
                "refresh_token": "rotated-refresh",
            }
        ).to_dict()

    async def stream_chat(self, credential, payload, model):
        validate_payload(payload)
        prompt = prepare_prompt(payload)
        self.calls.append((credential.uid, model, copy.deepcopy(payload), prompt))
        tool_result = any(message["role"] == "tool" for message in payload["messages"])
        if payload.get("tools"):
            response = {
                "content": "file summary" if tool_result else None,
                "tool_calls": []
                if tool_result
                else [{"name": "read_file", "arguments": {"path": "main.py"}}],
            }
            events = parse_response(json.dumps(response), payload)
        else:
            events = parse_response(f"reply from {model}", payload)
        for event in events:
            yield event
        yield Event(
            kind=EventKind.USAGE, usage=Usage(input_tokens=10, output_tokens=4, cached_tokens=2)
        )
        reason = (
            "tool_calls" if any(event.kind == EventKind.TOOL_CALLS for event in events) else "stop"
        )
        yield Event(kind=EventKind.FINISH, finish_reason=reason)

    async def aclose(self):
        self.closed = True


def token(uid: str, marker: str, *, exp: int | None = None) -> str:
    return jwt_token(
        exp if exp is not None else int(time.time()) + 15 * 86400,
        payload_extra={"sub": uid, "jti": marker, "iss": "https://miora.design"},
    )


def import_account(client, uid="account-1", marker="initial", **extra):
    response = client.post(
        "/api/credentials",
        json={
            "provider": "miora",
            "credential": {"authToken": token(uid, marker), "refreshToken": f"refresh-{uid}"},
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def miora_app(tmp_path):
    network = LocalMioraClient()
    provider = MioraProvider(client=network)
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path), ADMIN_USERNAMES="root"
    )
    app = build_app(settings, providers={"miora": provider})
    with TestClient(app) as client:
        wait_for_startup(app, client)
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        yield app, client, network
    assert network.closed


def api_headers(app):
    return {"Authorization": "Bearer " + app.state.api_keys.create("root", "Miora test")["api_key"]}


def wait_for_probes(app, client):
    async def wait():
        await asyncio.wait_for(asyncio.gather(*list(app.state.pending_probes)), timeout=5)

    client.portal.call(wait)


def test_account_import_merge_batch_and_redaction(miora_app):
    app, client, _network = miora_app
    first = import_account(client, nickname="first", tags=["team-a"])
    app.state.credentials.set_enabled(first["id"], False)
    same = import_account(client, marker="replacement", nickname="updated")
    second = import_account(client, "account-2", nickname="second")
    assert not first["merged"] and same["merged"]
    assert same["id"] == first["id"] and second["id"] != first["id"]
    stored = app.state.credentials.credential_data(first["id"])
    assert stored["access_token"] == token("account-1", "replacement", exp=stored["expires_at"])
    assert stored["uid"] == "account-1"
    listed = client.get("/api/credentials")
    assert listed.status_code == 200
    rows = {item["id"]: item for item in listed.json()["credentials"]}
    assert rows[first["id"]]["enabled"] == 0
    assert rows[first["id"]]["tags"] == ["team-a"]
    assert rows[first["id"]]["nickname"] == "updated"
    assert stored["access_token"] not in listed.text
    assert stored["refresh_token"] not in listed.text

    third = {
        "provider": "miora",
        "credential": {"authToken": token("account-3", "batch"), "refreshToken": "third-refresh"},
    }
    batch = client.post(
        "/api/credentials/batch-import",
        json={
            "items": [
                third,
                third,
                {"provider": "miora", "credential": {}},
                {
                    "provider": "miora",
                    "credential": {
                        "authToken": token("account-4", "batch"),
                        "refreshToken": "fourth-refresh",
                    },
                },
            ]
        },
    )
    assert batch.status_code == 200, batch.text
    assert [row["index"] for row in batch.json()["created"]] == [0, 3]
    assert [row["index"] for row in batch.json()["skipped"]] == [1]
    assert [row["index"] for row in batch.json()["failed"]] == [2]
    assert "authToken" in batch.json()["failed"][0]["reason"]
    assert len(app.state.credentials.candidates(["miora"])) == 4


def test_refresh_rotates_tokens_in_encrypted_repository(miora_app):
    app, client, network = miora_app
    now = int(time.time())
    previous = token("soon", "old", exp=now + 60)
    old = app.state.credentials.add(
        provider="miora",
        credential_data={"access_token": previous, "refresh_token": "old-refresh", "uid": "soon"},
    )
    task = RefreshTask(
        app.state.credentials, app.state.services.registry, skew_seconds=120, now=lambda: now
    )
    report = client.portal.call(task.run_once)
    assert report.succeeded == 1
    assert len(network.refreshes) == 1
    updated = app.state.credentials.credential_data(old)
    assert updated["access_token"] != previous
    assert updated["refresh_token"] == "rotated-refresh"
    assert updated["uid"] == "soon" and updated["expires_at"] > now + 120
    cipher = (
        app.state.credentials._db.connect()
        .execute("SELECT data_enc FROM credentials WHERE id=?", (old,))
        .fetchone()[0]
    )
    assert previous.encode() not in cipher
    assert updated["access_token"].encode() not in cipher
    assert b"rotated-refresh" not in cipher
    assert client.portal.call(task.run_once).attempted == 0


@pytest.mark.parametrize("batch", [False, True])
def test_same_account_manual_login_waits_for_refresh_and_keeps_new_tokens(
    miora_app, batch, monkeypatch
):
    app, client, network = miora_app
    now = int(time.time())
    old_token = token("same-account", "old", exp=now + 60)
    initial = {
        "id": app.state.credentials.add(
            provider="miora",
            credential_data={
                "access_token": old_token,
                "refresh_token": "old-refresh",
                "uid": "same-account",
            },
            tags=["original"],
        )
    }
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def delayed_refresh(credential):
        entered.set()
        await proceed.wait()
        return {
            "access_token": token(credential.uid, "old-rotation"),
            "refresh_token": "old-rotation-refresh",
            "uid": credential.uid,
        }

    network.refresh_token = delayed_refresh
    task = RefreshTask(
        app.state.credentials, app.state.services.registry, skew_seconds=120, now=lambda: now
    )
    refresh = client.portal.start_task_soon(task.run_once)
    client.portal.call(entered.wait)
    import_entered = asyncio.Event()
    lookup = app.state.credentials.find_login_credential

    def track_lookup(*args, **kwargs):
        import_entered.set()
        return lookup(*args, **kwargs)

    monkeypatch.setattr(app.state.credentials, "find_login_credential", track_lookup)
    new_token = token("same-account", "manual-login")
    item = {
        "provider": "miora",
        "credential": {"authToken": new_token, "refreshToken": "manual-refresh"},
        "nickname": "latest",
        "tags": ["new-tag"],
    }

    async def import_while_refreshing():
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={"coding2api_session": client.cookies.get("coding2api_session")},
        ) as async_client:
            pending = asyncio.create_task(
                async_client.post(
                    "/api/credentials/batch-import" if batch else "/api/credentials",
                    json={"items": [item]} if batch else item,
                )
            )
            try:
                await asyncio.wait_for(import_entered.wait(), timeout=5)
                assert not pending.done()
                assert (
                    app.state.credentials.credential_data(initial["id"])["access_token"]
                    == old_token
                )
            finally:
                proceed.set()
                response = await pending
            return response

    try:
        response = client.portal.call(import_while_refreshing)
    finally:
        # 断言失败也释放人工控制的刷新挂起点，测试结束不能遗留后台任务。
        client.portal.call(proceed.set)
    assert refresh.result().succeeded == 1
    assert response.status_code == 200, response.text
    saved = app.state.credentials.credential_data(initial["id"])
    assert saved["access_token"] == new_token
    assert saved["refresh_token"] == "manual-refresh"
    assert saved["uid"] == "same-account"
    rows = app.state.credentials.list_all()
    assert len(rows) == 1 and rows[0]["tags"] == ["original"]
    assert rows[0]["nickname"] == "latest"


@pytest.mark.parametrize("outcome", ["valid", "expired-refresh", "unavailable"])
def test_import_expired_access_refreshes_before_quota_with_shared_task(miora_app, outcome):
    app, client, network = miora_app
    now = int(time.time())
    old = token("expired-account", "old", exp=now - 1)
    new = token("expired-account", "new")
    probes = []

    async def refresh(credential):
        network.refreshes.append(credential)
        if outcome == "expired-refresh":
            raise UpstreamReloginRequired("synthetic invalid refresh")
        if outcome == "unavailable":
            raise MioraHTTPError(503, b"temporary upstream failure")
        return {"access_token": new, "refresh_token": "rotated", "uid": credential.uid}

    async def probe(credential):
        probes.append(credential.access_token)
        if credential.expires_at <= now:
            raise MioraHTTPError(401, b"expired access")
        saved = app.state.credentials.candidates(["miora"])[0].credential_id
        assert (
            app.state.credentials.credential_data(saved)["access_token"] == credential.access_token
        )
        return Quota(remaining=250, total=250, probed_at=now)

    network.refresh_token, network.probe_quota = refresh, probe
    response = client.post(
        "/api/credentials",
        json={
            "provider": "miora",
            "credential": {"authToken": old, "refreshToken": "synthetic-refresh"},
        },
    )
    assert response.status_code == 200, response.text
    wait_for_probes(app, client)
    assert len(network.refreshes) == 1
    row = app.state.credentials.list_all()[0]
    data = app.state.credentials.credential_data(response.json()["id"])
    if outcome == "valid":
        assert data["access_token"] == new and data["refresh_token"] == "rotated"
        assert probes == [new] and row["disabled"] == 0 and row["quota_remaining"] == 250
    else:
        assert data["access_token"] == old and probes == []
        assert row["disabled"] == (1 if outcome == "expired-refresh" else 0)


def test_fresh_import_does_not_rotate_long_term_tokens(miora_app):
    app, client, network = miora_app
    import_account(client)
    wait_for_probes(app, client)
    assert network.refreshes == []
    assert app.state.credentials.list_all()[0]["quota_remaining"] == 250


def test_miora_probe_without_runner_does_not_use_expired_snapshot(miora_app, monkeypatch):
    app, client, network = miora_app
    monkeypatch.delattr(app.state, "task_runner")
    import_account(client)
    wait_for_probes(app, client)
    assert network.refreshes == []
    assert app.state.credentials.list_all()[0]["quota_remaining"] is None


def test_miora_probe_skips_credential_removed_after_refresh(miora_app, monkeypatch):
    app, client, network = miora_app

    async def remove(credential_id):
        app.state.credentials.delete(credential_id)
        return TaskReport()

    monkeypatch.setattr(app.state.task_runner, "refresh_credential", remove)
    import_account(client)
    wait_for_probes(app, client)
    assert network.refreshes == [] and app.state.credentials.list_all() == []


def test_paused_account_is_maintained_without_becoming_api_selectable(miora_app):
    app, client, network = miora_app
    now = int(time.time())
    old = token("paused", "old", exp=now - 1)
    account = app.state.credentials.add(provider="miora", credential_data={
        "access_token": old, "refresh_token": "synthetic-refresh", "uid": "paused"})
    app.state.credentials.set_enabled(account, False)
    report = client.portal.call(app.state.task_runner.refresh_credential, account)
    assert report.succeeded == 1 and len(network.refreshes) == 1
    assert app.state.credentials.credential_data(account)["access_token"] != old
    quota_task = QuotaProbeTask(app.state.credentials, app.state.services.registry)
    assert client.portal.call(quota_task.run_once).succeeded == 1
    row = app.state.credentials.list_all()[0]
    assert row["enabled"] == 0 and row["quota_remaining"] == 250
    response = client.post("/v1/chat/completions", headers=api_headers(app), json={
        "model": "glm-5.3@miora", "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 503
    assert network.calls == []


def test_models_selection_and_account_pool_isolation(miora_app):
    app, client, network = miora_app
    one = import_account(client)
    import_account(client, "account-2")
    headers = api_headers(app)
    listing = client.get("/v1/models", headers=headers)
    assert listing.status_code == 200
    entries = listing.json()["data"]
    assert {entry["id"] for entry in entries} == {"glm-5.3", "gpt-6-astra"}
    assert all(entry["providers"] == ["miora"] for entry in entries)
    for model in ("glm-5.3", "gpt-6-astra"):
        response = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": model + "@miora", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == f"reply from {model}"
        assert network.calls[-1][1] == model
    app.state.credentials.set_enabled(one["id"], False)
    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "glm-5.3@miora", "messages": [{"role": "user", "content": "next account"}]},
    )
    assert response.status_code == 200
    assert network.calls[-1][0] == "account-2"


def test_429_cools_whole_account_and_retries_another_account(miora_app):
    app, client, network = miora_app
    import_account(client)
    import_account(client, "account-2")
    attempted = []
    original = network.stream_chat
    retry_at = int(time.time()) + 300

    async def limited_once(credential, payload, model):
        attempted.append(credential.uid)
        if len(attempted) == 1:
            raise MioraHTTPError(429, b"insufficient credit in burst window", retry_at=retry_at)
        async for event in original(credential, payload, model):
            yield event

    network.stream_chat = limited_once
    headers = api_headers(app)
    for model in ("glm-5.3", "gpt-6-astra"):
        response = client.post("/v1/chat/completions", headers=headers, json={
            "model": f"{model}@miora", "messages": [{"role": "user", "content": "hello"}]})
        assert response.status_code == 200, response.text

    assert len(attempted) == 3 and attempted[0] != attempted[1] == attempted[2]
    cooled = next(candidate for candidate in app.state.credentials.candidates(["miora"])
                  if app.state.credentials.credential_data(candidate.credential_id)["uid"]
                  == attempted[0])
    row = next(row for row in app.state.credentials.list_all()
               if row["id"] == cooled.credential_id)
    assert row["cooling_kind"] == "soft" and cooled.cooling_until == retry_at
    assert cooled.err_count == 0 and not cooled.disabled and not cooled.model_cooldowns


@pytest.mark.parametrize("anthropic", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_two_protocol_text_and_usage(miora_app, anthropic, stream):
    app, client, _network = miora_app
    import_account(client)
    headers = api_headers(app)
    body = {
        "model": "glm-5.3@miora",
        "stream": stream,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "write code"}],
    }
    path = "/v1/messages" if anthropic else "/v1/chat/completions"
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    if stream:
        assert "reply from glm-5.3" in response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        assert (
            ("event: message_stop" in response.text)
            if anthropic
            else ("data: [DONE]" in response.text)
        )
    elif anthropic:
        assert response.json()["content"] == [{"type": "text", "text": "reply from glm-5.3"}]
        assert response.json()["usage"] == {
            "input_tokens": 8,
            "output_tokens": 4,
            "cache_read_input_tokens": 2,
        }
    else:
        assert response.json()["choices"][0]["message"]["content"] == "reply from glm-5.3"
        assert response.json()["usage"]["prompt_tokens"] == 10
        assert response.json()["usage"]["completion_tokens"] == 4


@pytest.mark.parametrize("anthropic", [False, True])
def test_text_only_channel_rejects_images_before_chat(miora_app, anthropic):
    app, client, network = miora_app
    import_account(client)
    if anthropic:
        image = {"type": "image", "source": {"type": "url", "url": "https://example.org/image.png"}}
    else:
        image = {"type": "image_url", "image_url": {"url": "https://example.org/image.png"}}
    response = client.post(
        "/v1/messages" if anthropic else "/v1/chat/completions",
        headers=api_headers(app),
        json={
            "model": "glm-5.3@miora",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Explain"}, image]}],
        },
    )
    assert response.status_code == 400, response.text
    assert network.calls == []


@pytest.mark.parametrize("anthropic", [False, True])
def test_bad_tool_schema_is_rejected_before_chat_without_penalizing_account(miora_app, anthropic):
    app, client, network = miora_app
    import_account(client)
    schema = {"type": "object", "properties": None}
    tools = (
        [{"name": "read_file", "input_schema": schema}]
        if anthropic
        else [{"type": "function", "function": {"name": "read_file", "parameters": schema}}]
    )
    response = client.post(
        "/v1/messages" if anthropic else "/v1/chat/completions",
        headers=api_headers(app),
        json={
            "model": "glm-5.3@miora",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Read main.py"}],
            "tools": tools,
        },
    )
    assert response.status_code == 400, response.text
    assert network.calls == []
    assert not app.state.credentials.candidates(["miora"])[0].disabled


@pytest.mark.parametrize("anthropic", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_two_protocol_tools_and_result_roundtrip(miora_app, anthropic, stream):
    app, client, network = miora_app
    import_account(client)
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    body = {
        "model": "gpt-6-astra@miora",
        "stream": stream,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Read main.py"}],
    }
    if anthropic:
        body["tools"] = [{"name": "read_file", "input_schema": schema}]
        body["tool_choice"] = {"type": "tool", "name": "read_file"}
    else:
        body["tools"] = [
            {"type": "function", "function": {"name": "read_file", "parameters": schema}}
        ]
        body["tool_choice"] = {"type": "function", "function": {"name": "read_file"}}
    path = "/v1/messages" if anthropic else "/v1/chat/completions"
    headers = api_headers(app)
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert network.calls[-1][1] == "gpt-6-astra"
    assert network.calls[-1][2]["tools"][0]["function"]["name"] == "read_file"
    if stream:
        assert "read_file" in response.text and "main.py" in response.text
        assert ("tool_use" in response.text) if anthropic else ("tool_calls" in response.text)
        records = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        if anthropic:
            tool_id = next(
                record["content_block"]["id"]
                for record in records
                if record.get("type") == "content_block_start"
                and record["content_block"]["type"] == "tool_use"
            )
        else:
            tool_id = next(
                call["id"]
                for record in records
                for choice in record["choices"]
                for call in choice.get("delta", {}).get("tool_calls", [])
                if "id" in call
            )
    elif anthropic:
        tool = response.json()["content"][0]
        assert tool["type"] == "tool_use" and tool["name"] == "read_file"
        assert tool["input"] == {"path": "main.py"}
        assert response.json()["stop_reason"] == "tool_use"
        tool_id = tool["id"]
    else:
        tool = response.json()["choices"][0]["message"]["tool_calls"][0]
        assert tool["function"]["name"] == "read_file"
        assert json.loads(tool["function"]["arguments"]) == {"path": "main.py"}
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
        tool_id = tool["id"]

    # 第二轮由原客户端提供工具结果；Miora 仅接收消息上下文，不执行文件操作。
    followup = copy.deepcopy(body)
    followup["stream"] = False
    if anthropic:
        followup["tool_choice"] = {"type": "auto"}
        followup["messages"] += [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "read_file",
                        "input": {"path": "main.py"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": "CLIENT FILE CONTENT",
                    }
                ],
            },
        ]
    else:
        followup["tool_choice"] = "auto"
        followup["messages"] += [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "type": "function",
                        "id": tool_id,
                        "function": {"name": "read_file", "arguments": '{"path":"main.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": tool_id, "content": "CLIENT FILE CONTENT"},
        ]
    result = client.post(path, headers=headers, json=followup)
    assert result.status_code == 200, result.text
    assert "file summary" in result.text
    assert "CLIENT FILE CONTENT" in network.calls[-1][3][0]["text"]
