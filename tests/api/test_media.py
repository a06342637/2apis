"""媒体路由 HTTP 契约，只替换生成服务和仓储，不构造本机数据库/部署。"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from src.api import media as mod
from src.auth.rbac import UnauthorizedError
from src.compat.openai.request import InvalidRequest
from src.config import Settings
from src.key_quota import QuotaExceeded
from src.media.context import set_media_principal
from src.media.service import MediaRateLimited
from src.media.storage import MediaStore
from src.webapp.handlers import register_exception_handlers
from src.webapp.request_id import RequestIdMiddleware
from tests.helpers.constants import SECRET

AUTH = {"Authorization": "Bearer sk-media-test"}
IMAGE = b"\x89PNG\r\n\x1a\nroute-image"
VIDEO = b"\x00\x00\x00\x18ftypmp42route-video"


class Lease:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


class Guard:
    def __init__(self):
        self.calls = []
        self.leases = []
        self.error = None

    def admit(self, key_id, quota, **options):
        self.calls.append((key_id, quota, options))
        if self.error:
            raise self.error
        lease = Lease()
        self.leases.append(lease)
        return lease


class Manager:
    def __init__(self, store, records):
        self.store = store
        self.records = records
        self.submitted = []
        self.existing_calls = []
        self.waited = []
        self.refreshed = []
        self.owned_calls = []
        self.fresh_calls = []
        self.previous = None
        self.submit_error = None
        self.wait_error = None
        self.wait_gate = None
        self.wait_started = asyncio.Event()
        self.cancelled = False
        self.revoked = False

    def add(self, kind="video", *, completed=False, **values):
        job = self.store.create(
            key_id="key-a",
            username="alice",
            kind=kind,
            model="gem-3.1" if kind == "image" else "vidu-q2-pro-video",
            status="completed" if completed else "in_progress",
            progress=100 if completed else 0,
            files=[{"mime_type": "image/png" if kind == "image" else "video/mp4"}],
            **values,
        )
        self.store.asset(job["id"], 0).write_bytes(IMAGE if kind == "image" else VIDEO)
        return job

    def existing(self, principal, body, nonce):
        self.existing_calls.append((principal, body, nonce))
        if self.previous is not None and nonce == "same-request":
            if body["model"] != self.previous["model"]:
                raise InvalidRequest("Idempotency-Key request mismatch")
            return self.previous
        return None

    async def submit(self, principal, body, *, idempotency_key):
        self.submitted.append((principal, body, idempotency_key))
        if self.submit_error:
            raise self.submit_error
        return self.add(body["kind"])

    async def wait(self, job, principal):
        self.waited.append((job, principal))
        self.wait_started.set()
        if self.wait_error:
            raise self.wait_error
        if self.wait_gate is not None:
            try:
                await self.wait_gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return self.store.save({**job, "status": "completed", "progress": 100})

    async def image_response(self, job, response_format):
        data = (
            {"b64_json": base64.b64encode(IMAGE).decode()}
            if response_format == "b64_json"
            else {"url": self.store.signed_url(job, "http://testserver")}
        )
        return {"created": job["created_at"], "data": [data]}

    def public_job(self, job):
        return {
            "id": job["id"].replace("media_", "video_", 1),
            "object": "video",
            "model": job["model"],
            "status": job["status"],
            "progress": job["progress"],
        }

    def fresh_principal(self, principal):
        self.fresh_calls.append(principal)
        if self.revoked:
            raise UnauthorizedError("revoked media key")
        return replace(principal, provider_binding="miora", credential_ids=("mi1",))

    def owned(self, job_id, principal):
        self.owned_calls.append((job_id, principal))
        job = self.store.load(job_id.replace("video_", "media_", 1))
        if (job["key_id"], job["username"]) != (principal.key_id, principal.username):
            raise HTTPException(404, "media job not found")
        return job

    async def refresh(self, job, principal):
        self.refreshed.append((job, principal))
        return job


@pytest.fixture
async def app(tmp_path, monkeypatch):
    from src.media import storage

    # 私有文件权限实现另有全平台测试；本路由夹具只隔离该文件系统边界。
    monkeypatch.setattr(storage, "secure_dir", lambda path: None)
    monkeypatch.setattr(storage, "secure_file", lambda path: None)
    records = {
        "sk-media-test": {
            "id": "key-a",
            "username": "alice",
            "provider_binding": "miora",
            "credential_ids": ["mi1"],
        },
        "sk-media-other": {"id": "key-b", "username": "bob"},
    }
    settings = Settings(
        _env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path), NONSTREAM_KEEPALIVE_SECONDS=0.05
    )
    store = MediaStore(tmp_path / "media", SECRET, ttl=60, clock=lambda: 100)
    manager = Manager(store, records)
    services = SimpleNamespace(
        settings=settings,
        media=manager,
        api_keys=SimpleNamespace(authenticate=lambda key: records.get(key)),
        users=SimpleNamespace(is_active=lambda username: True),
        user_repo=SimpleNamespace(get=lambda username: {}),
        key_quota=Guard(),
        user_quota=Guard(),
    )
    application = FastAPI()
    application.state.services = services
    application.include_router(mod.create_router(services))
    register_exception_handlers(application)
    application.add_middleware(RequestIdMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://testserver", headers=AUTH
    ) as client:
        yield SimpleNamespace(
            app=application, services=services, manager=manager, client=client, records=records
        )
    set_media_principal(None)


def image_body(**values):
    return {"model": "gem-3.1", "prompt": "A ceramic cup", **values}


def video_body(**values):
    return {"model": "vidu-q2-pro-video", "prompt": "A ceramic cup", **values}


def released(app):
    counts = [
        lease.released
        for guard in (app.services.key_quota, app.services.user_quota)
        for lease in guard.leases
    ]
    assert counts == [1] * len(counts)


def update_settings(app, **values):
    app.services.settings = app.services.settings.model_copy(update=values)


@pytest.mark.parametrize("response_format", ["url", "b64_json"])
async def test_standard_images_response_uses_authenticated_account_scope_and_releases_quota(
    app, response_format
):
    response = await app.client.post(
        "/v1/images/generations", json=image_body(response_format=response_format)
    )
    assert response.status_code == 200
    item = response.json()["data"][0]
    if response_format == "b64_json":
        assert base64.b64decode(item["b64_json"]) == IMAGE
    else:
        assert urlsplit(item["url"]).path.startswith("/v1/media/media_")
    principal, body, nonce = app.manager.submitted[0]
    assert principal.username == "alice" and principal.credential_ids == ("mi1",)
    assert principal.provider_binding == "miora" and body["kind"] == "image" and nonce is None
    assert app.services.key_quota.calls[0][2]["tokens"] > 0
    released(app)


async def test_json_video_creation_is_asynchronous_and_status_and_content_can_be_read(app):
    response = await app.client.post(
        "/v1/videos", json=video_body(seconds="1"), headers={"Idempotency-Key": "new-request"}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["object"] == "video" and result["status"] == "in_progress"
    assert app.manager.submitted[0][1]["options"]["duration"] == 1
    assert app.manager.submitted[0][2] == "new-request" and app.manager.waited == []
    stored = app.manager.store.load(result["id"].replace("video_", "media_", 1))
    app.manager.store.save({**stored, "status": "completed", "progress": 100})
    status = await app.client.get(f"/v1/videos/{result['id']}")
    content = await app.client.get(f"/v1/videos/{result['id']}/content")
    assert status.json()["status"] == "completed" and content.content == VIDEO
    assert content.headers["content-type"] == "video/mp4"
    assert content.headers["cache-control"] == "private, no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["referrer-policy"] == "no-referrer"
    released(app)


async def test_openai_sdk_multipart_fields_map_to_native_video_parameters(app):
    response = await app.client.post(
        "/v1/videos",
        files=[
            ("model", (None, "vidu-q2-pro-video")),
            ("prompt", (None, "A ceramic cup")),
            ("seconds", (None, "1")),
            ("size", (None, "1280x720")),
        ],
    )
    assert response.status_code == 200 and response.json()["object"] == "video"
    assert app.manager.submitted[0][1]["options"] == {
        "count": 1,
        "duration": 1,
        "aspect_ratio": "16:9",
        "resolution": "720P",
    }
    released(app)


@pytest.mark.parametrize(
    "extra",
    [
        [("prompt", (None, "duplicate"))],
        [("input_reference", (None, "file/user/reference"))],
        [("input_reference", ("reference.png", IMAGE, "image/png"))],
        [("attachment", ("reference.png", IMAGE, "image/png"))],
        [(f"extra-{index}", (None, "value")) for index in range(17)],
    ],
)
async def test_duplicate_edit_file_and_excess_multipart_fields_are_rejected_before_submit(
    app, extra
):
    response = await app.client.post(
        "/v1/videos",
        files=[
            ("model", (None, "vidu-q2-pro-video")),
            ("prompt", (None, "cup")),
            *extra,
        ],
    )
    assert response.status_code == 400
    assert app.manager.submitted == [] and app.services.key_quota.calls == []


@pytest.mark.parametrize("path", ["/v1/images/generations", "/v1/videos"])
async def test_bad_json_and_disabled_generation_never_admit_quota(app, path):
    bad = await app.client.post(path, content="{bad", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400
    app.services.media = None
    response = await app.client.post(path, json=image_body() if "images" in path else video_body())
    assert response.status_code == 400 and "disabled" in response.json()["error"]["message"]
    assert app.services.key_quota.calls == []


@pytest.mark.parametrize("kind", ["image", "video"])
async def test_idempotent_replay_does_not_resubmit_or_re_admit_quota(app, kind):
    app.manager.previous = app.manager.add(kind, completed=True)
    body = image_body(response_format="b64_json") if kind == "image" else video_body()
    response = await app.client.post(
        "/v1/images/generations" if kind == "image" else "/v1/videos",
        json=body,
        headers={"Idempotency-Key": "same-request"},
    )
    assert response.status_code == 200
    assert app.manager.submitted == [] and app.services.key_quota.calls == []
    assert bool(app.manager.waited) == (kind == "image")


async def test_idempotency_payload_mismatch_is_an_error_without_submitting(app):
    app.manager.previous = app.manager.add("image", completed=True)
    response = await app.client.post(
        "/v1/images/generations",
        json=image_body(model="different"),
        headers={"Idempotency-Key": "same-request"},
    )
    assert response.status_code == 400 and "mismatch" in response.json()["error"]["message"]
    assert app.manager.submitted == [] and app.services.key_quota.calls == []


@pytest.mark.parametrize("guard_name", ["key_quota", "user_quota"])
@pytest.mark.parametrize("kind", ["image", "video"])
async def test_quota_429_preserves_retry_after_and_never_starts_generation(app, guard_name, kind):
    getattr(app.services, guard_name).error = QuotaExceeded("quota exceeded", 13)
    response = await app.client.post(
        "/v1/images/generations" if kind == "image" else "/v1/videos",
        json=image_body() if kind == "image" else video_body(),
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "13"
    assert response.json()["error"]["type"] == "quota_exceeded" and app.manager.submitted == []
    released(app)


@pytest.mark.parametrize("kind", ["image", "video"])
async def test_upstream_429_is_not_retried_and_releases_both_quota_leases(app, kind):
    app.manager.submit_error = MediaRateLimited(27)
    response = await app.client.post(
        "/v1/images/generations" if kind == "image" else "/v1/videos",
        json=image_body() if kind == "image" else video_body(),
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "27"
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert len(app.manager.submitted) == 1
    released(app)


async def test_image_poll_timeout_is_explicit_and_does_not_repeat_submission(app):
    app.manager.wait_error = TimeoutError()
    response = await app.client.post("/v1/images/generations", json=image_body())
    assert response.status_code == 400 and "not repeated" in response.json()["error"]["message"]
    assert len(app.manager.submitted) == 1
    released(app)


@pytest.mark.parametrize("kind", ["image", "video"])
async def test_transport_failure_cannot_leak_quota_or_repeat_submit(app, kind):
    app.manager.submit_error = httpx.ReadTimeout("uncertain submission")
    response = await app.client.post(
        "/v1/images/generations" if kind == "image" else "/v1/videos",
        json=image_body() if kind == "image" else video_body(),
    )
    assert response.status_code == 502 and len(app.manager.submitted) == 1
    released(app)


@pytest.mark.parametrize("suffix", ["", "/content"])
async def test_image_jobs_are_not_accepted_by_video_routes_and_other_key_cannot_read_video(
    app, suffix
):
    image = app.manager.add("image", completed=True)
    response = await app.client.get(f"/v1/videos/{image['id']}{suffix}")
    assert response.status_code == 404
    video = app.manager.add("video", completed=True)
    denied = await app.client.get(
        f"/v1/videos/{video['id']}{suffix}", headers={"Authorization": "Bearer sk-media-other"}
    )
    assert denied.status_code == 404


async def test_video_content_is_not_exposed_until_completed(app):
    job = app.manager.add("video")
    response = await app.client.get(f"/v1/videos/{job['id']}/content")
    assert response.status_code == 409 and "not ready" in response.json()["detail"]


def signed(manager, job, index=0):
    return manager.store.signed_url(job, "http://testserver", index)


async def test_signed_content_revalidates_current_principal_without_bearer_and_has_private_headers(
    app,
):
    job = app.manager.add("image", completed=True)
    response = await app.client.get(signed(app.manager, job), headers={"Authorization": ""})
    assert response.status_code == 200 and response.content == IMAGE
    assert app.manager.fresh_calls[0].key_id == "key-a"
    assert app.manager.owned_calls[-1][1].credential_ids == ("mi1",)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("index", [-1, 1])
async def test_validly_signed_out_of_range_index_returns_not_ready(app, index):
    job = app.manager.add("image", completed=True)
    response = await app.client.get(signed(app.manager, job, index))
    assert response.status_code == 409


async def test_expired_bad_or_missing_signature_is_not_allowed(app):
    job = app.manager.add("image", completed=True)
    url = signed(app.manager, job)
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    expired = await app.client.get(
        parsed.path, params={"expires": 100, "signature": query["signature"][0]}
    )
    bad = await app.client.get(parsed.path, params={"expires": 160, "signature": "0" * 64})
    missing = await app.client.get(parsed.path)
    assert expired.status_code == bad.status_code == 403 and missing.status_code == 422
    assert app.manager.fresh_calls == []


async def test_signed_content_rechecks_gateway_pause_and_revoked_key(app):
    job = app.manager.add("image", completed=True)
    url = signed(app.manager, job)
    update_settings(app, gateway_paused=True)
    paused = await app.client.get(url)
    assert paused.status_code == 503 and paused.json()["error"]["code"] == "service_paused"
    assert app.manager.fresh_calls == []
    update_settings(app, gateway_paused=False)
    app.manager.revoked = True
    revoked = await app.client.get(url)
    assert revoked.status_code == 401


@pytest.mark.parametrize("path", ["/v1/images/generations", "/v1/videos"])
async def test_authentication_expiry_and_global_pause_also_gate_new_generation(app, path):
    missing = await app.client.post(path, json=image_body(), headers={"Authorization": ""})
    assert missing.status_code == 401
    app.records["sk-media-test"]["expires_at"] = int(time.time()) - 1
    expired = await app.client.post(path, json=image_body())
    assert expired.status_code == 401
    app.records["sk-media-test"]["expires_at"] = None
    update_settings(app, gateway_paused=True)
    paused = await app.client.post(path, json=image_body())
    assert paused.status_code == 503 and app.manager.submitted == []


def asgi_request(app):
    messages = []
    disconnected = asyncio.Event()
    whitespace = asyncio.Event()
    received = False
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "root_path": "",
        "path": "/v1/images/generations",
        "raw_path": b"/v1/images/generations",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer sk-media-test"),
            (b"content-type", b"application/json"),
        ],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1),
    }

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {
                "type": "http.request",
                "body": json.dumps(image_body()).encode(),
                "more_body": False,
            }
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message.copy())
        if message["type"] == "http.response.body" and message.get("body") == b"\n":
            whitespace.set()

    task = asyncio.create_task(app.app(scope, receive, send))
    return messages, disconnected, whitespace, task


@pytest.mark.parametrize("cancel", [False, True])
async def test_slow_images_keep_valid_json_and_cancel_work_and_release_on_disconnect(app, cancel):
    update_settings(app, nonstream_keepalive_seconds=0.01)
    app.manager.wait_gate = asyncio.Event()
    messages, disconnected, whitespace, task = asgi_request(app)
    async with asyncio.timeout(2):
        await app.manager.wait_started.wait()
        await whitespace.wait()
        response_start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        assert response_start["status"] == 200
        headers = dict(response_start["headers"])
        assert headers[b"content-type"] == b"application/json"
        assert headers[b"x-accel-buffering"] == b"no"
        if cancel:
            disconnected.set()
        else:
            app.manager.wait_gate.set()
        await task
    assert app.manager.cancelled == cancel
    assert len(app.manager.submitted) == 1
    released(app)
    if not cancel:
        body = b"".join(message.get("body", b"") for message in messages)
        assert json.loads(body)["data"][0]["url"].startswith("http://testserver/v1/media/")


async def test_zero_keepalive_configuration_does_not_stream_or_spin_while_work_is_pending(app):
    update_settings(app, nonstream_keepalive_seconds=0)
    app.manager.wait_gate = asyncio.Event()
    messages, _disconnected, _whitespace, task = asgi_request(app)
    # The guard catches deadlocks, not host scheduling delays. Observe pending
    # work across loop turns rather than requiring a 20 ms wall-clock wakeup.
    async with asyncio.timeout(30):
        await app.manager.wait_started.wait()
        for _ in range(5):
            await asyncio.sleep(0)
        before_completion = list(messages)
        app.manager.wait_gate.set()
        await task
    assert before_completion == []
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    assert b"content-length" in dict(response_start["headers"])
    released(app)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("private timeout details"),
        MediaRateLimited(42),
        httpx.ReadTimeout("private upstream details"),
    ],
)
async def test_failure_after_first_keepalive_is_valid_sanitized_json_without_replaying(
    app,
    monkeypatch,
    failure,
):
    update_settings(app, nonstream_keepalive_seconds=0.01)
    gate = asyncio.Event()

    async def late(job, principal):
        app.manager.wait_started.set()
        await gate.wait()
        raise failure

    monkeypatch.setattr(app.manager, "wait", late)
    messages, _disconnected, whitespace, task = asgi_request(app)
    async with asyncio.timeout(2):
        await whitespace.wait()
        gate.set()
        await task
    payload = json.loads(b"".join(message.get("body", b"") for message in messages))
    assert payload["error"]["code"] == "upstream_error"
    assert "private" not in payload["error"]["message"]
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    assert response_start["status"] == 200
    assert b"retry-after" not in dict(response_start["headers"])
    assert len(app.manager.submitted) == 1
    released(app)


async def test_cancellation_before_headers_cancels_wait_and_releases_quota(app):
    update_settings(app, nonstream_keepalive_seconds=1)
    app.manager.wait_gate = asyncio.Event()
    messages, _disconnected, _whitespace, task = asgi_request(app)
    async with asyncio.timeout(2):
        await app.manager.wait_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert messages == [] and app.manager.cancelled
    assert len(app.manager.submitted) == 1
    released(app)
