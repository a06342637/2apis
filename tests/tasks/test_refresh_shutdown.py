"""停止服务时排空已提交的令牌轮转，先落库再关闭上游与 SQLite。"""

from __future__ import annotations

import asyncio

import pytest

from src.provider.base import UpstreamReloginRequired
from src.stats.collector import StatsCollector
from src.tasks.checkin import CheckinTask
from src.tasks.quota_probe import QuotaProbeTask
from src.tasks.refresh import RefreshTask, _wait_committed
from src.tasks.retention import RetentionTask
from src.tasks.runner import TaskRunner
from tests.tasks._support import ProbeProvider

OLD = {"bearer_token": "old", "refresh_token": "old-rt", "auth_source": "oauth", "expires_at": 1}
NEW = {
    "bearer_token": "new",
    "refresh_token": "new-rt",
    "auth_source": "oauth",
    "expires_at": 10000,
}


class ControlledProvider(ProbeProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.reply = asyncio.get_running_loop().create_future()
        self.closed = False

    async def refresh(self, data):
        assert data["refresh_token"] == "old-rt"
        self.entered.set()
        return await self.reply

    async def aclose(self):
        assert self.reply.done() and not self.reply.cancelled()
        self.closed = True


def setup(repo):
    credentials, db = repo
    account = credentials.add(provider="codebuddy", credential_data=OLD)
    credentials.set_enabled(account, False)
    provider = ControlledProvider()
    providers = {"codebuddy": provider}
    refresh = RefreshTask(credentials, providers, skew_seconds=100, now=lambda: 1000)
    runner = TaskRunner(
        refresh=refresh,
        quota_probe=QuotaProbeTask(credentials, providers),
        checkin=CheckinTask(credentials, providers),
        retention=RetentionTask(StatsCollector(db)),
    )
    return credentials, account, provider, refresh, runner


async def finish(provider, *tasks):
    """断言失败时也释放人工挂起点，不留下排空任务。"""
    if not provider.reply.done():
        provider.reply.set_result(NEW)
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancelled_refresh_commits_rotation_under_edit_lock_and_keeps_pause(repo):
    credentials, account, provider, refresh, _runner = setup(repo)
    request = asyncio.create_task(refresh.run_once())
    await provider.entered.wait()
    try:
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done() and not provider.reply.cancelled()
        assert credentials.edit_lock(account).locked()
        # 第二次取消仍不能打断正在消费一次性票据的请求。
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done() and not provider.reply.cancelled()
        provider.reply.set_result(NEW)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 2)
        assert credentials.credential_data(account) == NEW
        assert credentials.list_all()[0]["enabled"] == 0
        assert not credentials.edit_lock(account).locked()
        assert refresh._rotations == set()
        await refresh.drain()
        await provider.aclose()
        assert provider.closed
    finally:
        await finish(provider, request)


@pytest.mark.parametrize("terminal", [False, True])
async def test_cancelled_refresh_records_real_upstream_failure_before_close(repo, terminal):
    credentials, account, provider, refresh, _runner = setup(repo)
    request = asyncio.create_task(refresh.run_once())
    await provider.entered.wait()
    try:
        request.cancel()
        await asyncio.sleep(0)
        error = UpstreamReloginRequired("expired refresh") if terminal else RuntimeError("network")
        provider.reply.set_exception(error)
        with pytest.raises(asyncio.CancelledError):
            await request
        row = credentials.list_all()[0]
        assert row["enabled"] == 0 and row["disabled"] == int(terminal)
        assert credentials.credential_data(account) == OLD
        assert not credentials.edit_lock(account).locked()
        await provider.aclose()
    finally:
        await finish(provider, request)


@pytest.mark.parametrize("periodic", [False, True])
async def test_runner_stop_waits_for_periodic_and_manual_refresh_commits(repo, periodic):
    credentials, account, provider, refresh, runner = setup(repo)
    if periodic:
        request = asyncio.create_task(
            runner._loop("refresh", refresh.run_once, lambda: 0, key="refresh")
        )
        runner._tasks.append(request)
    else:
        request = asyncio.create_task(runner.refresh_credential(account))
    await provider.entered.wait()
    stopping = asyncio.create_task(runner.stop())
    try:
        await asyncio.sleep(0)
        assert not stopping.done() and not provider.reply.cancelled()
        provider.reply.set_result(NEW)
        await asyncio.wait_for(stopping, 2)
        assert credentials.credential_data(account) == NEW
        assert credentials.list_all()[0]["enabled"] == 0
        assert runner._tasks == []
        if periodic:
            assert request.cancelled()
        else:
            assert (await request).succeeded == 1
        await provider.aclose()
        assert provider.closed
    finally:
        await finish(provider, request, stopping)


async def test_cancelling_stop_still_drains_manual_rotation_before_propagating(repo):
    credentials, account, provider, _refresh, runner = setup(repo)
    request = asyncio.create_task(runner.refresh_credential(account))
    await provider.entered.wait()
    stopping = asyncio.create_task(runner.stop())
    try:
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done() and not provider.reply.cancelled()
        provider.reply.set_result(NEW)
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert (await request).succeeded == 1
        assert credentials.credential_data(account) == NEW
        await provider.aclose()
    finally:
        await finish(provider, request, stopping)


async def test_commit_failure_is_logged_and_reported_by_explicit_drain(repo, monkeypatch, caplog):
    credentials, account, provider, refresh, _runner = setup(repo)

    def fail_commit(_id, _data):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(credentials, "save_credential_data", fail_commit)
    request = asyncio.create_task(refresh.run_once())
    await provider.entered.wait()
    draining = asyncio.create_task(refresh.drain())
    try:
        await asyncio.sleep(0)
        provider.reply.set_result(NEW)
        for task in (request, draining):
            with pytest.raises(RuntimeError, match="database unavailable"):
                await task
        assert "refresh credential commit failed" in caplog.text
        assert credentials.credential_data(account) == OLD
        assert not credentials.edit_lock(account).locked()
        await provider.aclose()
    finally:
        await finish(provider, request, draining)


async def test_self_cancelled_upstream_operation_does_not_spin_forever():
    operation = asyncio.get_running_loop().create_future()
    waiter = asyncio.create_task(_wait_committed(operation))
    await asyncio.sleep(0)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, 2)


async def test_drain_stops_new_rotations_after_current_commit_and_resume_is_explicit(repo):
    credentials, _account, provider, refresh, _runner = setup(repo)
    credentials.add(provider="codebuddy", credential_data=OLD)
    request = asyncio.create_task(refresh.run_once())
    await provider.entered.wait()
    draining = asyncio.create_task(refresh.drain())
    try:
        await asyncio.sleep(0)
        provider.reply.set_result(NEW)
        assert (await request).succeeded == 1
        await draining
        data = [credentials.credential_data(row.credential_id) for row in credentials.candidates()]
        assert data.count(NEW) == 1 and data.count(OLD) == 1
        assert (await refresh.run_once()).attempted == 0
        refresh.resume()
        assert (await refresh.run_once()).succeeded == 1
        assert sorted(row["enabled"] for row in credentials.list_all()) == [0, 1]
    finally:
        await finish(provider, request, draining)


async def test_waiting_edit_lock_does_not_start_new_upstream_refresh_after_drain(repo):
    credentials, account, provider, refresh, _runner = setup(repo)
    lock = credentials.edit_lock(account)
    await lock.acquire()
    request = asyncio.create_task(refresh.run_once())
    try:
        await asyncio.sleep(0)
        await refresh.drain()
        lock.release()
        assert (await request).skipped == 1
        assert not provider.entered.is_set()
        assert credentials.credential_data(account) == OLD
    finally:
        if lock.locked():
            lock.release()
        await finish(provider, request)
