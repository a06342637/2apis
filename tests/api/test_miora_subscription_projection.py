"""导入后套餐投影与缓存失效，实际可用性仍来自该账号目录。"""

from __future__ import annotations

import asyncio
from functools import partial

import pytest

from src.api import account_models as mod
from src.api.account_models import account_model_tables
from src.api.models import _refresh_provider, serve_models
from src.provider.base import Model, Quota
from src.provider.miora.subscription import subscription_state
from tests.api.test_miora_integration import import_account, token, wait_for_probes
from tests.api.test_miora_integration import miora_app as _miora_fixture

miora_app = _miora_fixture


def plan(tier="pro", *, expires=110, probed=100):
    return {
        "miora_subscription": {
            "tier": tier,
            "status": "free" if tier == "free" else "active",
            "vip": tier != "free",
            "expires_at": expires,
            "probed_at": probed,
        }
    }


def test_import_probe_exposes_anonymous_subscription_and_old_balance_updates_preserve_it(miora_app):
    app, client, network = miora_app
    detail = plan("free", expires=None)

    async def probe(_credential):
        return Quota(remaining=7, total=10, probed_at=100, detail=detail)

    network.probe_quota = probe
    cid = import_account(client, "free")["id"]
    wait_for_probes(app, client)
    row = next(
        row for row in client.get("/api/credentials").json()["credentials"] if row["id"] == cid
    )
    assert row["quota_detail"] == detail and row["quota_remaining"] == 7
    stored = app.state.services.credentials.credential_data(cid)
    assert stored["access_token"] not in str(row) and stored["refresh_token"] not in str(row)
    app.state.services.credentials.save_quota(cid, Quota(remaining=6, total=10, probed_at=101))
    assert app.state.services.credentials.quota_detail(cid) == detail


@pytest.mark.parametrize("subscription,status,vip,expiry", [
    ({"tierCode": "pro", "status": "active", "expireAt": "2030-01-01T00:00:00Z"},
     "active", True, 1_893_456_000),
    ({"tierCode": "standard", "status": "expired", "expireAt": "2020-01-01T00:00:00Z"},
     "expired", False, 1_577_836_800),
    ({"tierCode": "pro", "status": "active", "expireAt": "not-a-date"},
     "unknown", None, None),
    ({"tierCode": "free", "status": "active", "expireAt": ""}, "free", False, None),
])
def test_membership_expiry_projection_supports_countdown_without_inventing_dates(
    miora_app, subscription, status, vip, expiry
):
    app, client, network = miora_app
    normalized = subscription_state(subscription, now=1_800_000_000)

    async def probe(_credential):
        return Quota(remaining=7, total=10, probed_at=1_800_000_000,
                     detail={"miora_subscription": normalized})

    network.probe_quota = probe
    cid = import_account(client, "expiry-account")["id"]
    wait_for_probes(app, client)
    row = next(row for row in client.get("/api/credentials").json()["credentials"]
               if row["id"] == cid)
    public = row["quota_detail"]["miora_subscription"]
    assert public == normalized
    assert public["status"] == status and public["vip"] is vip
    assert public["expires_at"] == expiry
    assert set(public) == {"tier", "status", "vip", "expires_at", "probed_at"}


def test_subscription_changes_and_expiry_refresh_scope_but_probe_time_does_not(
    miora_app, monkeypatch
):
    app, client, network = miora_app
    now = [100]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    services = app.state.services
    cid = services.credentials.add(
        provider="miora",
        credential_data={
            "access_token": token("vip", "initial"),
            "refresh_token": "synthetic",
            "uid": "vip",
        },
    )
    offered = [True]
    seen = []

    async def models(_credential):
        seen.append(offered[0])
        return [
            Model(id="ordinary", account_available=True),
            Model(id="vip", account_available=offered[0], required_account_tier="pro"),
        ]

    network.fetch_models = models

    def fetch():
        return client.portal.call(account_model_tables, services, (cid,))[cid]

    services.credentials.save_quota(cid, Quota(remaining=7, total=10, probed_at=100, detail=plan()))
    assert "vip" in fetch()
    version = services.credential_model_versions[cid]
    services.credentials.save_quota(
        cid, Quota(remaining=6, total=10, probed_at=101, detail=plan(probed=101))
    )
    assert "vip" in fetch() and services.credential_model_versions[cid] == version
    assert len(seen) == 1
    services.credentials.save_quota(
        cid, Quota(remaining=6, total=10, probed_at=101, detail=plan("free", expires=None))
    )
    offered[0] = False
    assert "vip" not in fetch() and len(seen) == 2
    services.credentials.save_quota(
        cid, Quota(remaining=6, total=10, probed_at=101, detail=plan(expires=102))
    )
    offered[0] = True
    assert "vip" in fetch() and len(seen) == 3
    now[0] = 102
    offered[0] = False
    assert "vip" not in fetch() and len(seen) == 4


def test_changed_subscription_failure_never_reuses_previous_vip_catalog(miora_app):
    app, client, network = miora_app
    services = app.state.services
    cid = services.credentials.add(
        provider="miora",
        credential_data={
            "access_token": token("vip", "initial"),
            "refresh_token": "synthetic",
            "uid": "vip",
        },
    )
    detail = plan(expires=2_000_000_000)
    services.credentials.save_quota(cid, Quota(remaining=7, total=10, probed_at=100, detail=detail))
    first = client.portal.call(account_model_tables, services, (cid,))
    assert first[cid]
    services.credentials.save_quota(
        cid, Quota(remaining=7, total=10, probed_at=101, detail=plan("free", expires=None))
    )

    async def failed(_credential):
        raise RuntimeError("directory offline after plan changed")

    network.fetch_models = failed
    tables = client.portal.call(
        partial(account_model_tables, services, (cid,), include_unavailable=True)
    )
    assert tables == {cid: {}}


def test_provider_directory_tracks_plan_and_account_changes_without_refreshing_unchanged_accounts(
    miora_app,
):
    app, client, network = miora_app
    services = app.state.services
    offered = {"first": False, "second": False}
    seen = []

    async def models(credential):
        seen.append(credential.uid)
        return [
            Model(id="ordinary", account_available=True),
            Model(id="vip", account_available=offered[credential.uid], required_account_tier="pro"),
        ]

    network.fetch_models = models
    first = services.credentials.add(
        provider="miora",
        credential_data={
            "access_token": token("first", "initial"),
            "refresh_token": "synthetic",
            "uid": "first",
        },
    )
    services.credentials.save_quota(
        first, Quota(remaining=7, total=10, probed_at=100, detail=plan("free", expires=None))
    )

    def ids():
        return {row["id"] for row in client.get("/api/playground/models").json()["data"]}

    assert ids() == {"ordinary"} and seen == ["first"]
    offered["first"] = True
    services.credentials.save_quota(
        first, Quota(remaining=7, total=10, probed_at=101, detail=plan(expires=2_000_000_000))
    )
    assert ids() == {"ordinary", "vip"} and seen == ["first", "first"]
    services.credentials.save_quota(
        first,
        Quota(remaining=7, total=10, probed_at=102, detail=plan(expires=2_000_000_000, probed=102)),
    )
    assert ids() == {"ordinary", "vip"} and len(seen) == 2
    services.credentials.add(
        provider="miora",
        credential_data={
            "access_token": token("second", "initial"),
            "refresh_token": "synthetic",
            "uid": "second",
        },
    )
    assert ids() == {"ordinary", "vip"} and seen == ["first", "first", "second"]
    services.credentials.delete(first)
    assert ids() == {"ordinary"} and len(seen) == 3


def test_changed_provider_permissions_failure_drops_old_vip_and_does_not_retry_unchanged_state(
    miora_app,
):
    app, client, network = miora_app
    services = app.state.services
    fail = [False]
    seen = []

    async def models(_credential):
        seen.append(1)
        if fail[0]:
            raise RuntimeError("directory unavailable after permissions changed")
        return [Model(id="vip", account_available=True, required_account_tier="pro")]

    network.fetch_models = models
    cid = services.credentials.add(
        provider="miora",
        credential_data={
            "access_token": token("vip", "initial"),
            "refresh_token": "synthetic",
            "uid": "vip",
        },
    )
    services.credentials.save_quota(
        cid, Quota(remaining=7, total=10, probed_at=100, detail=plan(expires=2_000_000_000))
    )
    assert client.get("/api/playground/models").json()["data"]
    fail[0] = True
    services.credentials.save_quota(
        cid, Quota(remaining=7, total=10, probed_at=101, detail=plan("free", expires=None))
    )
    assert client.get("/api/playground/models").json()["data"] == []
    assert client.get("/api/playground/models").json()["data"] == []
    assert len(seen) == 2 and services.model_list_failures["miora"] == 1
    services.credentials.save_quota(
        cid,
        Quota(remaining=7, total=10, probed_at=102, detail=plan("free", expires=None, probed=102)),
    )
    assert client.get("/api/playground/models").json()["data"] == [] and len(seen) == 2


def test_parallel_permission_revalidation_waits_for_one_complete_directory(miora_app):
    app, client, network = miora_app
    services = app.state.services
    services.credentials.add(
        provider="miora",
        credential_data={
            "access_token": token("vip", "initial"),
            "refresh_token": "synthetic",
            "uid": "vip",
        },
    )

    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def models(_credential):
            seen.append(1)
            entered.set()
            await release.wait()
            return [Model(id="ordinary", account_available=True)]

        network.fetch_models = models
        first = asyncio.create_task(serve_models(services))
        await entered.wait()
        second = asyncio.create_task(serve_models(services))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        results = await asyncio.gather(first, second)
        assert all([row["id"] for row in result["data"]] == ["ordinary"] for result in results)
        assert seen == [1]

    client.portal.call(check)


def test_other_provider_cache_and_failure_do_not_require_miora_subscription(miora_app):
    app, client, _network = miora_app
    services = app.state.services

    class Other:
        async def list_models(self, _data):
            return [Model(id="other")]

        async def aclose(self):
            # 由应用关闭调用；没有网络资源。
            return None

    provider = Other()
    services.registry["codearts"] = provider
    cid = services.credentials.add(provider="codearts", credential_data={"token": "synthetic"})
    assert client.portal.call(account_model_tables, services, (cid,), "codearts")[cid]
    assert [row["id"] for row in client.get("/api/playground/models").json()["data"]] == ["other"]

    async def failed(_data):
        raise RuntimeError("generic provider unavailable")

    provider.list_models = failed
    client.portal.call(_refresh_provider, services, "codearts", provider)
    assert services.model_list_cache["codearts"] and services.model_list_failures["codearts"] == 1
