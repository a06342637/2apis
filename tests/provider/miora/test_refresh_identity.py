"""刷新只轮转当前账号的票，旧身份未知时允许由新JWT补全。"""

import httpx
import pytest

from src.provider.base import UpstreamReloginRequired
from src.provider.miora.client import MioraClient
from src.provider.miora.credential import MioraCredential
from tests.provider.miora.test_credential import jwt


@pytest.mark.parametrize("old_uid,new_uid", [("same", "same"), ("", "known"), ("", "")])
async def test_known_refresh_identity_and_unknown_identity_metadata(old_uid, new_uid):
    token = jwt({"sub": new_uid, "iat": 1000, "exp": 2000})
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        200, json={"access_token": token, "refresh_token": "synthetic-new"}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http)
        original = MioraCredential("old", "synthetic-old", old_uid, 100, "project")
        refreshed = await client.refresh_token(original)
        assert refreshed["access_token"] == token and refreshed["expires_at"] == 2000
        assert refreshed.get("uid", "") == new_uid
        assert refreshed["project_id"] == "project"
        assert original.refresh_token == "synthetic-old" and original.uid == old_uid
        await client.aclose()


async def test_refresh_rejects_different_jwt_identity_without_echoing_tokens():
    token = jwt({"sub": "another", "iat": 1000, "exp": 2000})
    transport = httpx.MockTransport(lambda _request: httpx.Response(
        200, json={"access_token": token, "refresh_token": "synthetic-secret"}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http)
        original = MioraCredential(jwt({"sub": "original", "exp": 100}),
                                   "synthetic-old", "original", 100, "project")
        with pytest.raises(UpstreamReloginRequired, match="身份不一致") as caught:
            await client.refresh_token(original)
        assert token not in str(caught.value) and "synthetic-secret" not in str(caught.value)
        assert original.uid == "original" and original.refresh_token == "synthetic-old"
        await client.aclose()


@pytest.mark.parametrize("token", ["opaque-new-token", jwt({"exp": 2000}),
                                    jwt({"sub": 123, "exp": 2000})])
@pytest.mark.parametrize("explicit_uid", [None, "original"])
async def test_known_account_requires_identity_in_new_token_itself(token, explicit_uid):
    data = {"access_token": token, "refresh_token": "synthetic-new"}
    if explicit_uid is not None:
        data["uid"] = explicit_uid
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=data))
    async with httpx.AsyncClient(transport=transport) as http:
        client = MioraClient(short_client=http)
        original = MioraCredential.from_import({
            "authToken": jwt({"sub": "original", "exp": 100}),
            "refreshToken": "synthetic-old"})
        with pytest.raises(UpstreamReloginRequired, match="身份不一致"):
            await client.refresh_token(original)
        assert original.uid == "original" and original.refresh_token == "synthetic-old"
        await client.aclose()
