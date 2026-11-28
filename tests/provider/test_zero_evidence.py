"""Only affirmative, exact upstream balances authorize destructive cleanup (no real calls)."""

import httpx
import pytest

from src.provider.base import Quota, UpstreamProtocolViolation, matching_amounts
from src.provider.codearts.client import parse_credits
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.miora.client import MioraClient
from src.provider.miora.credential import MioraCredential
from src.provider.officeace.events import summarize_subscription
from src.provider.qoder.events import parse_quota, parse_web_quota
from src.provider.trae.client import TraeClient, TraeCredential
from tests.provider.codebuddy._support import client as cb_client
from tests.provider.lingxi._support import CREDENTIAL
from tests.provider.lingxi._support import client as lingxi_client


@pytest.mark.parametrize("value", [False, True, None, "", "nan", "Infinity", "-1", "1e-330"])
def test_not_a_measured_zero(value):
    assert not matching_amounts(value)


@pytest.mark.parametrize("overrides", [dict(remaining=False), dict(probe_failed=True),
                                       dict(probed_at=None), dict(total=float("nan")),
                                       dict(total=-1), dict(zero_evidence=False)])
def test_observation_requires_success_timestamp_and_real_numbers(overrides):
    args = dict(remaining=0, total=1, zero_evidence=True, probed_at=1) | overrides
    assert not Quota(**args).confirmed_zero


@pytest.mark.parametrize("accounts,count,expected", [
    ([{"Status": 0, "CycleCapacitySize": 5, "CycleCapacityRemainPrecise": "0.00"}], 1, True),
    ([{"Status": 0, "CycleCapacitySize": 5}], 1, False),
    ([{"Status": 0, "CycleCapacitySize": 5, "CycleCapacityRemain": 0}], 2, False),
    ([{}, {"Status": 0, "CycleCapacitySize": 5, "CycleCapacityRemain": 0}], 2, False),
    ([], 0, False), (None, 0, False),
])
async def test_codebuddy_missing_partial_and_real(accounts, count, expected):
    body = {"data": {"Response": {"Data": {"TotalCount": count, "Accounts": accounts}}}}
    client = cb_client(lambda _: httpx.Response(200, json=body))
    assert (await client.fetch_quota(CodeBuddyCredential(bearer_token="test"))).confirmed_zero is (
        expected)


@pytest.mark.parametrize("packs,expected", [
    ([], False), ([{}], False),
    ([{"entitlement_base_info": {"quota": {"credits_limit": 2}},
       "usage": {"credits_amount": 2}}], True),
])
async def test_trae_only_complete_packs(packs, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"user_entitlement_pack_list": packs}))) as http:
        client = TraeClient(short_client=http, stream_client=http)
        assert (await client.fetch_quota(TraeCredential(access_token="test"))).confirmed_zero is (
            expected)


@pytest.mark.parametrize("code,source,expected", [
    ("Success", {"total_balance": "0"}, True), ("Success", {"credits_balance": 0}, True),
    ("Success", {"total_balance": "bad", "credits_balance": 0}, False),
    ("Success", {}, False), ("Unauthorized", {"total_balance": 0}, None),
])
async def test_lingxi_explicit_balance(code, source, expected):
    client = lingxi_client(lambda _: httpx.Response(200, json={"code": code, "data": source}))
    if expected is None:
        with pytest.raises(UpstreamProtocolViolation):
            await client.probe_quota(CREDENTIAL)
    else:
        assert (await client.probe_quota(CREDENTIAL)).confirmed_zero is expected


@pytest.mark.parametrize("amount,used,items,expected", [
    ("5.0", 5, [], True), (False, 0, [], False), (4, 5, [], False),
    (5, 5, None, False), ("9007199254740993", "9007199254740992", [], False),
    (5, 5, [{"status": "active", "total": 5, "used": 0}], False),
    (5, 5, [{"status": "active", "total": 5, "used": 5}], True),
])
async def test_miora_exact_balance_and_consistent_detail(amount, used, items, expected):
    def handle(request):
        if request.url.path.endswith("/credit"):
            data = {"totalAmount": amount, "usedAmount": used}
        elif request.url.path.endswith("/subscription"):
            data = {"tierCode": "free"}
        elif items is None:
            return httpx.Response(401)
        else:
            data = {"groups": [{"items": items}]}
        return httpx.Response(200, json={"code": 0, "data": data})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        quota = await MioraClient(short_client=http).probe_quota(MioraCredential("test"))
        assert quota.confirmed_zero is expected


def test_qoder_both_sources_require_balances_not_just_details_or_totals():
    bucket = {"remaining": 0}
    assert parse_quota({"userQuota": bucket, "addOnQuota": bucket}, now=1).confirmed_zero
    assert not parse_quota({"userQuota": bucket}, now=1).confirmed_zero
    assert not parse_quota({}, now=1).confirmed_zero
    container = {"quota_summary": {"remaining_value": 0}, "quota_detail": [{"remaining_value": 0}]}
    web = dict.fromkeys(("plan_quota", "resource_package_quota",
                         "dedicated_resource_package_quota"), container)
    assert parse_web_quota(web, now=1).confirmed_zero
    container["quota_detail"][0]["remaining_value"] = 1
    assert not parse_web_quota(web, now=1).confirmed_zero
    container.pop("quota_summary")
    assert not parse_web_quota(web, now=1).confirmed_zero


def test_officeace_and_codearts_require_actual_readings():
    data = {"skus": [], "bonus_skus": [{"points": "5", "current_value": "5"}]}
    assert summarize_subscription(data)["zero_evidence"]
    assert not summarize_subscription(data | {"skus": [{}]})["zero_evidence"]
    assert not summarize_subscription({})["zero_evidence"]
    data["bonus_skus"].append({"points": "bad"})
    assert not summarize_subscription(data)["zero_evidence"]
    metric = {"name": "usageTotalPackageCredit", "package_credit_amount": 100}
    assert not parse_credits({"metrics": [metric]}).confirmed_zero
    metric["package_credit_remain"] = 0
    assert parse_credits({"metrics": [metric]}).confirmed_zero
