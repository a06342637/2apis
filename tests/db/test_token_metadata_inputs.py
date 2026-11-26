"""无效JWT元数据安全降级，不阻断真实数据库中的凭证导入和刷新保存。"""

import base64

import pytest

from src.db.crypto import CredentialCipher
from src.db.repo import CredentialRepository
from src.provider.miora.credential import MioraCredential
from src.provider.token_expiry import credential_expiry, jwt_times, normalize_epoch
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET
from tests.provider.miora.test_credential import jwt


def deep_token():
    payload = '{"sub":' + '[' * 10000 + '"nested"' + ']' * 10000 + '}'
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"header.{body}.signature"


INVALID_EPOCHS = [float("inf"), float("-inf"), float("nan"), 2**63, 10**30, 1e300]


@pytest.mark.parametrize("token,expected", [(deep_token(), (0, 0)),
    *[(jwt({"sub": "account", "iat": value, "exp": value}), (0, 0))
      for value in INVALID_EPOCHS],
    (jwt({"sub": "account", "iat": float("inf"), "exp": 1000}), (0, 1000)),
    (jwt({"sub": "account", "iat": 100, "exp": float("nan")}), (100, 0))])
def test_jwt_metadata_can_be_imported_and_saved_in_real_database(db, token, expected):
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    raw = {"access_token": token}
    imported = MioraCredential.from_import(raw).to_dict()
    assert imported["access_token"] == token
    assert jwt_times(token) == expected
    for data in (raw, imported):
        cid = credentials.add(provider="miora", credential_data=data)
        credentials.save_credential_data(cid, data)
        metadata = db.connect().execute(
            "SELECT token_issued_at,token_expires_at FROM credentials WHERE id=?",
            (cid,)).fetchone()
        assert tuple(metadata) == expected


@pytest.mark.parametrize("field", ["expires_at", "expiresAt", "expiration"])
@pytest.mark.parametrize("value", INVALID_EPOCHS)
def test_invalid_explicit_time_is_unknown_or_falls_back_to_valid_jwt(db, field, value):
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    raw = {"access_token": "opaque-token", field: value}
    assert MioraCredential.from_import(raw).expires_at == 0
    cid = credentials.add(provider="miora", credential_data=raw)
    credentials.save_credential_data(cid, raw)
    assert db.connect().execute(
        "SELECT token_expires_at FROM credentials WHERE id=?", (cid,)).fetchone()[0] == 0
    assert credential_expiry({**raw, "access_token": jwt({"exp": 1000})}) == 1000


@pytest.mark.parametrize("value,expected", [(0, 0), (-1, 0), (0.5, 0), (1.5, 1),
    (1700000000, 1700000000), (1700000000000, 1700000000),
    (2**63 - 1, (2**63 - 1) // 1000)])
def test_ordinary_seconds_milliseconds_and_integer_boundary(value, expected):
    assert normalize_epoch(value) == expected
