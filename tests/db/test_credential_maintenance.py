"""后台维护资格按ID现读；软暂停和冷却不会丢失token续期。"""

import pytest

from src.db.crypto import CredentialCipher
from src.db.repo import CredentialRepository
from src.engine.scheduler import ErrorOutcome
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET


@pytest.mark.parametrize("state,allowed", [("active", True), ("paused", True),
    ("cooling", True), ("disabled", False), ("deleted", False), ("missing", False)])
def test_maintenance_requires_existing_account_without_hard_disable(db, state, allowed):
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    cid = credentials.add(provider="miora", credential_data={"access_token": "synthetic"})
    if state == "paused":
        credentials.set_enabled(cid, False)
    elif state == "cooling":
        credentials.save_error(cid, ErrorOutcome(cooling_until=10000))
    elif state == "disabled":
        credentials.mark_relogin_required(cid, "synthetic invalid refresh token")
    elif state == "deleted":
        credentials.delete(cid)
    elif state == "missing":
        cid = "missing"
    assert credentials.maintenance_allowed(cid) is allowed
