"""真实Miora认证刷新不能换账号，硬禁用后排队维护不能再消费同一坏票。"""

import asyncio

import httpx
import pytest

from src.db.crypto import CredentialCipher
from src.db.repo import CredentialRepository
from src.provider.miora import MioraProvider
from src.provider.miora.client import MioraClient
from src.provider.miora.credential import MioraCredential
from src.tasks.refresh import RefreshTask
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET
from tests.provider.miora.test_credential import jwt


@pytest.fixture
def credentials(db):
    return CredentialRepository(db, CredentialCipher(SECRET))


def old_account(credentials):
    data = MioraCredential.from_import({
        "authToken": jwt({"sub": "account-a", "iat": 1, "exp": 10}),
        "refreshToken": "synthetic-refresh"}).to_dict()
    return credentials.add(provider="miora", credential_data=data), data


@pytest.mark.parametrize("new_token", [jwt({"sub": "account-b", "iat": 1000, "exp": 10000}),
                                     jwt({"iat": 1000, "exp": 10000}), "opaque-new-token"])
async def test_invalid_refresh_identity_never_changes_stored_account(credentials, new_token):
    cid, before = old_account(credentials)

    def response(request):
        assert request.url.path == "/api/auth/refresh-token"
        return httpx.Response(200, json={
            "access_token": new_token,
            "refresh_token": "synthetic-new-refresh"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as short:
        provider = MioraProvider(client=MioraClient(short_client=short))
        report = await RefreshTask(credentials, {"miora": provider},
            skew_seconds=60, now=lambda: 1000).run_once()
        assert report.failed == 1 and report.succeeded == 0
        assert credentials.credential_data(cid) == before
        assert credentials.candidates()[0].disabled
        await provider.aclose()


@pytest.mark.parametrize("state", ["paused", "disabled"])
async def test_refresh_rechecks_maintenance_state_after_waiting_for_edit_lock(credentials, state):
    cid, before = old_account(credentials)
    requests, paced = [], []

    class Pacer:
        async def wait_turn(self):
            paced.append(1)

    def response(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={
            "access_token": jwt({"sub": "account-a", "iat": 1000, "exp": 10000}),
            "refresh_token": "synthetic-new-refresh"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as short:
        provider = MioraProvider(client=MioraClient(short_client=short))
        task = RefreshTask(credentials, {"miora": provider},
            skew_seconds=60, now=lambda: 1000, pacer=Pacer())
        lock = credentials.edit_lock(cid)
        await lock.acquire()
        operation = asyncio.create_task(task.run_once())
        await asyncio.sleep(0)
        if state == "paused":
            credentials.set_enabled(cid, False)
        else:
            credentials.mark_relogin_required(cid, "synthetic invalid token")
        lock.release()
        report = await asyncio.wait_for(operation, 2)
        assert paced == [1]
        if state == "paused":
            assert len(requests) == report.succeeded == 1
            assert not credentials.candidates()[0].enabled
        else:
            assert requests == [] and report.skipped == 1
            assert credentials.credential_data(cid) == before
        await provider.aclose()


async def test_paused_account_still_refreshes_and_preserves_pause(credentials):
    cid, before = old_account(credentials)
    credentials.set_enabled(cid, False)

    def response(_request):
        return httpx.Response(200, json={
            "access_token": jwt({"sub": "account-a", "iat": 1000, "exp": 10000}),
            "refresh_token": "synthetic-new-refresh"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as short:
        provider = MioraProvider(client=MioraClient(short_client=short))
        report = await RefreshTask(credentials, {"miora": provider},
            skew_seconds=60, now=lambda: 1000).run_once()
        assert report.succeeded == 1 and report.failed == 0
        stored = credentials.credential_data(cid)
        assert stored["uid"] == before["uid"] and stored["refresh_token"] == "synthetic-new-refresh"
        assert stored["expires_at"] == 10000
        assert not credentials.candidates()[0].enabled and not credentials.candidates()[0].disabled
        await provider.aclose()


async def test_queued_refresh_does_not_repeat_after_terminal_authentication_failure(credentials):
    cid, before = old_account(credentials)
    entered, finish = asyncio.Event(), asyncio.Event()
    requests = []

    async def response(request):
        requests.append(request.url.path)
        entered.set()
        await finish.wait()
        return httpx.Response(401, json={"error": "invalid refresh token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as short:
        provider = MioraProvider(client=MioraClient(short_client=short))
        task = RefreshTask(credentials, {"miora": provider},
            skew_seconds=60, now=lambda: 1000)
        first = asyncio.create_task(task.run_once(credential_id=cid))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(task.run_once(credential_id=cid))
        try:
            await asyncio.sleep(0)
        finally:
            finish.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert len(requests) == 1
        assert results[0].failed == 1 and results[1].skipped == 1
        assert credentials.credential_data(cid) == before
        assert credentials.candidates()[0].disabled
        await provider.aclose()
