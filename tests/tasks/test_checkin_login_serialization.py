"""按需登录与预刷新共享完整读写锁，取消也不能丢失已轮转的票据。"""

import asyncio
from types import SimpleNamespace

import pytest

from src.db.crypto import CredentialCipher
from src.db.repo import CredentialRepository
from src.provider.base import CheckinResult
from src.tasks.checkin import CheckinTask
from src.tasks.refresh import RefreshTask
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET


@pytest.fixture
def credentials(db):
    return CredentialRepository(db, CredentialCipher(SECRET))


async def test_autologin_and_refresh_read_and_commit_in_order(credentials):
    cid = credentials.add(provider="raccoon", credential_data={
        "access_token": "initial", "refresh_token": "initial-refresh", "uid": "one",
        "phone": "13900000001", "password": "synthetic-password"})
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Provider:
        def checkin_scope(self, data):
            return data["uid"]

        def credential_from(self, data):
            return SimpleNamespace(needs_refresh=lambda *_: True)

        async def checkin(self, data):
            return CheckinResult(ok=data["access_token"] != "initial")

        async def relogin(self, data):
            calls.append(("login", data["access_token"]))
            started.set()
            await release.wait()
            return {**data, "access_token": "login", "refresh_token": "login-refresh"}

        async def refresh(self, data):
            calls.append(("refresh", data["access_token"]))
            return {**data, "access_token": "refresh", "refresh_token": "refresh-refresh"}

    provider = Provider()
    login_task = asyncio.create_task(CheckinTask(credentials, {"raccoon": provider},
        auto_login=True).run_once())
    await asyncio.wait_for(started.wait(), 2)
    refresh_task = asyncio.create_task(RefreshTask(credentials, {"raccoon": provider},
        skew_seconds=60).run_once())
    try:
        # 让预刷新进入持锁阶段；未受保护的旧实现会先用 initial 票据轮转。
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        release.set()
        login_report, refresh_report = await asyncio.wait_for(
            asyncio.gather(login_task, refresh_task), 2)
    assert login_report.succeeded == refresh_report.succeeded == 1
    assert calls == [("login", "initial"), ("refresh", "login")]
    assert credentials.credential_data(cid)["refresh_token"] == "refresh-refresh"


@pytest.mark.parametrize("change", ["replace", "delete", "corrupt"])
async def test_waiting_login_never_overwrites_changed_or_unreadable_credentials(
        credentials, change, caplog):
    original = {"access_token": "initial", "uid": "one"}
    cid = credentials.add(provider="raccoon", credential_data=original)
    candidate = credentials.candidates()[0]
    if change == "replace":
        credentials.save_credential_data(cid, {"access_token": "new-account", "uid": "two"})
    elif change == "delete":
        credentials.delete(cid)
    else:
        with credentials._db.transaction() as conn:
            conn.execute("UPDATE credentials SET data_enc=? WHERE id=?", (b"broken", cid))

    class UnusedProvider:
        async def relogin(self, _data):
            pytest.fail("must not log in using stale credentials")

    task = CheckinTask(credentials, {"raccoon": UnusedProvider()}, auto_login=True)
    failure = CheckinResult(ok=False)
    with caplog.at_level("ERROR"):
        outcome = await task._try_autologin(UnusedProvider(), candidate, original, failure,
                                          "day|one", "day")
    assert outcome == (failure, original) and task._login_days == {}
    assert not credentials.edit_lock(cid).locked()
    if change == "replace":
        assert credentials.credential_data(cid)["uid"] == "two"
    elif change == "delete":
        assert credentials.credential_data(cid) is None
    else:
        assert "凭证解密失败" in caplog.text


async def test_cancelled_autologin_commits_rotated_token_before_releasing_lock(credentials):
    data = {"access_token": "initial", "refresh_token": "old", "uid": "one"}
    cid = credentials.add(provider="raccoon", credential_data=data)
    started, release = asyncio.Event(), asyncio.Event()

    class Provider:
        async def relogin(self, original):
            started.set()
            await release.wait()
            return {**original, "access_token": "new", "refresh_token": "rotated"}

        async def checkin(self, _data):
            return CheckinResult(ok=True)

    provider = Provider()
    task = CheckinTask(credentials, {"raccoon": provider}, auto_login=True)
    operation = asyncio.create_task(task._try_autologin(provider, credentials.candidates()[0],
        data, CheckinResult(ok=False), "day|one", "day"))
    await asyncio.wait_for(started.wait(), 2)
    operation.cancel()
    await asyncio.sleep(0)
    operation.cancel()
    await asyncio.sleep(0)
    assert credentials.edit_lock(cid).locked() and not operation.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 2)
    assert credentials.credential_data(cid)["refresh_token"] == "rotated"
    assert not credentials.edit_lock(cid).locked()


async def test_concurrent_failed_login_cannot_repeat_same_day(credentials):
    data = {"access_token": "initial", "uid": "one"}
    credentials.add(provider="raccoon", credential_data=data)
    candidate = credentials.candidates()[0]
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Provider:
        async def relogin(self, _data):
            calls.append("login")
            started.set()
            await release.wait()
            raise RuntimeError("temporary login failure")

    provider = Provider()
    task = CheckinTask(credentials, {"raccoon": provider}, auto_login=True)
    failure = CheckinResult(ok=False)
    first = asyncio.create_task(task._try_autologin(provider, candidate, data, failure,
                                                   "day|one", "day"))
    await asyncio.wait_for(started.wait(), 2)
    second = asyncio.create_task(task._try_autologin(provider, candidate, data, failure,
                                                    "day|one", "day"))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.wait_for(asyncio.gather(first, second), 2) == [
        (failure, data), (failure, data)]
    assert calls == ["login"]
