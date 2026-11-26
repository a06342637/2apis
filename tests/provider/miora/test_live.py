import asyncio
from types import MethodType

import httpx
import pytest

from src.compat.openai.request import InvalidRequest
from src.provider.base import Event, EventKind, InvalidModelName, Model
from src.provider.miora import MioraProvider
from src.provider.miora.client import MioraClient
from src.provider.miora.credential import MioraCredential
from src.provider.miora.live import current_credential
from src.request_context import credential_scope
from src.tasks.pacer import Pacer
from tests.provider.miora.test_provider import Client


def test_no_reader_or_request_context_retains_low_level_credential():
    original = MioraCredential("old", uid="u")
    with credential_scope("account"):
        assert current_credential(original, None) is original
    with credential_scope(None):
        assert current_credential(original, lambda: None) is original


def test_same_identity_uses_new_ticket_and_preserves_read_errors():
    with credential_scope("account"):
        current = current_credential(MioraCredential("old", uid="u"),
            lambda: {"access_token": "new", "uid": "u"})
        assert current.access_token == "new"

        def unavailable():
            raise OSError("database unavailable")

        with pytest.raises(OSError, match="database"):
            current_credential(current, unavailable)


@pytest.mark.parametrize("data", [None, {}, {"access_token": "new", "uid": "another"}])
def test_removed_invalid_or_changed_identity_is_not_used(data):
    with credential_scope("account"), pytest.raises(InvalidRequest):
        current_credential(MioraCredential("old", uid="u"), lambda: data)


@pytest.mark.parametrize("data", [None, {"access_token": "new", "uid": "another"}])
async def test_failed_reload_releases_pacer_without_opening_inner_stream(data):
    client = Client()
    provider = MioraProvider(client=client, reload_credential=lambda: data,
        pacer=Pacer(0, 0, allow_concurrent=True, max_concurrency=1))
    with credential_scope("account"), pytest.raises(InvalidRequest):
        [event async for event in provider.stream_chat({"access_token": "old", "uid": "u"},
                                                       {}, "text")]
    assert provider.pacer._inflight == {}
    assert not client.seen


async def test_actual_client_receives_reload_hook_and_reads_fresh_rest_header():
    seen, checks = [], []

    def response(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"code": 0, "data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as short:
        client = MioraClient(short_client=short)
        original = MioraCredential("old", uid="u")
        account = client._account(original)
        account.models = [Model("text")]
        account.selections = {"text": ("raw", "")}

        async def stream(self, credential, _account, _payload, _prompt, _selected):
            await self._request(credential, "GET", "/metadata")
            yield Event(kind=EventKind.CONTENT, content="ok")

        client._stream_session = MethodType(stream, client)
        provider = MioraProvider(client=client,
            reload_credential=lambda: {"access_token": "new", "uid": "u"},
            before_inference=lambda: checks.append(1))
        with credential_scope("account"):
            assert [event.content async for event in provider.stream_chat(original.to_dict(),
                                                                          {}, "text")] == ["ok"]
        assert seen == ["Bearer new"]
        assert client.reload_credential is provider.reload_credential
        assert client.before_inference is provider.before_inference
        assert checks == [1, 1]
        await client.aclose()


@pytest.mark.parametrize("change", ["token", "project", "identity", "pause"])
async def test_client_account_lock_wait_rechecks_token_project_and_identity(change):
    client = MioraClient()
    original = MioraCredential("old", uid="u", project_id="p")
    account = client._account(original)
    account.models = [Model("text")]
    account.selections = {"text": ("raw", "")}
    data = original.to_dict()
    client.reload_credential = lambda: data
    seen = []
    paused = False

    def guard():
        if paused:
            raise InvalidRequest("account paused")

    client.before_inference = guard

    async def stream(self, credential, _account, _payload, _prompt, _selected):
        seen.append(credential.access_token)
        yield Event(kind=EventKind.CONTENT, content="ok")

    client._stream_session = MethodType(stream, client)
    await account.lock.acquire()

    async def collect():
        return [event async for event in client.stream_chat(original, {}, "text")]

    with credential_scope("account"):
        task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    data = {**data, "access_token": "new"}
    if change == "project":
        data["project_id"] = "new-project"
    elif change == "identity":
        data["uid"] = "new-user"
    elif change == "pause":
        paused = True
    account.lock.release()
    if change == "token":
        assert (await task)[0].content == "ok"
        assert seen == ["new"]
    else:
        expected = InvalidModelName if change == "project" else InvalidRequest
        with pytest.raises(expected):
            await task
        assert not seen
    assert not account.lock.locked() and account.users == 0
    await client.aclose()


async def test_provider_rechecks_pause_after_pacer_wait_and_releases_slot():
    client = Client()
    paused, released = False, []

    class Pacer:
        async def wait_turn(self, _identity):
            nonlocal paused
            paused = True

        def release(self, _identity):
            released.append(1)

    def guard():
        if paused:
            raise InvalidRequest("account paused")

    provider = MioraProvider(client=client, pacer=Pacer(), before_inference=guard)
    with pytest.raises(InvalidRequest, match="paused"):
        [event async for event in provider.stream_chat({"access_token": "old", "uid": "u"},
                                                       {}, "text")]
    assert not client.seen and released == [1]
