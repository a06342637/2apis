"""到期时间超出 JSON 数值/SQLite 整数范围必须在写库前返回客户端错误。"""

import httpx
import pytest

from src.api.admin_keys import _parse_expires_at
from src.compat.openai.request import InvalidRequest
from tests.api.test_api_key_reveal import headers
from tests.api.test_api_key_reveal import reveal_app as reveal_app
from tests.db.test_api_key_reveal import db as db


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"),
    9223372036854775808, float(9223372036854775807), 10**100])
def test_expiry_rejects_out_of_storage_range(value):
    with pytest.raises(InvalidRequest, match="expires_at"):
        _parse_expires_at(value)


@pytest.mark.parametrize("value", [1, 1.5, 4102444800, 9223372036854775807])
def test_expiry_keeps_valid_timestamp_values(value):
    assert _parse_expires_at(value) == int(value)


@pytest.mark.parametrize("method", ["post", "put"])
@pytest.mark.parametrize("literal", ["1e999", "NaN", "Infinity", "9223372036854775808"])
async def test_bad_expiry_returns_400_without_creating_or_updating_key(reveal_app, method, literal):
    app = reveal_app
    before = app.keys.list_for("alice")
    path = "/api/api-keys" if method == "post" else f"/api/api-keys/{app.alice['id']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app,
        raise_app_exceptions=False), base_url="http://testserver") as client:
        response = await client.request(method, path,
            headers=headers(**{"Content-Type": "application/json"}),
            content='{"expires_at":' + literal + '}')
    assert response.status_code == 400, response.text
    assert "expires_at" in response.json()["error"]["message"]
    assert app.keys.list_for("alice") == before
