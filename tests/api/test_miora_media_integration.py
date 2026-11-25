"""真实网关/DB/鉴权/媒体服务的离线端到端验证，只替换网络边界。"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from src import main
from src.auth.session import create_session_token
from src.provider.base import Event, EventKind, Model, Quota, Usage
from src.provider.miora import MioraProvider
from tests.helpers.auth import isolated_settings
from tests.helpers.chat import jwt_token
from tests.helpers.constants import SECRET
from tests.helpers.startup import wait_for_startup

IMAGE = b"\xff\xd8\xffgateway-test-image"
VIDEO = b"\x00\x00\x00\x18ftypmp42gateway-test-video"


class TextBoundary:
    async def fetch_models(self, _credential):
        return [Model(id="glm-5.3", name="GLM 5.3", supports_tool_call=True)]

    async def probe_quota(self, _credential):
        return Quota(remaining=250, total=250, probed_at=int(time.time()))

    async def stream_chat(self, _credential, payload, _model):
        tools = payload.get("tools", [])
        names = {tool["function"]["name"] for tool in tools}
        prompt = payload["messages"][-1]["content"]
        name = "miora_generate_video" if "video" in prompt else "miora_generate_image"
        if name in names:
            args = {"model": "vidu-q2-pro-video" if "video" in prompt else "gem-3.1",
                    "prompt": "a red circle"}
            if "video" in prompt:
                args["seconds"] = 1
            yield Event(kind=EventKind.TOOL_CALLS, tool_calls=[{
                "id": "call_media", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}])
            reason = "tool_calls"
        else:
            yield Event(kind=EventKind.CONTENT, content="plain reply")
            reason = "stop"
        yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=10, output_tokens=5))
        yield Event(kind=EventKind.FINISH, finish_reason=reason)

    async def aclose(self):
        pass


class MediaBoundary:
    def __init__(self):
        self.submissions = []

    async def catalog(self, _credential, *, force=False):
        return [
            Model(id="gem-3.1", name="Nano Banana 2", supports_tool_call=False,
                  gateway_endpoints=("/v1/images/generations",), output_modalities=("image",)),
            Model(id="vidu-q2-pro-video", name="Vidu Q2 Pro", supports_tool_call=False,
                  gateway_endpoints=("/v1/videos",), output_modalities=("video",)),
        ]

    async def submit(self, credential, kind, model, prompt, **options):
        self.submissions.append((credential.uid, kind, model, prompt, options))
        return {"session_id": "film-session", "task_id": f"task-{len(self.submissions)}",
                "status": "in_progress", "progress": 0}

    async def status(self, _credential, _session, _task):
        return {"status": "completed", "progress": 100,
                "files": [{"file_key": "asset-key", "width": 1024, "height": 1024}]}

    async def file_url(self, _credential, _key):
        return "https://sign.miorausercontent.com/test-resource"


class DownloadBoundary:
    async def download(self, url, target, kind, *, max_bytes=None):
        assert url.startswith("https://sign.miorausercontent.com/")
        data = IMAGE if kind == "image" else VIDEO
        assert max_bytes >= len(data)
        target.write_bytes(data)
        return {"bytes": len(data), "mime_type": "image/jpeg" if kind == "image" else "video/mp4"}


@pytest.fixture
def media_gateway(tmp_path, monkeypatch):
    # 验证的是组装与协议；平台门禁和文件权限另有专用平台覆盖测试。
    monkeypatch.setattr(main, "require_supported_platform", lambda: None)
    monkeypatch.setattr("src.db.conn.secure_dir", lambda *_a, **_k: True)
    provider = MioraProvider(client=TextBoundary())
    app = main.build_app(isolated_settings(tmp_path, MODEL_BLOCKLIST="internal_*",
        NONSTREAM_KEEPALIVE_SECONDS=0), providers={"miora": provider})
    boundary = MediaBoundary()
    app.state.services.media.client = boundary
    app.state.services.media.downloader = DownloadBoundary()
    provider.media_client = boundary
    raw = provider.import_credential({"authToken": jwt_token(int(time.time()) + 15 * 86400,
        payload_extra={"sub": "account-a"}), "refreshToken": "synthetic-refresh"})
    account = app.state.credentials.add(provider="miora", credential_data=raw)
    key = app.state.api_keys.create("root", "media test", provider_binding="miora",
                                    credential_ids=[account])
    headers = {"Authorization": "Bearer " + key["api_key"]}
    with TestClient(app) as client:
        wait_for_startup(app, client)
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        yield app, client, headers, boundary, account, key


def test_catalog_selected_image_id_download_and_idempotency(media_gateway):
    _app, client, headers, boundary, _account, _key = media_gateway
    models = client.get("/v1/models", headers=headers).json()["data"]
    image = next(item for item in models if item.get("output_modalities") == ["image"])
    assert image["by_provider"]["miora"]["raw_id"] == "gem-3.1"
    body = {"model": image["id"], "prompt": "a red circle", "size": "1024x1024"}
    headers = {**headers, "Idempotency-Key": "image-once"}
    response = client.post("/v1/images/generations", headers=headers, json=body)
    assert response.status_code == 200, response.text
    url = urlsplit(response.json()["data"][0]["url"])
    downloaded = client.get(url.path + "?" + url.query)
    assert downloaded.status_code == 200 and downloaded.content == IMAGE
    repeated = client.post("/v1/images/generations", headers=headers, json=body)
    assert repeated.status_code == 200, repeated.text
    assert len(boundary.submissions) == 1
    assert boundary.submissions[0][2] == "gem-3.1"
    changed = client.post("/v1/images/generations", headers=headers,
                          json={**body, "prompt": "another picture"})
    assert changed.status_code == 409 and len(boundary.submissions) == 1


def test_images_base64_response(media_gateway):
    _app, client, headers, _boundary, _account, _key = media_gateway
    response = client.post("/v1/images/generations", headers=headers,
        json={"model": "gem-3.1", "prompt": "a circle", "response_format": "b64_json"})
    assert response.status_code == 200, response.text
    assert base64.b64decode(response.json()["data"][0]["b64_json"]) == IMAGE


def test_video_resume_paused_download_and_binding_revocation(media_gateway):
    app, client, headers, boundary, account, key = media_gateway
    response = client.post("/v1/videos", headers=headers,
        json={"model": "vidu-q2-pro-video", "prompt": "a moving circle", "seconds": "1"})
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    app.state.credentials.set_enabled(account, False)
    status = client.get(f"/v1/videos/{job_id}", headers=headers)
    assert status.status_code == 200 and status.json()["status"] == "completed"
    download = client.get(f"/v1/videos/{job_id}/content", headers=headers)
    assert download.status_code == 200 and download.content == VIDEO
    assert len(boundary.submissions) == 1
    other_key = app.state.api_keys.create("root", "another key")["api_key"]
    assert client.get(f"/v1/videos/{job_id}", headers={
        "Authorization": "Bearer " + other_key}).status_code == 404
    raw = app.state.credentials.credential_data(account)
    second = app.state.credentials.add(provider="miora",
                                      credential_data={**raw, "uid": "account-b"})
    update = client.put(f"/api/api-keys/{key['id']}", json={"credential_ids": [second]})
    assert update.status_code == 200, update.text
    assert client.get(f"/v1/videos/{job_id}", headers=headers).status_code == 400
    assert len(boundary.submissions) == 1


@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("kind", ["image", "video"])
def test_normal_conversation_executes_media_and_returns_real_links(media_gateway, protocol, kind):
    app, client, headers, boundary, account, _key = media_gateway
    prompt = f"Please generate a real {kind} of a red circle"
    model = "glm-5.3@miora"
    if protocol == "chat":
        path = "/v1/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    elif protocol == "responses":
        path = "/v1/responses"
        body = {"model": model, "input": prompt}
    else:
        path = "/v1/messages"
        body = {"model": model, "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}]}
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert "/v1/media/media_" in response.text
    assert "signature=" in response.text
    assert len(boundary.submissions) == 1 and boundary.submissions[0][1] == kind
    assert app.state.services.inflight.count(account) == 0


def test_runtime_auto_media_switch_and_tool_choice_none(media_gateway):
    app, client, headers, boundary, _account, _key = media_gateway
    app.state.runtime_settings.set("miora_auto_media_enabled", False)
    body = {"model": "glm-5.3@miora", "messages": [{"role": "user", "content": "draw image"}]}
    response = client.post("/v1/chat/completions", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "plain reply"
    app.state.runtime_settings.set("miora_auto_media_enabled", True)
    response = client.post("/v1/chat/completions", headers=headers,
                           json={**body, "tool_choice": "none"})
    assert response.status_code == 200, response.text
    assert not boundary.submissions
