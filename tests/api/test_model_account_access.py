"""展示目录的 VIP 元数据不能扩大任何 Key 的账号权限。"""

from __future__ import annotations

import itertools
import time
from functools import partial
from types import SimpleNamespace

import pytest

from src.api.account_models import account_model_tables, merge_account_model
from src.api.miora_preflight import miora_account_scope
from src.api.model_catalog import load_catalog, save_catalog
from src.api.models import _refresh_provider
from src.compat.openai.request import parse_chat_request
from src.provider.base import Model
from tests.api.test_miora_integration import import_account
from tests.api.test_miora_integration import miora_app as _miora_fixture

miora_app = _miora_fixture


@pytest.mark.parametrize("first,second", list(itertools.product([None, False, True], repeat=2)))
def test_permission_union_does_not_let_unknown_replace_explicit_denial(first, second):
    result = merge_account_model(
        Model(id="one", account_available=first),
        Model(
            id="one",
            account_available=second,
            required_account_tier="standard",
            account_unavailable_reason="tier_locked",
        ),
    )
    expected = True if True in (first, second) else False if False in (first, second) else None
    assert result.account_available is expected
    assert result.required_account_tier == "standard"
    assert result.account_unavailable_reason == (None if expected is True else "tier_locked")


def install_catalog(app, monkeypatch):
    async def models(credential):
        return [
            Model(id="ordinary", name="Ordinary", account_available=True),
            Model(
                id="premium",
                name="Premium",
                required_account_tier="standard",
                account_available=credential.uid == "vip",
                account_unavailable_reason=None if credential.uid == "vip" else "tier_locked",
            ),
        ]

    monkeypatch.setattr(app.state.services.registry["miora"].client, "fetch_models", models)


def key(client, ids):
    result = client.post("/api/api-keys", json={"credential_ids": ids, "provider_binding": "miora"})
    assert result.status_code == 200
    return {"Authorization": "Bearer " + result.json()["api_key"]}


def test_free_and_vip_pool_display_and_client_scope_are_separate(miora_app, monkeypatch):
    app, client, _network = miora_app
    install_catalog(app, monkeypatch)
    free = import_account(client, "free")["id"]
    vip = import_account(client, "vip")["id"]
    services = app.state.services
    services.model_list_cache.clear()
    services.model_list_fetched_at.clear()
    client.portal.call(_refresh_provider, services, "miora", services.registry["miora"])
    assert services.model_list_cache["miora"]["premium"].account_available is True
    assert "premium" not in services.credential_model_cache[free][1]
    assert services.credential_model_display_cache[free]["premium"].account_available is False
    assert {
        row["id"] for row in client.get("/v1/models", headers=key(client, [free])).json()["data"]
    } == {"ordinary"}
    assert {
        row["id"] for row in client.get("/v1/models", headers=key(client, [vip])).json()["data"]
    } == {"ordinary", "premium"}
    assert {
        row["id"]
        for row in client.get("/v1/models", headers=key(client, [free, vip])).json()["data"]
    } == {"ordinary", "premium"}
    # /v1 不读取该 query，不允许客户端用展示开关绕过账号范围。
    assert {
        row["id"]
        for row in client.get(
            "/v1/models?include_unavailable=true", headers=key(client, [free])
        ).json()["data"]
    } == {"ordinary"}
    display = client.get("/api/playground/models?include_unavailable=true").json()["data"]
    premium = next(row for row in display if row["id"] == "premium")
    assert premium["account_available"] is True and premium["required_account_tier"] == "standard"
    assert premium["by_provider"]["miora"]["required_account_tier"] == "standard"


def test_free_only_display_keeps_locked_items_but_no_callable_alias(miora_app, monkeypatch):
    app, client, _network = miora_app
    install_catalog(app, monkeypatch)
    free = import_account(client, "free")["id"]
    services = app.state.services
    services.model_list_cache.clear()
    services.model_list_fetched_at.clear()
    display = client.get("/api/playground/models?include_unavailable=true").json()["data"]
    locked = next(row for row in display if row["id"] == "premium")
    assert locked["account_available"] is False
    assert locked["account_unavailable_reason"] == "tier_locked"
    assert "premium" not in services.model_aliases.get("miora", {})
    assert {row["id"] for row in client.get("/api/playground/models").json()["data"]} == {
        "ordinary"
    }
    assert client.get("/v1/models/premium", headers=key(client, [free])).status_code == 404
    client.cookies.clear()
    assert client.get("/api/playground/models?include_unavailable=true").status_code == 401


def test_failed_catalog_retains_display_evidence_without_widening_routing(miora_app, monkeypatch):
    app, client, _network = miora_app
    install_catalog(app, monkeypatch)
    free = import_account(client, "free")["id"]
    services = app.state.services
    first = client.portal.call(
        partial(account_model_tables, services, (free,), include_unavailable=True)
    )
    services.credential_model_cache[free] = (
        time.monotonic() - 301,
        services.credential_model_cache[free][1],
    )

    async def fail(_data):
        raise RuntimeError("offline")

    monkeypatch.setattr(services.registry["miora"].client, "fetch_models", fail)
    assert (
        client.portal.call(
            partial(account_model_tables, services, (free,), include_unavailable=True)
        )
        == first
    )
    assert "premium" not in client.portal.call(account_model_tables, services, (free,))[free]
    services.credentials.delete(free)
    assert client.portal.call(account_model_tables, services, (free,)) == {}
    assert free not in services.credential_model_display_cache


def test_catalog_snapshot_roundtrips_account_permission_metadata(tmp_path):
    model = Model(
        id="premium",
        required_account_tier="standard",
        account_available=False,
        account_unavailable_reason="tier_locked",
    )
    save_catalog(str(tmp_path), {"miora": {"premium": model}})
    assert load_catalog(str(tmp_path))["miora"][1]["premium"] == model


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/messages"])
def test_vip_call_uses_only_authorized_accounts_even_for_unbound_key(miora_app, monkeypatch, path):
    app, client, network = miora_app
    install_catalog(app, monkeypatch)
    free = import_account(client, "free")["id"]
    vip = import_account(client, "vip")["id"]
    body = {"model": "premium@miora", "max_tokens": 32}
    if path == "/v1/responses":
        body["input"] = "hello"
    else:
        body["messages"] = [{"role": "user", "content": "hello"}]
    for ids in ([free, vip], []):
        response = client.post(path, headers=key(client, ids), json=body)
        assert response.status_code == 200, response.text
    assert [call[0] for call in network.calls] == ["vip", "vip"]
    before = len(network.calls)
    response = client.post(path, headers=key(client, [free]), json=body)
    assert response.status_code == 400, response.text
    assert len(network.calls) == before


@pytest.mark.parametrize(
    "model,binding,aliases,expected",
    [
        ("premium", "codearts", {}, ()),
        ("premium@codearts", "", {}, ()),
        ("premium", "", {}, ("free", "vip")),
        ("premium", "", {"codearts": {"premium": "raw"}}, ()),
        ("premium", "", {"miora": {"premium": "raw"}}, ("free", "vip")),
        ("premium@miora", "", {}, ("free", "vip")),
        ("auto", "miora", {}, ("free", "vip")),
    ],
)
def test_all_pool_scope_is_only_created_for_unambiguous_miora_requests(
    model, binding, aliases, expected
):
    services = SimpleNamespace(
        settings=SimpleNamespace(default_model="ordinary"),
        model_aliases=aliases,
        credentials=SimpleNamespace(
            candidates=lambda *args, **kwargs: [
                SimpleNamespace(credential_id="free", provider="miora"),
                SimpleNamespace(credential_id="vip", provider="miora"),
            ]
        ),
    )
    request = parse_chat_request({"model": model, "messages": [{"role": "user", "content": "x"}]})
    assert miora_account_scope(services, request, binding) == expected


def test_all_failed_accounts_preserve_global_snapshot_evidence(miora_app, monkeypatch):
    app, client, _network = miora_app
    install_catalog(app, monkeypatch)
    import_account(client, "free")
    services = app.state.services
    previous = Model(
        id="premium",
        required_account_tier="standard",
        account_available=False,
        account_unavailable_reason="tier_locked",
    )
    services.model_list_cache["miora"] = {"premium": previous}
    services.credential_model_cache.clear()
    services.credential_model_display_cache.clear()
    services.credential_model_versions.clear()

    async def fail(_credential):
        raise RuntimeError("offline")

    monkeypatch.setattr(services.registry["miora"].client, "fetch_models", fail)
    client.portal.call(_refresh_provider, services, "miora", services.registry["miora"])
    assert services.model_list_cache["miora"] == {"premium": previous}
    assert services.model_list_failures["miora"] == 1


def test_invalid_payload_is_rejected_before_account_catalog_load(miora_app, monkeypatch):
    app, client, network = miora_app
    install_catalog(app, monkeypatch)
    free = import_account(client, "free")["id"]

    async def forbidden(*args, **kwargs):
        raise AssertionError("invalid payload attempted account catalog loading")

    monkeypatch.setattr("src.api.endpoints.account_model_tables", forbidden)
    response = client.post(
        "/v1/chat/completions",
        headers=key(client, [free]),
        json={
            "model": "ordinary@miora",
            "temperature": 0.5,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 400, response.text
    assert not network.calls
