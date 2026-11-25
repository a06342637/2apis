"""API Key 的指定账号范围、模型目录与跨协议执行边界。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.api.account_models import account_model_tables, account_provider_tables
from src.auth.account_binding import credential_ids_from_text, normalize_credential_ids
from src.db.crypto import CredentialDecryptError
from src.db.key_update import update_api_key_fields
from src.provider.base import Event, EventKind, Model
from src.provider.miora.client import MioraHTTPError
from tests.api.test_miora_integration import import_account
from tests.api.test_miora_integration import miora_app as _miora_fixture

miora_app = _miora_fixture


def make_key(app, client, ids, **policy):
    response = client.post("/api/api-keys", json={"credential_ids": ids, **policy})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("raw", [None, "cred", [1], [""], [" "], {}])
def test_invalid_account_array_is_rejected_before_key_creation(miora_app, raw):
    app, client, _network = miora_app
    response = client.post("/api/api-keys", json={"credential_ids": raw})
    assert response.status_code == 400
    assert app.state.api_keys.count_for("root") == 0


def test_binding_create_list_update_and_channel_validation(miora_app):
    app, client, _network = miora_app
    one = import_account(client)["id"]
    two = import_account(client, "two")["id"]
    key = make_key(app, client, [f" {one} ", one], provider_binding="miora")
    assert key["credential_ids"] == [one]
    listing = client.get("/api/api-keys").json()["api_keys"]
    assert listing[0]["credential_ids"] == [one]
    assert app.state.api_keys.list_for("root")[0]["credential_ids"] == [one]
    assert app.state.api_keys.authenticate(key["api_key"])["credential_ids"] == [one]
    response = client.put(f"/api/api-keys/{key['id']}", json={"credential_ids": [two]})
    assert response.status_code == 200 and response.json()["changed"] == ["credential_ids"]
    assert (
        client.put(f"/api/api-keys/{key['id']}", json={"credential_ids": [two]}).json()["changed"]
        == []
    )
    assert (
        client.put(f"/api/api-keys/{key['id']}", json={"provider_binding": "codearts"}).status_code
        == 400
    )
    assert client.post("/api/api-keys", json={"credential_ids": ["missing"]}).status_code == 400
    assert (
        client.post(
            "/api/api-keys", json={"provider_binding": "codearts", "credential_ids": [one]}
        ).status_code
        == 400
    )
    assert client.put(f"/api/api-keys/{key['id']}", json={"credential_ids": []}).status_code == 200
    assert app.state.api_keys.authenticate(key["api_key"])["credential_ids"] == []


@pytest.mark.parametrize("stored", ["not-json", "null", "[1]", '[""]'])
def test_damaged_binding_fails_closed(miora_app, stored):
    app, client, _network = miora_app
    key = make_key(app, client, [])
    with app.state.api_keys._db.transaction() as conn:
        conn.execute("UPDATE api_keys SET credential_ids=? WHERE id=?", (stored, key["id"]))
    assert (
        client.get("/v1/models", headers={"Authorization": f"Bearer {key['api_key']}"}).status_code
        == 401
    )


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_bound_request_cannot_retry_unselected_account(miora_app, path, stream):
    app, client, network = miora_app
    one = import_account(client)["id"]
    import_account(client, "unselected")
    key = make_key(app, client, [one])
    attempted = []

    async def unavailable(credential, payload, model):
        attempted.append(credential.uid)
        if credential.uid == "account-1":
            raise MioraHTTPError(503, b"temporarily unavailable")
        yield Event(kind=EventKind.CONTENT, content="unselected account escaped")

    network.stream_chat = unavailable
    body = {"model": "glm-5.3@miora", "stream": stream, "max_tokens": 32}
    if path == "/v1/responses":
        body["input"] = "hello"
    else:
        body["messages"] = [{"role": "user", "content": "hello"}]
    response = client.post(path, headers={"Authorization": f"Bearer {key['api_key']}"}, json=body)
    assert response.status_code in (200, 503), response.text
    assert attempted == ["account-1"]


def test_models_union_uses_selected_accounts_and_applies_all_whitelists(miora_app):
    app, client, network = miora_app
    one = import_account(client)["id"]
    two = import_account(client, "two")["id"]
    import_account(client, "unselected")
    fetched = []

    async def models(credential):
        fetched.append(credential.uid)
        return [
            Model(id=f"raw-{credential.uid}", name=f"Model {credential.uid}"),
            Model(id="shared", name="Shared"),
            Model(id="hidden", name="Hidden"),
        ]

    network.fetch_models = models
    with app.state.credentials._db.transaction() as conn:
        conn.execute("UPDATE credentials SET allowed_models='model-*,shared' WHERE id=?", (one,))
    app.state.user_repo.set_policy("root", allowed_models="model-*,shared", quota="")
    key = make_key(app, client, [one, two], allowed_models="model-account-1,shared")
    headers = {"Authorization": f"Bearer {key['api_key']}"}
    first = client.get("/v1/models", headers=headers)
    assert first.status_code == 200, first.text
    assert {row["id"] for row in first.json()["data"]} == {"model-account-1", "shared"}
    assert "unselected" not in fetched
    counts = list(fetched)
    assert client.get("/v1/models/shared", headers=headers).status_code == 200
    assert client.get("/v1/models/model-two", headers=headers).status_code == 404
    assert fetched == counts
    # 暂停/删除立即撤下目录；绑定值仍保留，不退回全池。
    app.state.credentials.set_enabled(one, False)
    app.state.credentials.delete(two)
    assert client.get("/v1/models", headers=headers).json()["data"] == []
    assert app.state.api_keys.authenticate(key["api_key"])["credential_ids"] == [one, two]
    assert (
        client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "shared@miora", "messages": [{"role": "user", "content": "hello"}]},
        ).status_code
        == 503
    )


def test_selected_accounts_rotate_fairly_and_map_account_specific_raw_ids(miora_app):
    app, client, network = miora_app
    ids = [import_account(client, uid)["id"] for uid in ("one", "two")]

    async def models(credential):
        return [Model(id=f"raw-{credential.uid}", name="Shared model")]

    network.fetch_models = models
    key = make_key(app, client, ids)
    headers = {"Authorization": f"Bearer {key['api_key']}"}
    for _ in range(4):
        response = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "shared-model@miora",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert response.status_code == 200, response.text
    assert [uid for uid, *_ in network.calls] in (
        ["one", "two", "one", "two"],
        ["two", "one", "two", "one"],
    )
    assert all(model == f"raw-{uid}" for uid, model, *_ in network.calls)


def test_account_cache_failure_deletion_and_parallel_fetch(miora_app, monkeypatch):
    app, client, network = miora_app
    account = import_account(client)["id"]
    services = app.state.services
    fetched = []

    async def models(credential):
        fetched.append(credential.uid)
        await asyncio.sleep(0)
        return [Model(id="ok", name="Okay")]

    network.fetch_models = models

    async def parallel():
        return await asyncio.gather(
            account_model_tables(services, (account,)), account_model_tables(services, (account,))
        )

    tables = client.portal.call(parallel)
    assert len(fetched) == 1 and tables[0] == tables[1]
    services.credential_model_cache[account] = (time.monotonic() - 301, tables[0][account])

    async def fail(credential):
        raise RuntimeError("failure")

    network.fetch_models = fail
    assert client.portal.call(account_model_tables, services, (account,)) == tables[0]
    services.credential_model_cache.clear()
    assert client.portal.call(account_model_tables, services, (account,)) == {account: {}}
    services.credential_model_cache.clear()

    def bad_data(_account):
        raise CredentialDecryptError("broken")

    monkeypatch.setattr(services.credentials, "credential_data", bad_data)
    assert client.portal.call(account_model_tables, services, (account,)) == {account: {}}
    services.credential_model_cache.clear()
    monkeypatch.setattr(services.credentials, "credential_data", lambda _account: None)
    assert client.portal.call(account_model_tables, services, (account,)) == {}
    assert account_provider_tables(services, {"deleted": tables[0][account]}) == {}


def test_binding_parser_and_update_repository_contract(miora_app):
    app, client, _network = miora_app
    assert normalize_credential_ids([" a ", "a", "b"]) == ["a", "b"]
    assert credential_ids_from_text("[]") == []
    key = make_key(app, client, [])
    assert update_api_key_fields(
        app.state.api_keys, key["id"], "root", {"credential_ids": ["kept"]}
    )
    stored = (
        app.state.api_keys._db.connect()
        .execute("SELECT credential_ids FROM api_keys WHERE id=?", (key["id"],))
        .fetchone()[0]
    )
    assert json.loads(stored) == ["kept"]


def test_provider_only_binding_filters_model_list_and_detail(miora_app):
    app, client, _network = miora_app
    import_account(client)
    for binding, expected in (("miora", {"glm-5.3", "gpt-6-astra"}), ("trae", set())):
        key = make_key(app, client, [], provider_binding=binding)
        headers = {"Authorization": f"Bearer {key['api_key']}"}
        listing = client.get("/v1/models", headers=headers)
        assert {row["id"] for row in listing.json()["data"]} == expected
        assert client.get("/v1/models/glm-5.3", headers=headers).status_code == (
            200 if expected else 404
        )
