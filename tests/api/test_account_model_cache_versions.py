"""按账号目录缓存随登录票据更新失效，不能把旧账号目录当新票据兜底。"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.api.account_models import MAX_ACCOUNT_MODEL_CACHES, account_model_tables
from src.provider.base import Model
from src.provider.miora.client import MioraHTTPError
from tests.api.test_miora_integration import miora_app as _miora_fixture
from tests.api.test_miora_integration import token

miora_app = _miora_fixture


def test_negative_catalog_cache_is_invalidated_after_shared_token_refresh(miora_app, caplog):
    app, client, network = miora_app
    services = app.state.services
    now = int(time.time())
    cid = services.credentials.add(
        provider="miora",
        credential_data={
            "access_token": token("restore", "old", exp=now - 1),
            "refresh_token": "old-rt",
            "uid": "restore",
        },
    )
    seen = []

    async def catalog(credential):
        seen.append(credential.access_token)
        if credential.expires_at <= now:
            raise MioraHTTPError(401, b"expired credential")
        return [Model(id="restored")]

    network.fetch_models = catalog
    assert client.portal.call(account_model_tables, services, (cid,)) == {cid: {}}
    old_version = services.credential_model_versions[cid]
    assert client.portal.call(app.state.task_runner.refresh_credential, cid).succeeded == 1
    restored = client.portal.call(account_model_tables, services, (cid,))
    assert set(restored[cid]) == {"restored"} and len(seen) == 2
    assert services.credential_model_versions[cid] != old_version
    assert all(
        version not in caplog.text for version in services.credential_model_versions.values()
    )
    listing = client.get("/api/credentials").text
    assert all(version not in listing for version in services.credential_model_versions.values())


def test_changed_token_failure_never_uses_old_ticket_model_catalog(miora_app):
    app, client, network = miora_app
    services = app.state.services
    cid = services.credentials.add(
        provider="miora",
        credential_data={"access_token": token("same", "old"), "refresh_token": "r", "uid": "same"},
    )
    original = client.portal.call(account_model_tables, services, (cid,))
    assert original[cid]
    old = services.credentials.credential_data(cid)
    services.credentials.save_credential_data(cid, {**old, "access_token": token("same", "new")})
    attempts = []

    async def failed(credential):
        attempts.append(credential.access_token)
        raise MioraHTTPError(401, b"new login is not available")

    network.fetch_models = failed
    assert client.portal.call(account_model_tables, services, (cid,)) == {cid: {}}
    assert client.portal.call(account_model_tables, services, (cid,)) == {cid: {}}
    assert len(attempts) == 1


def test_failed_credential_read_clears_live_cache_and_fingerprint(miora_app, monkeypatch):
    app, client, _network = miora_app
    services = app.state.services
    cid = services.credentials.add(
        provider="miora",
        credential_data={"access_token": token("same", "old"), "refresh_token": "r", "uid": "same"},
    )
    assert client.portal.call(account_model_tables, services, (cid,))[cid]

    def failed(_cid):
        raise RuntimeError("credential read failed")

    monkeypatch.setattr(services.credentials, "credential_data", failed)
    assert client.portal.call(account_model_tables, services, (cid,)) == {cid: {}}
    assert cid not in services.credential_model_versions
    assert cid not in services.credential_model_cache


def add_accounts(services, count):
    return [services.credentials.add(
        provider="miora", credential_data={"access_token": token(f"account-{index}", "initial"),
            "refresh_token": f"synthetic-{index}", "uid": f"account-{index}"})
        for index in range(count)]


def test_hot_catalog_limit_keeps_all_bound_accounts_usable_and_never_expands_scope(miora_app):
    app, client, network = miora_app
    services = app.state.services
    ids = add_accounts(services, MAX_ACCOUNT_MODEL_CACHES + 1)
    fetched = []

    async def catalog(credential):
        fetched.append(credential.uid)
        return [Model(id=f"model-{credential.uid}")]

    network.fetch_models = catalog
    key = app.state.api_keys.create("root", "all bound", credential_ids=ids)
    tables = client.portal.call(account_model_tables, services, tuple(ids))
    assert len(tables) == len(ids) == 129
    assert all(tables[cid] for cid in ids)
    assert len(services.credential_model_cache) == MAX_ACCOUNT_MODEL_CACHES
    assert set(services.credential_model_cache) == set(services.credential_model_versions)
    assert ids[0] not in services.credential_model_cache
    assert app.state.api_keys.authenticate(key["api_key"])["credential_ids"] == ids

    # 淘汰只影响热缓存。单账号 Key 仍能重拉该账号，不借其余128张票的目录或执行。
    bound = app.state.api_keys.create("root", "evicted only", credential_ids=[ids[0]])
    headers = {"Authorization": f"Bearer {bound['api_key']}"}
    listing = client.get("/v1/models", headers=headers)
    assert listing.status_code == 200, listing.text
    assert {row["id"] for row in listing.json()["data"]} == {"model-account-0"}
    assert fetched == [f"account-{index}" for index in range(129)] + ["account-0"]
    response = client.post("/v1/chat/completions", headers=headers,
        json={"model": "model-account-0@miora", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200, response.text
    assert [call[0] for call in network.calls] == ["account-0"]
    assert len(services.credential_model_cache) == MAX_ACCOUNT_MODEL_CACHES
    assert set(services.credential_model_cache) == set(services.credential_model_versions)


@pytest.mark.parametrize("changed", [False, True])
def test_batch_lru_snapshot_avoids_reloading_hot_catalogs_and_rejects_old_ticket(
        miora_app, monkeypatch, changed):
    app, client, network = miora_app
    services = app.state.services
    monkeypatch.setattr("src.api.account_models.MAX_ACCOUNT_MODEL_CACHES", 2)
    ids = add_accounts(services, 3)
    fetched = []

    async def catalog(credential):
        fetched.append(credential.uid)
        return [Model(id=credential.uid)]

    network.fetch_models = catalog
    assert len(client.portal.call(account_model_tables, services, tuple(ids))) == 3
    assert list(services.credential_model_cache) == ids[1:]
    if changed:
        data = services.credentials.credential_data(ids[1])
        services.credentials.save_credential_data(ids[1],
            {**data, "access_token": token("account-1", "replacement")})

        async def failed(credential):
            fetched.append(credential.uid)
            if credential.uid == "account-1":
                raise MioraHTTPError(401, b"replacement unavailable")
            return [Model(id=credential.uid)]

        network.fetch_models = failed
    tables = client.portal.call(account_model_tables, services, tuple(ids))
    assert tables[ids[1]] == ({} if changed else {"account-1": Model(id="account-1")})
    assert fetched == ["account-0", "account-1", "account-2", "account-0"] + (
        ["account-1"] if changed else [])
    assert list(services.credential_model_cache) == ids[1:]
    assert set(services.credential_model_cache) == set(services.credential_model_versions)


def test_deleted_account_removes_hot_catalog_and_orphaned_fingerprint(miora_app):
    app, client, _network = miora_app
    services = app.state.services
    first, second = add_accounts(services, 2)
    assert client.portal.call(account_model_tables, services, (first, second))
    services.credentials.delete(first)
    services.credential_model_versions["already-deleted"] = "synthetic"
    assert set(client.portal.call(account_model_tables, services, (first, second))) == {second}
    assert set(services.credential_model_cache) == {second}
    assert set(services.credential_model_versions) == {second}



def test_parallel_miora_catalog_is_bounded_and_skips_stalled_account(miora_app, monkeypatch):
    app, client, network = miora_app
    services = app.state.services
    ids = add_accounts(services, 3)
    first_pair = asyncio.Event()
    stall = asyncio.Event()
    active = peak = 0
    attempted = []
    released = []

    async def catalog(credential):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        attempted.append(credential.uid)
        if active == 2:
            first_pair.set()
        try:
            await first_pair.wait()
            if credential.uid == "account-0":
                await stall.wait()
            return [Model(id=credential.uid)]
        finally:
            active -= 1
            released.append(credential.uid)

    network.fetch_models = catalog
    # Use two slots so the third account must wait, but can finish while the first stalls.
    services.miora_catalog_slots = asyncio.Semaphore(2)
    monkeypatch.setattr("src.api.account_models.MIORA_ACCOUNT_CATALOG_SECONDS", 0.15)
    tables = client.portal.call(account_model_tables, services, tuple(ids))
    assert tables == {ids[0]: {}, ids[1]: {"account-1": Model(id="account-1")},
                      ids[2]: {"account-2": Model(id="account-2")}}
    assert attempted == ["account-0", "account-1", "account-2"]
    assert released == ["account-1", "account-2", "account-0"]
    assert active == 0 and peak == 2
    assert client.portal.call(account_model_tables, services, tuple(ids)) == tables
    assert len(attempted) == 3
