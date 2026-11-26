"""真实导入路由与数据库应容忍不可用的到期元数据，不产生 500。"""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.api.admin_credentials import create_router
from src.api.deps import csrf_protected, principal_from_request
from src.auth.rbac import Principal
from src.db.crypto import CredentialCipher
from src.db.repo import AuditRepository, CredentialRepository
from src.provider.codearts import CodeArtsProvider
from src.provider.officeace import OfficeAceProvider
from src.webapp.handlers import register_exception_handlers
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET


@pytest.fixture
async def import_app(db):
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    registry = {"codearts": CodeArtsProvider(), "officeace": OfficeAceProvider()}
    services = SimpleNamespace(credentials=credentials, registry=registry,
        schedule_probe=lambda _cid: None, audit=AuditRepository(db))
    app = FastAPI()
    app.include_router(create_router(services))
    register_exception_handlers(app)
    app.dependency_overrides[principal_from_request] = lambda: Principal("root", True)
    app.dependency_overrides[csrf_protected] = lambda: None
    try:
        yield app, credentials
    finally:
        for provider in registry.values():
            await provider.aclose()


@pytest.mark.parametrize("provider", ["codearts", "officeace"])
@pytest.mark.parametrize("literal", ["1e999", "NaN", "-Infinity", "-1"])
@pytest.mark.parametrize("batch", [False, True])
async def test_unusable_expiration_imports_as_unknown(import_app, provider, literal, batch):
    app, credentials = import_app
    raw = {"access_key_id": "test-ak", "secret_access_key": "test-sk",
           "model_app_key": "test-key", "model_app_secret": "test-secret",
           "model_base_url": "https://gateway.example.com"}
    # 用实际 JSON 数字溢出覆盖解析器；httpx json= 会提前拒绝非有限值。
    entry = ('{"provider":' + json.dumps(provider) + ',"credential":'
             + json.dumps(raw)[:-1] + ',"expiration":' + literal + '}}')
    body = '{"items":[' + entry + ']}' if batch else entry
    path = "/api/credentials/batch-import" if batch else "/api/credentials"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,
        raise_app_exceptions=False), base_url="http://testserver") as client:
        response = await client.post(path, content=body,
                                     headers={"Content-Type": "application/json"})
    assert response.status_code == 200, response.text
    result = response.json()
    if batch:
        assert result["failed"] == [] and result["skipped"] == []
        result = result["created"][0]
    assert credentials.credential_data(result["id"])["expiration"] == 0
