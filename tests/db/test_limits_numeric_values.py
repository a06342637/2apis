"""已落库的非法按模型数值/深层JSON不应拖垮整份护栏快照。"""

import pytest

from src.db.limits import LimitsRepository
from tests.db.test_api_key_reveal import db as db


@pytest.mark.parametrize("text,expected", [
    ('{"bad":Infinity,"model":17}', {"model": 17}),
    ('{"bad":1e999,"model":17}', {"model": 17}),
    ('{"bad":NaN,"model":17}', {"model": 17}),
    ('{"bad":' + '[' * 10000 + '0' + ']' * 10000 + '}', {}),
])
def test_dirty_numeric_model_limit_can_be_loaded_without_breaking_other_limits(db, text, expected):
    repo = LimitsRepository(db)
    repo.set("account", daily_tokens=100, per_model_tokens={"model": 17})
    with db.transaction() as conn:
        conn.execute("UPDATE credential_limits SET per_model_tokens=? WHERE credential_id=?",
                     (text, "account"))
    for record in (repo.get("account"), repo.map()["account"]):
        assert record.daily_tokens == 100 and record.per_model_tokens == expected
