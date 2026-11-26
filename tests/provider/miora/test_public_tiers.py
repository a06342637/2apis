"""匿名目录仅补 VIP 最低套餐，账号的实际权限始终来自认证响应。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.provider.miora import media as mod
from src.provider.miora.client import MioraClient
from src.provider.miora.credential import MioraCredential
from src.provider.miora.media import MioraMediaClient
from tests.provider.miora.test_media import row

pytestmark = pytest.mark.real_upstream("miora_media")


class Transport:
    def __init__(self):
        self.public_calls = 0
        self.auth_calls = 0
        self.failure = None
        self.private_available = True
        self.public = {
            "data": [
                row(
                    "video",
                    available=False,
                    requiredTier="standard",
                    unavailableReason="tier_locked",
                )
            ]
        }

    async def __call__(self, request):
        await asyncio.sleep(0)
        if request.headers.get("authorization"):
            self.auth_calls += 1
            data = {"data": [row("video", available=self.private_available)]}
        else:
            self.public_calls += 1
            if self.failure == "network":
                raise httpx.ConnectError("offline", request=request)
            if self.failure == "http":
                return httpx.Response(503, json={"message": "unavailable"})
            if self.failure == "protocol":
                return httpx.Response(200, json=[])
            data = self.public
        return httpx.Response(200, json={"code": 0, "data": data})


@pytest.fixture()
async def public_clients():
    transport = Transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as short:
        client = MioraClient(short_client=short)
        yield transport, MioraMediaClient(client)


async def test_paid_only_catalog_uses_public_tier_without_overwriting_actual_access(public_clients):
    transport, media = public_clients
    credential = MioraCredential("private-paid-token", uid="vip")
    display = await media.catalog(credential, include_unavailable=True)
    assert display[0].account_available is True
    assert display[0].required_account_tier == "standard"
    assert display[0].account_unavailable_reason is None
    actual = await media.catalog(credential)
    assert actual[0].account_available is True
    assert actual[0].required_account_tier is None
    assert transport.public_calls == transport.auth_calls == 1


async def test_concurrent_accounts_share_one_public_minimum_tier_request(public_clients):
    transport, media = public_clients
    results = await asyncio.gather(
        *[
            media.catalog(MioraCredential(f"private-{uid}", uid=uid), include_unavailable=True)
            for uid in ["one", "two", "three"]
        ]
    )
    assert all(models[0].required_account_tier == "standard" for models in results)
    assert transport.public_calls == 1 and transport.auth_calls == 3


@pytest.mark.parametrize("failure", ["network", "http", "protocol"])
async def test_public_failure_has_ttl_and_does_not_change_actual_catalog(
    public_clients, monkeypatch, failure
):
    transport, media = public_clients
    now = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    transport.failure = failure
    credential = MioraCredential("private-token", uid="vip")
    models = await media.catalog(credential, include_unavailable=True)
    assert models[0].account_available is True and models[0].required_account_tier is None
    await media.catalog(credential, force=True, include_unavailable=True)
    assert transport.public_calls == 1
    now[0] += mod.CATALOG_TTL + 1
    transport.failure = None
    models = await media.catalog(credential, force=True, include_unavailable=True)
    assert models[0].required_account_tier == "standard" and models[0].account_available is True
    assert transport.public_calls == 2
    now[0] += mod.CATALOG_TTL + 1
    transport.failure = failure
    models = await media.catalog(credential, force=True, include_unavailable=True)
    assert models[0].required_account_tier == "standard" and models[0].account_available is True
    assert transport.public_calls == 3


async def test_public_tier_cache_is_bounded_and_does_not_infer_feature_flag_vip(
    public_clients, monkeypatch
):
    transport, media = public_clients
    transport.public["data"] += [
        row("video", modelId="free", requiredTier="free", unavailableReason="tier_locked"),
        row(
            "video",
            modelId="feature-off",
            requiredTier="standard",
            unavailableReason="feature_flag_off",
        ),
        row("video", modelId="ordinary"),
        row("video", modelId="second-vip", requiredTier="pro", unavailableReason="tier_locked"),
    ]
    credential = MioraCredential("private-token", uid="vip")
    assert await media._minimum_tiers(credential) == {"video": "standard", "second-vip": "pro"}
    monkeypatch.setattr(mod, "MAX_PUBLIC_TIER_MODELS", 1)
    media._public_tiers_at = None
    assert await media._minimum_tiers(credential) == {"video": "standard"}


async def test_subscription_probe_clock_and_state_changes_invalidate_only_actual_account_catalog(
    public_clients, monkeypatch
):
    transport, media = public_clients
    now = [100]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    credential = MioraCredential("private-token", uid="vip")
    detail = {
        "miora_subscription": {
            "tier": "pro",
            "status": "active",
            "vip": True,
            "expires_at": 101,
            "probed_at": 100,
        }
    }
    media.note_subscription(credential, detail)
    assert [model.id for model in await media.catalog(credential)] == ["video"]
    account = media._account(credential)
    cached_at = account.catalog_at
    detail["miora_subscription"]["probed_at"] = 101
    media.note_subscription(credential, detail)
    assert account.catalog_at == cached_at
    now[0] = 101
    transport.private_available = False
    assert await media.catalog(credential) == []
    assert transport.auth_calls == 2 and transport.public_calls == 0
    detail["miora_subscription"].update(tier="free", status="free", vip=False, expires_at=None)
    media.note_subscription(credential, detail)
    assert account.catalog_at is None
