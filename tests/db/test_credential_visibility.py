"""普通登录用户可选择账号，但共享池昵称不会泄露给非运维角色。"""

from src.db.crypto import CredentialCipher
from src.db.repo import CredentialRepository
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET


def test_account_list_hides_nickname_without_changing_stored_account(db):
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    account = credentials.add(provider="miora", credential_data={"uid": "synthetic-owner"},
                              nickname="synthetic-private-email@example.invalid")
    visible = credentials.list_all(include_nickname=True)[0]
    hidden = credentials.list_all(include_nickname=False)[0]
    assert visible["nickname"] == "synthetic-private-email@example.invalid"
    assert hidden == {**visible, "nickname": ""}
    assert "data_enc" not in hidden
    assert credentials.nickname_of(account) == visible["nickname"]
    assert credentials.credential_data(account) == {"uid": "synthetic-owner"}
