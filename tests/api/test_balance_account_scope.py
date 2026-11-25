"""组/账号绑定的余额和Key实际可用池相同，保留无绑定的旧共享池语义。"""

import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.api.balance import balance_payload, create_router
from src.config import Settings
from src.db.crypto import CredentialCipher
from src.db.groups import CredentialGroupRepository
from src.db.repo import ApiKeyRepository, CredentialRepository
from src.provider.base import Quota
from src.webapp.handlers import register_exception_handlers
from src.webapp.request_id import RequestIdMiddleware
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET


@pytest.fixture
def pool(db):
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    ids = []
    for provider, remaining in [("miora", 2), ("miora", 50), ("codearts", 100)]:
        account = credentials.add(provider=provider, credential_data={"uid": "synthetic"})
        credentials.save_quota(account, Quota(remaining=remaining, total=200,
                                              probed_at=int(time.time())))
        ids.append(account)
    groups = CredentialGroupRepository(db)
    group = groups.create("miora", "Miora selected", ids[:1])
    state = SimpleNamespace(role="admin")
    keys = ApiKeyRepository(db)
    services = SimpleNamespace(settings=Settings(_env_file=None, APP_SECRET=SECRET),
        credentials=credentials, credential_groups=groups, api_keys=keys,
        users=SimpleNamespace(is_active=lambda _: True, role_of=lambda _: state.role),
        user_repo=SimpleNamespace(get=lambda _: {}))
    app = FastAPI()
    app.state.services = services
    app.include_router(create_router(services))
    register_exception_handlers(app)
    app.add_middleware(RequestIdMiddleware)
    return SimpleNamespace(app=app, services=services, ids=ids, group=group,
                           groups=groups, keys=keys, state=state)


async def read(pool, key):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pool.app),
                                base_url="http://testserver") as client:
        return await client.get("/v1/user/balance", headers={"Authorization": "Bearer " + key})


async def test_group_key_balance_changes_with_members_and_refuses_empty_or_deleted(pool):
    key = pool.keys.create("alice", group_ids=[pool.group["id"]])["api_key"]
    response = await read(pool, key)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["balance_infos"][0]["total_balance"] == "2.00"
    assert body["providers"] == [{"provider": "miora", "remaining": 2,
                                  "total": 200, "credentials": 1}]
    pool.groups.update(pool.group["id"], credential_ids=pool.ids[1:2])
    body = (await read(pool, key)).json()
    assert body["balance_infos"][0]["total_balance"] == "50.00"
    pool.groups.update(pool.group["id"], credential_ids=[])
    assert (await read(pool, key)).status_code == 403
    pool.groups.delete(pool.group["id"])
    assert (await read(pool, key)).status_code == 403


async def test_account_key_excludes_other_accounts_even_same_provider(pool):
    key = pool.keys.create("alice", credential_ids=pool.ids[:1])["api_key"]
    assert (await read(pool, key)).json()["balance_infos"][0]["total_balance"] == "2.00"
    pool.services.credentials.set_enabled(pool.ids[0], False)
    body = (await read(pool, key)).json()
    assert body["balance_known"] is False and body["providers"] == []


async def test_channel_binding_without_ids_keeps_only_that_channel(pool):
    key = pool.keys.create("alice", provider_binding="miora")["api_key"]
    body = (await read(pool, key)).json()
    assert body["balance_infos"][0]["total_balance"] == "52.00"
    assert [row["provider"] for row in body["providers"]] == ["miora"]


async def test_empty_scope_preserves_full_pool_and_viewer_hides_provider_details(pool):
    key = pool.keys.create("alice")["api_key"]
    body = (await read(pool, key)).json()
    assert body["balance_infos"][0]["total_balance"] == "152.00"
    pool.state.role = "viewer"
    body = (await read(pool, key)).json()
    assert body["balance_infos"][0]["total_balance"] == "152.00" and body["providers"] == []


def test_direct_payload_scope_intersection_and_unprobed_or_disabled_accounts(pool):
    payload = balance_payload(pool.services, include_providers=True,
        credential_ids=tuple(pool.ids), provider_binding="codearts")
    assert payload["balance_infos"][0]["total_balance"] == "100.00"
    unprobed = pool.services.credentials.add(provider="miora", credential_data={})
    pool.services.credentials.mark_relogin_required(pool.ids[0], "expired")
    payload = balance_payload(pool.services, include_providers=True,
                              credential_ids=(pool.ids[0], unprobed))
    assert payload["balance_known"] is False
    # Cookie/内部调用没有Key作用域参数时保留共享池接口。
    assert balance_payload(pool.services, include_providers=False)["balance_infos"][0][
        "total_balance"] == "150.00"
