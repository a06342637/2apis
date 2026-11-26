import asyncio
from types import MethodType

import pytest

from src.provider.base import Event, EventKind, InvalidModelName, Model
from src.provider.miora.client import MioraClient
from src.provider.miora.credential import MioraCredential

pytestmark = pytest.mark.real_upstream("miora")


@pytest.mark.parametrize("change", ["updated", "removed"])
async def test_model_selection_is_revalidated_after_waiting_for_account_lock(change):
    client = MioraClient()
    credential = MioraCredential("synthetic", uid="account")
    account = client._account(credential)
    account.models = [Model("text")]
    account.selections = {"text": ("old-model", "old-tier")}
    observed = []

    async def stream(self, _credential, _account, _payload, _prompt, selected):
        observed.append(selected)
        yield Event(kind=EventKind.CONTENT, content="reply")

    client._stream_session = MethodType(stream, client)
    await account.lock.acquire()

    async def collect():
        return [event async for event in client.stream_chat(credential, {}, "text")]

    pending = asyncio.create_task(collect())
    await asyncio.sleep(0)
    account.selections = {"text": ("new-model", "new-tier")} if change == "updated" else {}
    account.lock.release()
    try:
        if change == "updated":
            assert (await pending)[0].content == "reply"
            assert observed == [("new-model", "new-tier")]
        else:
            with pytest.raises(InvalidModelName):
                await pending
            assert not observed
        assert account.users == 0 and not account.lock.locked()
    finally:
        await client.aclose()
