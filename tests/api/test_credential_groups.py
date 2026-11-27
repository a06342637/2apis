"""分组管理权限、动态Key绑定与所有对话协议的严格账号作用域。"""

from __future__ import annotations

import pytest

from src.db.key_update import update_api_key_fields
from src.provider.base import Model
from src.provider.miora.client import MioraHTTPError
from tests.api.test_miora_integration import miora_app as _miora_fixture
from tests.api.test_miora_integration import token

miora_app = _miora_fixture


def add_accounts(app, count=3):
    return [app.state.credentials.add(provider="miora", credential_data={
        "access_token": token(f"account-{index}", "initial"), "refresh_token": "synthetic",
        "uid": f"account-{index}"}) for index in range(count)]


def create_group(client, ids, name="Team"):
    response = client.post("/api/credential-groups", json={
        "provider": "miora", "name": name, "credential_ids": ids})
    assert response.status_code == 200, response.text
    return response.json()


def group_key(client, gids, **policy):
    response = client.post("/api/api-keys", json={"group_ids": gids, **policy})
    assert response.status_code == 200, response.text
    return response.json()


def headers(key):
    return {"Authorization": f"Bearer {key['api_key']}"}


def test_group_crud_permission_visibility_csrf_audit_and_edit_conflict(miora_app):
    app, client, _network = miora_app
    first, second, _foreign = add_accounts(app)
    created = create_group(client, [first])
    groups = client.get("/api/credential-groups?provider=miora").json()
    assert groups["is_operator"] and groups["groups"] == [created]
    assert client.get(f"/api/credential-groups/{created['id']}").json() == created
    assert client.get("/api/credential-groups/missing").status_code == 400
    response = client.put(f"/api/credential-groups/{created['id']}", json={
        "name": "Renamed", "credential_ids": [second],
        "expected_updated_at": created["updated_at"]})
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["id"] == created["id"] and changed["updated_at"] > created["updated_at"]
    assert client.put(f"/api/credential-groups/{created['id']}", json={
        "name": "stale", "expected_updated_at": created["updated_at"]}).status_code == 409
    assert client.put(f"/api/credential-groups/{created['id']}", json={}).json() == changed
    assert client.post("/api/credential-groups", headers={"Origin": "https://foreign.example"},
        json={"name": "CSRF", "provider": "miora"}).status_code == 403
    with app.state.credentials._db.transaction() as conn:
        conn.execute("UPDATE users SET role='viewer' WHERE username='root'")
    listing = client.get("/api/credential-groups").json()
    assert not listing["is_operator"] and "credential_ids" not in listing["groups"][0]
    assert "credential_ids" not in client.get(f"/api/credential-groups/{created['id']}").json()
    for method, path, body in (("post", "/api/credential-groups", {"name": "No"}),
                              ("put", f"/api/credential-groups/{created['id']}", {"name": "No"}),
                              ("delete", f"/api/credential-groups/{created['id']}", None)):
        assert client.request(method, path, json=body).status_code == 403
    with app.state.credentials._db.transaction() as conn:
        conn.execute("UPDATE users SET role='operator' WHERE username='root'")
    assert client.delete(f"/api/credential-groups/{created['id']}").status_code == 200
    assert client.delete(f"/api/credential-groups/{created['id']}").status_code == 400
    audits = [row for row in app.state.audit.query()
              if row["action"].startswith("credential_group.")]
    assert {row["action"] for row in audits} == {
        "credential_group.create", "credential_group.update", "credential_group.delete"}
    assert len(audits) == 3 and all(row["actor"] == "root" for row in audits)
    assert "access_token" not in str(audits) and "refresh_token" not in str(audits)


@pytest.mark.parametrize("payload", [
    {"provider": "miora"}, {"provider": "miora", "name": ""},
    {"provider": "unknown", "name": "Team"}, {"provider": None, "name": "Team"},
    {"provider": "miora", "name": "Team", "credential_ids": None},
    {"provider": "miora", "name": "Team", "credential_ids": ["missing"]},
    {"provider": "miora", "name": "Team", "secret": "unwanted"},
])
def test_group_create_invalid_fields_never_leave_partial_groups(miora_app, payload):
    app, client, _network = miora_app
    assert client.post("/api/credential-groups", json=payload).status_code == 400
    assert app.state.services.credential_groups.list_all() == []


@pytest.mark.parametrize("payload", [
    {"provider": "codearts"}, {"name": None}, {"credential_ids": "one"},
    {"expected_updated_at": None}, {"expected_updated_at": True},
    {"expected_updated_at": "1"}, {"expected_updated_at": -1},
])
def test_group_update_rejects_invalid_fields(miora_app, payload):
    app, client, _network = miora_app
    created = create_group(client, add_accounts(app)[:1])
    assert client.put(f"/api/credential-groups/{created['id']}", json=payload).status_code == 400
    assert app.state.services.credential_groups.get(created["id"]) == created


def test_group_missing_update_and_concurrent_delete_are_explicit(miora_app, monkeypatch):
    app, client, _network = miora_app
    assert client.put("/api/credential-groups/missing", json={"name": "No"}).status_code == 400
    created = create_group(client, add_accounts(app)[:1])
    monkeypatch.setattr(app.state.services.credential_groups, "delete", lambda _gid: False)
    assert client.delete(f"/api/credential-groups/{created['id']}").status_code == 400


def test_key_groups_exclusive_and_provider_validation_and_membership_is_dynamic(miora_app):
    app, client, network = miora_app
    one, two, _foreign = add_accounts(app)
    group = create_group(client, [one])
    empty = create_group(client, [], "Empty")
    assert client.post("/api/api-keys", json={"group_ids": ["missing"]}).status_code == 400
    assert client.post("/api/api-keys", json={"group_ids": [group["id"]],
        "credential_ids": [one]}).status_code == 400
    assert client.post("/api/api-keys", json={"group_ids": [group["id"]],
        "provider_binding": "codearts"}).status_code == 400
    key = group_key(client, [group["id"], empty["id"]], provider_binding="miora")
    assert key["credential_ids"] == [] and key["group_ids"] == [group["id"], empty["id"]]
    assert client.get("/api/api-keys").json()["api_keys"][0]["group_ids"] == key["group_ids"]
    assert client.put(f"/api/api-keys/{key['id']}",
                      json={"credential_ids": [one]}).status_code == 400
    assert client.put(f"/api/api-keys/{key['id']}",
                      json={"provider_binding": "codearts"}).status_code == 400

    async def catalog(credential):
        return [Model(id=f"model-{credential.uid}")]

    network.fetch_models = catalog
    assert {row["id"] for row in client.get("/v1/models", headers=headers(key)).json()["data"]} == {
        "model-account-0"}
    app.state.services.credential_groups.update(group["id"], name="Renamed", credential_ids=[two])
    assert {row["id"] for row in client.get("/v1/models", headers=headers(key)).json()["data"]} == {
        "model-account-1"}
    app.state.services.credential_groups.delete(group["id"])
    assert client.get("/v1/models", headers=headers(key)).status_code == 403
    assert app.state.api_keys.authenticate(key["api_key"])["group_ids"] == key["group_ids"]
    # 明确编辑到手工账号模式时同时清旧组，历史缺组并不阻止该模式切换。
    changed = client.put(f"/api/api-keys/{key['id']}",
                         json={"credential_ids": [one], "group_ids": []})
    assert changed.status_code == 200, changed.text
    assert set(changed.json()["changed"]) == {"credential_ids", "group_ids"}


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_group_retry_cannot_escape_and_empty_or_missing_groups_fail_before_dispatch(
        miora_app, path, stream):
    app, client, network = miora_app
    app.state.services.registry["miora"].pacer = None
    # 本用例只测绑定边界；短冷却续试在引擎假时钟回归中单独覆盖。
    app.state.executor._deps.short_cooldown_wait_seconds = 0
    one, _two, _foreign = add_accounts(app)
    group = create_group(client, [one])
    key = group_key(client, [group["id"]])
    calls = []

    async def unavailable(credential, payload, model):
        calls.append(credential.uid)
        raise MioraHTTPError(503, b"temporarily unavailable")
        yield  # 调用方是async iterator；无合法事件可产出。

    network.stream_chat = unavailable
    body = {"model": "glm-5.3@miora", "stream": stream, "max_tokens": 16}
    if path == "/v1/responses":
        body["input"] = "hi"
    else:
        body["messages"] = [{"role": "user", "content": "hi"}]
    response = client.post(path, headers=headers(key), json=body)
    assert response.status_code in (200, 503), response.text
    assert calls == ["account-0"]
    app.state.services.credential_groups.update(group["id"], credential_ids=[])
    assert client.post(path, headers=headers(key), json=body).status_code == 403
    assert client.get("/v1beta/models", headers=headers(key)).status_code == 403
    assert calls == ["account-0"]


def test_same_group_different_keys_rotate_independently_and_paused_members_stay_scoped(miora_app):
    app, client, network = miora_app
    app.state.services.registry["miora"].pacer = None
    one, two, _foreign = add_accounts(app)
    group = create_group(client, [one, two])
    keys = [group_key(client, [group["id"]]) for _ in range(2)]
    body = {"model": "glm-5.3@miora", "messages": [{"role": "user", "content": "hi"}]}
    for key in keys + keys:
        response = client.post("/v1/chat/completions", headers=headers(key), json=body)
        assert response.status_code == 200, response.text
    called = [call[0] for call in network.calls]
    assert called[0] == called[1] and called[2] == called[3] and called[0] != called[2]
    assert set(called) == {"account-0", "account-1"}
    for account_id in (one, two):
        app.state.credentials.set_enabled(account_id, False)
    assert app.state.services.credential_groups.get(group["id"])["member_count"] == 2
    response = client.post("/v1/chat/completions", headers=headers(keys[0]), json=body)
    assert response.status_code == 503, response.text
    assert [call[0] for call in network.calls] == called


def test_corrupt_or_mutually_exclusive_database_group_policy_cannot_authenticate(miora_app):
    app, client, _network = miora_app
    one = add_accounts(app)[0]
    group = create_group(client, [one])
    key = group_key(client, [group["id"]])
    with pytest.raises(ValueError, match="cannot both"):
        app.state.api_keys.create("root", credential_ids=[one], group_ids=[group["id"]])
    with pytest.raises(ValueError, match="cannot both"):
        update_api_key_fields(app.state.api_keys, key["id"], "root", {"credential_ids": [one]})
    assert not update_api_key_fields(app.state.api_keys, "missing", "root", {"group_ids": []})
    with app.state.credentials._db.transaction() as conn:
        conn.execute("UPDATE api_keys SET group_ids='null' WHERE id=?", (key["id"],))
    assert client.get("/v1/models", headers=headers(key)).status_code == 401
    with app.state.credentials._db.transaction() as conn:
        conn.execute("UPDATE api_keys SET group_ids=?,credential_ids=? WHERE id=?",
            ('["group"]', '["account"]', key["id"]))
    assert client.get("/v1/models", headers=headers(key)).status_code == 401
