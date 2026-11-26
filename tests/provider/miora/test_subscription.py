"""订阅匿名字段、有效期与余额探测的独立失败边界。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.provider.base import Quota
from src.provider.miora import MioraProvider
from src.provider.miora import subscription as mod
from src.provider.miora.client import MioraClient
from src.provider.miora.credential import MioraCredential
from src.provider.miora.subscription import subscription_signature, subscription_state

NOW = int(datetime(2026, 11, 26, tzinfo=UTC).timestamp())
FUTURE = "2026-12-26T00:00:00+00:00"


def test_real_free_subscription_fixture_is_anonymous_and_does_not_mean_zero_rate_models():
    data = json.loads((Path(__file__).parent / "fixtures" / "subscription-free.json").read_text())
    result = subscription_state(
        {**data, "userId": "private-id", "uin": "private-uin", "tierName": "untrusted-name"},
        now=NOW,
    )
    assert result == {
        "tier": "free",
        "status": "free",
        "vip": False,
        "expires_at": None,
        "probed_at": NOW,
    }
    assert set(result) == {"tier", "status", "vip", "expires_at", "probed_at"}


@pytest.mark.parametrize(
    "code,tier",
    [
        ("basic", "basic"),
        ("standard", "standard"),
        ("pro", "pro"),
        ("std", "standard"),
        ("trial_basic", "basic"),
        ("trial_standard", "standard"),
        ("trial_pro", "pro"),
    ],
)
def test_paid_tiers_require_active_status_and_future_expiry(code, tier):
    result = subscription_state({"tierCode": code, "status": "active", "expireAt": FUTURE}, now=NOW)
    assert result["tier"] == tier and result["status"] == "active" and result["vip"] is True


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"tierCode": 1},
        {"tierCode": "unknown"},
        {"tierCode": "pro", "status": "active"},
        {"tierCode": "pro", "status": "active", "expireAt": "not-date"},
        {"tierCode": "pro", "status": "paused", "expireAt": FUTURE},
        {"tierCode": "free"},
        {"tierCode": "free", "status": "unknown"},
    ],
)
def test_unknown_or_incomplete_subscription_is_never_guessed_as_free_or_vip(data):
    result = subscription_state(data, now=NOW)
    assert result["status"] == "unknown" and result["vip"] is None


@pytest.mark.parametrize(
    "data",
    [
        {"tierCode": "pro", "status": "active", "expireAt": "2026-11-25T00:00:00Z"},
        {"tierCode": "standard", "status": "active", "expireAt": "2026-11-26T00:00:00Z"},
        {"tierCode": "basic", "status": "expired", "expireAt": FUTURE},
    ],
)
def test_expired_plan_is_not_vip_even_when_tier_name_is_paid(data):
    assert subscription_state(data, now=NOW)["vip"] is False
    assert subscription_state(data, now=NOW)["status"] == "expired"


def test_timezone_expiry_matches_chinese_billing_and_explicit_timezone():
    assert mod._expiry("2026-12-26 08:00:00") == mod._expiry(FUTURE)
    assert mod._expiry("2026-12-26T00:00:00Z") == mod._expiry(FUTURE)
    assert mod._expiry(12345) is None


@pytest.mark.parametrize("error", [OverflowError, OSError])
def test_platform_timestamp_failure_only_makes_expiry_unknown(monkeypatch, error):
    def fail():
        raise error("unsupported platform timestamp")

    monkeypatch.setattr(
        mod,
        "datetime",
        SimpleNamespace(fromisoformat=lambda _value: SimpleNamespace(tzinfo=UTC, timestamp=fail)),
    )
    assert mod._expiry(FUTURE) is None


@pytest.mark.parametrize("failure", ["network", "http", "protocol", "recursion"])
async def test_subscription_probe_failure_does_not_break_valid_credit_balance(failure):
    def handle(request):
        if request.url.path.endswith("/credit"):
            return httpx.Response(200, json={"data": {"totalAmount": 10, "usedAmount": 3}})
        if failure == "network":
            raise httpx.ConnectError("offline", request=request)
        if failure == "http":
            return httpx.Response(503)
        if failure == "recursion":
            return httpx.Response(200, content="[" * 3000 + "0" + "]" * 3000)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as short:
        quota = await MioraClient(short_client=short).probe_quota(MioraCredential("synthetic"))
    assert quota.remaining == 7 and quota.total == 10 and quota.probe_failed is False
    assert quota.detail["miora_subscription"]["status"] == "unknown"
    assert quota.detail["miora_subscription"]["vip"] is None


async def test_successful_probe_informs_media_cache_without_exposing_raw_response():
    class Client:
        async def probe_quota(self, _credential):
            return Quota(
                remaining=7,
                total=10,
                detail={
                    "miora_subscription": subscription_state(
                        {"tierCode": "free", "status": "active"}, now=NOW
                    )
                },
            )

    provider = MioraProvider(client=Client())
    credential = {"access_token": "synthetic", "uid": "one"}
    quota = await provider.probe_quota(credential)
    account = provider.media_client._account(provider.credential_from(credential))
    assert account.subscription_detail == quota.detail


def test_subscription_signature_ignores_probe_time_but_changes_at_expiry():
    detail = {
        "miora_subscription": {
            "tier": "pro",
            "status": "active",
            "vip": True,
            "expires_at": NOW + 1,
            "probed_at": 1,
        }
    }
    before = subscription_signature(detail, now=NOW)
    detail["miora_subscription"]["probed_at"] = 2
    assert subscription_signature(detail, now=NOW) == before
    assert subscription_signature(detail, now=NOW + 1) != before
    assert subscription_signature(detail, now=NOW + 1)[1:3] == ("expired", False)


@pytest.mark.parametrize("detail", [None, [], {}, {"miora_subscription": []}])
def test_invalid_subscription_signature_does_not_assign_a_tier(detail):
    assert subscription_signature(detail, now=NOW) is None


@pytest.mark.parametrize("expires", [None, False, "123", float("nan"), float("inf")])
def test_invalid_signature_expiry_does_not_invent_expiration(expires):
    detail = {
        "miora_subscription": {
            "tier": "pro",
            "status": "unknown",
            "vip": None,
            "expires_at": expires,
        }
    }
    assert subscription_signature(detail, now=NOW) == ("pro", "unknown", None, None)


def test_huge_integer_expiry_does_not_overflow_float_conversion():
    detail = {
        "miora_subscription": {
            "tier": "pro",
            "status": "unknown",
            "vip": None,
            "expires_at": 10**400,
        }
    }
    assert subscription_signature(detail, now=NOW)[3] == 10**400
