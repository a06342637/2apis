"""额度探测任务。"""

from __future__ import annotations

from src.tasks.pacer import Pacer
from src.tasks.quota_probe import QuotaProbeTask
from tests.tasks._support import ProbeProvider


async def test_quota_probe_updates_health(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()})
    report = await task.run_once()
    assert report.succeeded == 1
    assert credentials.candidates()[0].health == 80

async def test_quota_probe_failure_marks_unknown_not_zero(repo):
    """探测失败 → health=NULL（unknown），绝不当作 0 分（三态）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider(error=RuntimeError("boom"))})
    report = await task.run_once()
    assert report.failed == 1
    assert credentials.candidates()[0].health is None

async def test_quota_probe_dead_401_marks_relogin_required(repo):
    """401 = 登录态死亡：额度探测直接硬禁用并写可操作原因，不停在「未知」。"""
    from src.provider.lingxi.client import UpstreamHTTPError

    credentials, _db = repo
    credential_id = credentials.add(
        provider="lingxi", credential_data={"wps_sid": "sid", "uid": "u1"})
    task = QuotaProbeTask(
        credentials, {"lingxi": ProbeProvider(error=UpstreamHTTPError(401, b""))})
    report = await task.run_once()

    assert report.failed == 1
    row = next(r for r in credentials.list_all() if r["id"] == credential_id)
    assert row["disabled"]
    assert "WPS" in row["disabled_reason"]
    # 硬禁用后不再进候选池，下一轮也不会再探
    assert credentials.candidates(selectable_only=True) == []


async def test_quota_probe_skips_unknown_provider_and_missing_data(repo):
    credentials, _db = repo
    credentials.add(provider="ghost", credential_data={"x": 1})
    task = QuotaProbeTask(credentials, {})
    report = await task.run_once()
    assert report.skipped == 1

async def test_quota_probe_uses_pacer(repo):
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    turns: list[int] = []

    class CountingPacer(Pacer):
        async def wait_turn(self) -> None:
            turns.append(1)

    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()},
                          CountingPacer(1, 1))
    await task.run_once()
    assert len(turns) == 1
    turns.clear()
    await task.run_once(apply_pacing=False)
    assert turns == []

async def test_quota_probe_skips_when_credential_vanished(repo):
    """凭证在探测前被删除 → 计入 skipped（background 93-96）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials, {"codebuddy": ProbeProvider()})
    original = credentials.credential_data
    credentials.credential_data = lambda _cid: None       # type: ignore[method-assign]
    report = await task.run_once()
    credentials.credential_data = original                # type: ignore[method-assign]
    assert report.skipped == 1


async def test_quota_probe_maintains_paused_but_skips_hard_disabled_credentials(repo):
    """暂停账号仍查额度；已失效的硬禁用账号不再打上游。"""
    from src.engine.scheduler import ErrorOutcome

    credentials, _db = repo
    alive = credentials.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    dead = credentials.add(provider="codebuddy", credential_data={"bearer_token": "d"})
    paused = credentials.add(provider="codebuddy", credential_data={"bearer_token": "p"})
    credentials.save_error(dead, ErrorOutcome(disabled=True))
    credentials.set_enabled(paused, False)
    provider = ProbeProvider()
    task = QuotaProbeTask(credentials, {"codebuddy": provider})

    report = await task.run_once()

    assert report.attempted == 2
    assert report.succeeded == 2
    assert report.skipped == 1
    healths = {c.credential_id: c.health for c in credentials.candidates()}
    assert healths[dead] is None
    assert healths[paused] == 80
    assert healths[alive] == 80
    assert next(row for row in credentials.list_all() if row["id"] == paused)["enabled"] == 0


async def test_quota_probe_scopes_credential_for_proxy_stickiness(repo):
    """探活必须在「该凭证」的上下文里发（出口 IP 与推理一致）。"""
    from src.request_context import get_credential_id

    credentials, _db = repo
    credential_id = credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    seen: list[str | None] = []

    class _Scoped:
        async def probe_quota(self, data):
            seen.append(get_credential_id())
            from src.provider.base import Quota
            return Quota(remaining=1, total=2, probed_at=1)

    task = QuotaProbeTask(credentials, {"codebuddy": _Scoped()})
    await task.run_once()
    assert seen == [credential_id]
    # 退出循环后归还上下文，不把凭证 id 泄漏给后续任务
    assert get_credential_id() is None


async def test_quota_probe_log_includes_type_when_message_empty(repo, caplog):
    """裸 `TimeoutError` 的 str 为空，日志必须补类名（否则只剩「: 」）。"""
    credentials, _db = repo
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    task = QuotaProbeTask(credentials,
                          {"codebuddy": ProbeProvider(error=TimeoutError())})
    with caplog.at_level("WARNING", logger="src.tasks.quota_probe"):
        report = await task.run_once()
    assert report.failed == 1
    message = next(r.getMessage() for r in caplog.records
                   if "quota probe failed" in r.getMessage())
    assert message.endswith("TimeoutError")
