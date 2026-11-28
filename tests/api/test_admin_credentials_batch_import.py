"""批量导入端点：逐条校验、去重、失败隔离与非操作员拦截。"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.compat.openai.request import InvalidRequest
from src.config import Settings
from src.main import build_app
from src.provider.base import Quota
from src.request_context import get_credential_id
from tests.helpers.constants import SECRET


class _StubProvider:
    """只验证端点接线：导入映射 + 探测桩，绝不出网。"""

    endpoint = "https://e"

    def import_credential(self, raw):
        if raw.get("bad"):
            raise InvalidRequest("bad credential")
        return {"bearer_token": raw.get("token", "t")}

    async def probe_quota(self, _data):
        return Quota(remaining=7, total=10, probed_at=1)


def _install(app) -> None:
    app.state.services.registry["codebuddy"] = _StubProvider()


def _count(app) -> int:
    return len(app.state.credentials.candidates(["codebuddy"]))


def test_batch_import_creates_and_skips_duplicates(admin_client):
    app, client = admin_client
    _install(app)
    app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "dup"})

    response = client.post("/api/credentials/batch-import", json={"items": [
        {"provider": "codebuddy", "credential": {"token": "dup"}},      # 与库里重复
        {"provider": "codebuddy", "credential": {"token": "new1"},
         "nickname": "一号", "tags": ["a", "b"]},
        {"provider": "codebuddy", "credential": {"token": "new1"}},     # 批内重复
        {"provider": "codebuddy", "credential": {"token": "new2"}},
    ]})

    assert response.status_code == 200
    body = response.json()
    assert [row["index"] for row in body["created"]] == [1, 3]
    assert [row["index"] for row in body["skipped"]] == [0, 2]
    assert body["skipped"][0]["reason"] == "duplicate"
    assert body["failed"] == []
    assert _count(app) == 3

    listed = {c.credential_id for c in app.state.credentials.candidates(["codebuddy"])}
    assert {row["id"] for row in body["created"]} <= listed


def test_batch_import_isolates_invalid_entries(admin_client):
    app, client = admin_client
    _install(app)

    response = client.post("/api/credentials/batch-import", json={"items": [
        "not-an-object",
        {"provider": "unknown", "credential": {"token": "x"}},
        {"credential": {"token": "x"}},
        {"provider": "codebuddy", "credential": {"bad": True}},
        {"provider": "codebuddy", "credential": {"token": "ok"}, "tags": {"no": 1}},
        {"provider": "codebuddy", "credential": {"token": "ok2"},
         "tags": ["fine", 1]},
        {"provider": "codebuddy", "credential": {"token": "ok3"}},
    ]})

    body = response.json()
    assert [row["index"] for row in body["failed"]] == [0, 1, 2, 3, 4, 5]
    assert [row["index"] for row in body["created"]] == [6]
    reasons = " ".join(row["reason"] for row in body["failed"])
    assert "unknown provider" in reasons
    assert "tags" in reasons
    assert _count(app) == 1


def test_batch_import_validates_shape_and_limit(admin_client):
    _app, client = admin_client
    for payload in (
        {},
        {"items": []},
        {"items": "nope"},
        {"items": [{}] * 101},
    ):
        assert client.post("/api/credentials/batch-import",
                           json=payload).status_code == 400


def test_batch_import_requires_operator(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as guest:
        guest.cookies.set("coding2api_session", create_session_token("guest", SECRET))
        response = guest.post("/api/credentials/batch-import", json={"items": [
            {"provider": "codebuddy", "credential": {"token": "x"}},
        ]})
        assert response.status_code == 403


def test_import_probes_in_order_and_updates_list_without_manual_probe(admin_client):
    app, client = admin_client
    release = asyncio.Event()
    calls = []

    class SerialProvider(_StubProvider):
        async def probe_quota(self, data):
            token = data["bearer_token"]
            calls.append((get_credential_id(), token))
            if token == "first":
                await release.wait()
            if token == "bad":
                raise TimeoutError
            return Quota(remaining=7, total=10, probed_at=1)

    app.state.services.registry["codebuddy"] = SerialProvider()
    entries = [{"provider": "codebuddy", "credential": {"token": token}}
               for token in ("first", "bad", "third", "third", "removed")]
    response = client.post("/api/credentials/batch-import", json={"items": entries}).json()
    first, bad, third, removed = [row["id"] for row in response["created"]]
    repo = app.state.credentials
    try:
        pending = client.get("/api/credentials").json()["credentials"]
        assert {row["id"] for row in pending if row["quota_probe_pending"]} == {
            first, bad, third, removed}
        assert calls == [(first, "first")]
        # 查询中触发一次后续刷新；排队中的重复触发合并。删号/换票要读取最新值。
        schedule = app.state.services.schedule_probe
        client.portal.call(schedule, first)
        client.portal.call(schedule, third)
        repo.merge_login_credential(third, {"bearer_token": "new"}, "")
        repo.delete(removed)
    finally:
        client.portal.call(release.set)

    async def drain():
        await asyncio.wait_for(asyncio.gather(*app.state.pending_probes), 5)

    client.portal.call(drain)
    assert calls == [(first, "first"), (bad, "bad"), (third, "new"), (first, "first")]
    rows = {row["id"]: row for row in client.get("/api/credentials").json()["credentials"]}
    assert not any(row["quota_probe_pending"] for row in rows.values())
    assert rows[first]["quota_remaining"] == rows[third]["quota_remaining"] == 7
    assert rows[bad]["quota_remaining"] is None and rows[bad]["quota_probed_at"] is not None
