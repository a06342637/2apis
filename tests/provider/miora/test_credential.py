from __future__ import annotations

import base64
import json

import pytest

from src.compat.openai.request import InvalidRequest
from src.provider.miora.credential import MioraCredential, jwt_subject, token_from_input


def jwt(payload: object) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


@pytest.mark.parametrize(
    "value", ["", "plain", "a..c", "a.!.c", "a._w.c", jwt([]), jwt({}), jwt({"sub": 123})]
)
def test_subject_invalid(value):
    assert jwt_subject(value) == ""


def test_subject_and_expiry():
    token = jwt({"sub": " user ", "iat": 1000.1, "exp": 2000.8})
    credential = MioraCredential.from_import(
        {
            "authToken": token,
            "refreshToken": " refresh ",
            "uid": "wrong",
            "projectId": "project",
        }
    )
    assert credential.access_token == token
    assert credential.uid == "user"
    assert credential.expires_at == 2000
    assert credential.project_id == "project"
    assert credential.refresh_token == "refresh"
    assert credential.identity() == "user"
    assert MioraCredential.from_dict(credential.to_dict()) == credential
    assert MioraCredential.from_dict({"access_token": token, "expires_at": 1}).expires_at == 2000


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", ""),
        (" 'plain' ", "plain"),
        ("Bearer token", "token"),
        ("bearer    token ", "token"),
        ("Bearer ", ""),
        ("Cookie: authToken=token; other=bad", "token"),
        ("authToken=token", "token"),
        ("other=bad; missing; authToken=; authToken=good", "good"),
        ("Cookie: other=bad", ""),
        ("other=bad", ""),
        ("Cookie: plain", "plain"),
    ],
)
def test_token_input(source, expected):
    assert token_from_input(source) == expected


@pytest.mark.parametrize(
    "raw", [None, "token", [], {}, {"authToken": ""}, {"authToken": 5}, {"cookie": "other=x"}]
)
def test_import_requires_token(raw):
    assert MioraCredential.from_dict(raw).access_token == ""
    with pytest.raises(InvalidRequest, match="authToken"):
        MioraCredential.from_import(raw)


def test_snake_case_aliases_identity_and_optional_fields():
    credential = MioraCredential.from_dict(
        {
            "access_token": "plain",
            "refresh_token": "r",
            "user_id": "u",
            "expires_at": 3000,
            "workflowId": "w",
            "ignoredSecret": "discard",
        }
    )
    assert credential.to_dict() == {
        "access_token": "plain",
        "refresh_token": "r",
        "uid": "u",
        "expires_at": 3000,
        "project_id": "w",
    }
    assert MioraCredential.from_dict({"accessToken": "token", "userId": "u"}).uid == "u"
    assert MioraCredential.from_dict({"token": "token"}).to_dict() == {"access_token": "token"}
    assert MioraCredential.from_dict({"raw": "Cookie: authToken=t"}).access_token == "t"
    assert MioraCredential(access_token="").identity() == ""
    anonymous = MioraCredential(access_token="private-token")
    assert anonymous.identity().startswith("token-")
    assert "private-token" not in anonymous.identity()
    assert anonymous.identity() == MioraCredential(access_token="private-token").identity()
    assert anonymous.identity() != MioraCredential(access_token="other-token").identity()
    for expiry in (float("inf"), float("nan")):
        assert (
            MioraCredential.from_dict({"access_token": "t", "expires_at": expiry}).expires_at == 0
        )


def test_refresh_window(monkeypatch):
    credential = MioraCredential(access_token="t", refresh_token="r", expires_at=100)
    assert credential.needs_refresh(10, now=90)
    assert not credential.needs_refresh(10, now=89)
    assert not credential.needs_refresh(-20, now=99)
    assert credential.needs_refresh(-20, now=100)
    monkeypatch.setattr("src.provider.miora.credential.time.time", lambda: 90)
    assert credential.needs_refresh(10)
    assert not MioraCredential(access_token="t", expires_at=100).needs_refresh(10, now=90)
    assert not MioraCredential(access_token="t", refresh_token="r").needs_refresh(10, now=90)
    assert not MioraCredential(access_token="t", refresh_token="r", expires_at=-1).needs_refresh(10)


@pytest.mark.parametrize(
    "field", ["cookie", "raw", "authToken", "access_token", "accessToken", "token"]
)
def test_cookie_import_keeps_its_refresh_token_for_pre_refresh(field):
    token = jwt({"sub": "cookie-account", "exp": 100})
    credential = MioraCredential.from_import({
        field: f"Cookie: other=ignored; authToken={token}; refreshToken= ; refreshToken=renew",
    })
    assert credential.access_token == token
    assert credential.refresh_token == "renew"
    assert credential.uid == "cookie-account"
    assert credential.needs_refresh(10, now=90)
    assert "other" not in credential.to_dict()


def test_explicit_refresh_token_wins_and_unselected_cookie_is_not_combined():
    assert MioraCredential.from_import({
        "cookie": "authToken=one; refreshToken=cookie-renew",
        "refreshToken": "explicit-renew",
    }).refresh_token == "explicit-renew"
    assert MioraCredential.from_import({
        "authToken": "one", "cookie": "authToken=other; refreshToken=other-renew",
    }).refresh_token == ""


@pytest.mark.parametrize("access_name,refresh_name", [
    ("authToken", "refreshToken"), ("authtoken", "refreshtoken"), ("AUTHTOKEN", "REFRESHTOKEN"),
])
def test_cookie_token_parsing_matches_frontend_case_and_quotes(access_name, refresh_name):
    credential = MioraCredential.from_import({
        "cookie": f'Cookie: {access_name}="access"; {refresh_name}=\'renew\'',
    })
    assert credential.access_token == "access"
    assert credential.refresh_token == "renew"
