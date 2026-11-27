"""Miora 积分各包独立到期，会员截止日期不能用作赠送积分的有效期。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src.provider.base import UpstreamProtocolViolation
from src.provider.miora import credits as mod
from src.provider.miora.client import MioraClient
from src.provider.miora.credential import MioraCredential
from src.provider.miora.credits import PACKS_API, credit_packages, next_expiring_credit

NOW = int(datetime(2026, 11, 27, tzinfo=UTC).timestamp())
SOON = "2026-11-29T00:00:00Z"
SOON_EPOCH = NOW + 2 * 24 * 3600
LATER = "2026-12-27T00:00:00Z"


def pack(*, total=100, used=20, end=SOON, status="active", **extra):
    return {"total": total, "used": used, "endAt": end, "status": status, **extra}


def overview(*rows):
    return {"groups": [{"packType": "gift", "items": list(rows)}]}


def test_observed_free_welcome_pack_has_its_own_expiry_and_real_balance():
    raw = json.loads((Path(__file__).parent / "fixtures" / "credit-packs-free.json").read_text())
    packages, ladder = credit_packages(raw["overview"], now=NOW)
    assert packages == [{"name": "注册赠送积分", "total": 250, "used": 8.5,
                         "end": 1_803_622_654, "usable": True, "expiring": 0}]
    assert ladder == [(1_803_622_654, 241.5)]
    assert next_expiring_credit(raw["credit"], now=NOW, remaining=241.5) == ladder[0]


def test_mixed_expiration_only_counts_future_active_nonzero_packages():
    packages, ladder = credit_packages(overview(
        pack(subPackType="subscription"),
        pack(total="50", used="10", end=LATER, subPackType="bonus"),
        pack(used=100, subPackType="topup"),
        pack(end="2026-11-26T00:00:00Z", subPackType="welcome"),
        pack(status="refunded", packageName="Refunded"),
        pack(status="expired", packageName="Expired"),
        pack(end=None, packageName="No date from upstream"),
        pack(end="permanent", packageName="Invalid date is unknown"),
    ), now=NOW)
    assert [row["name"] for row in packages[:4]] == [
        "基础积分", "福利积分", "购买积分", "注册赠送积分",
    ]
    assert ladder == [(SOON_EPOCH, 80), (NOW + 30 * 24 * 3600, 40)]
    assert sum(row["expiring"] or 0 for row in packages) == 80
    assert packages[3]["usable"] is False and packages[4]["usable"] is False
    assert packages[6]["end"] is None and packages[6]["expiring"] is None
    assert packages[7]["end"] is None and packages[7]["expiring"] is None


def test_zero_balance_unknown_status_and_missing_names_do_not_invent_available_expiry():
    packages, ladder = credit_packages(overview(
        pack(used=200), pack(status="unknown", subPackType={}), pack(end=None, packageName=123),
    ), now=NOW)
    assert ladder is None
    assert packages[0]["expiring"] == 0 and packages[1]["usable"] is False
    assert all(row["name"] == "Miora 积分包" for row in packages)
    assert credit_packages({"groups": []}, now=NOW) == ([], None)


@pytest.mark.parametrize("bad", [None, [], {}, {"groups": None}, {"groups": [None]},
                                  {"groups": [{"items": None}]}, overview(None)])
def test_invalid_overview_fails_as_whole_snapshot(bad):
    with pytest.raises(UpstreamProtocolViolation):
        credit_packages(bad, now=NOW)


@pytest.mark.parametrize("bad", [None, True, {}, [], "not-number", "nan", "inf", -1, 10**400])
@pytest.mark.parametrize("field", ["total", "used"])
def test_invalid_package_numbers_do_not_become_zero(bad, field):
    data = overview(pack())
    data["groups"][0]["items"][0][field] = bad
    with pytest.raises(UpstreamProtocolViolation):
        credit_packages(data, now=NOW)


def test_package_snapshot_is_bounded(monkeypatch):
    monkeypatch.setattr(mod, "MAX_CREDIT_PACKAGES", 1)
    with pytest.raises(UpstreamProtocolViolation, match="安全限制"):
        credit_packages(overview(pack(), pack()), now=NOW)


@pytest.mark.parametrize("raw", [None, [], {}, {"nextExpiring": []},
    {"nextExpiring": {"credit": 20}},
    {"nextExpiring": {"credit": 20, "expiredAt": "2026-11-26T00:00:00Z"}},
    {"nextExpiring": {"credit": None, "expiredAt": SOON}},
    {"nextExpiring": {"credit": 0, "expiredAt": SOON}},
])
def test_unknown_or_already_expired_summary_does_not_guess_dates(raw):
    assert next_expiring_credit(raw, now=NOW, remaining=100) is None


def test_nearest_expiring_summary_never_marks_all_remaining_credits_as_expiring():
    raw = {"nextExpiring": {"credit": "20", "expiredAt": SOON}}
    assert next_expiring_credit(raw, now=NOW, remaining=100) == (SOON_EPOCH, 20)
    assert next_expiring_credit(raw, now=NOW, remaining=10) == (SOON_EPOCH, 10)
    assert next_expiring_credit(raw, now=NOW, remaining=0) is None


@pytest.mark.parametrize("failure", [None, "network", "http", "protocol", "recursion"])
@pytest.mark.parametrize("summary", [True, False])
async def test_probe_packages_failure_preserves_credit_and_real_nearest_summary(
    failure, summary, monkeypatch
):
    monkeypatch.setattr("src.provider.miora.client.time.time", lambda: NOW)
    credit = {"totalAmount": "100", "usedAmount": "10"}
    if summary:
        credit["nextExpiring"] = {"credit": 20, "expiredAt": SOON}

    def handle(request):
        assert request.method == "POST"
        if request.url.path.endswith("/credit"):
            data = credit
        elif request.url.path.endswith("/subscription"):
            data = {"tierCode": "free", "status": "active", "expireAt": ""}
        else:
            assert request.url.path == PACKS_API
            if failure == "network":
                raise httpx.ConnectError("offline", request=request)
            if failure == "http":
                return httpx.Response(503)
            if failure == "protocol":
                data = {}
            elif failure == "recursion":
                return httpx.Response(200, content="[" * 3000 + "0" + "]" * 3000)
            else:
                data = overview(pack(total=100, used=10, subPackType="welcome"))
        return httpx.Response(200, json={"code": 0, "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as short:
        quota = await MioraClient(short_client=short).probe_quota(MioraCredential("synthetic"))
    assert quota.remaining == 90 and quota.total == 100 and not quota.probe_failed
    assert quota.detail["miora_subscription"]["expires_at"] is None
    metadata = quota.detail["miora_credits"]
    if failure is None:
        assert quota.packages[0]["total"] == 100 and quota.packages[0]["used"] == 10
        assert quota.expiry_ladder == [(SOON_EPOCH, 90)]
        assert metadata["packages_status"] == "ok"
    else:
        assert quota.packages is None
        assert quota.expiry_ladder == ([(SOON_EPOCH, 20)] if summary else None)
        assert metadata["packages_status"] == ("summary_only" if summary else "unavailable")
    assert quota.cycle_end == (SOON_EPOCH if not failure or summary else None)
    assert metadata["next_expiring"] == (
        {"credit": 20, "expires_at": SOON_EPOCH} if summary else None)
