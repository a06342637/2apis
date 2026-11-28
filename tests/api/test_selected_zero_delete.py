import asyncio
import time
from types import SimpleNamespace

import pytest

from src.api import admin_credentials
from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.provider.base import Quota, UpstreamHTTPError
from src.request_context import get_credential_id
from tests.api.test_admin_credentials_batch import _add
from tests.api.test_admin_credentials_batch import batch_app as batch_app


def observed(remaining=0, **kwargs):
    return Quota(remaining=remaining, total=100, probed_at=int(time.time()),
                 zero_evidence=True, **kwargs)


def payload(repo, ids):
    return {"action": "delete_zero", "ids": ids, "confirmed": True,
            "zero_tokens": {row["id"]: row["quota_zero_token"] for row in repo.list_all()
                            if row["id"] in ids and row["quota_zero_token"]}}


def test_two_observations_selection_order_concurrency_and_reasons(batch_app):
    app, client = batch_app
    repo = app.state.credentials
    channels = ["miora", "codebuddy", "codearts", "qoder", "trae", "raccoon", "officeace",
                "lingxi", "codebuddy_intl", "zen", "codebuddy", "codebuddy", "codebuddy"]
    ids = [_add(app, provider) for provider in channels]
    for cid in ids:
        repo.save_quota(cid, observed())
    # No observation, stale zero followed by probe failure, and an unselected zero.
    repo.save_quota(ids[10], Quota(remaining=.001, total=100, probed_at=1))
    repo.mark_probe_failed(ids[11])
    requested = ids[:-1] + [ids[0], "missing"]
    body = payload(repo, requested)
    calls, active = [], set()

    async def probe(_data):
        cid = get_credential_id()
        assert not active
        active.add(cid)
        calls.append(cid)
        await asyncio.sleep(0)
        active.remove(cid)
        if cid == ids[2]:
            return observed(0.01)
        if cid == ids[3]:
            return observed(probe_failed=True)
        if cid == ids[4]:
            raise UpstreamHTTPError(401, b"synthetic-private-token")
        if cid == ids[5]:
            return Quota(remaining=0, total=0, probed_at=1)  # synthesized display zero
        if cid == ids[6]:
            # Even another zero observation in the very same second must invalidate this CAS.
            repo.save_quota(cid, observed())
        if cid == ids[7]:
            repo.save_credential_data(cid, {"bearer_token": "synthetic-replacement"})
        return observed()

    registry = app.state.executor._deps.providers
    registry.update(dict.fromkeys(channels, SimpleNamespace(probe_quota=probe)))
    response = client.post("/api/credentials/batch", json=body)
    assert response.status_code == 200 and "synthetic-private-token" not in response.text
    result = response.json()
    assert calls == ids[:9]
    assert [row["id"] for row in result["results"]] == ids[:-1] + ["missing"]
    assert (result["succeeded"], result["skipped"], result["failed"]) == (3, 10, 0)
    deleted = {ids[0], ids[1], ids[8]}
    assert {row["id"] for row in result["results"] if row["ok"]} == deleted
    assert {cid for cid in ids if repo.zero_snapshot(cid) is None} == deleted
    assert repo.zero_snapshot(ids[2])["quota_remaining"] == .01
    assert all(repo.zero_snapshot(cid)["quota_zero_token"] is None for cid in ids[3:6])
    assert all("更新" in row["error"] for row in result["results"][6:8])
    assert {row["target"] for row in app.state.audit.query(action="credential.delete")} == (
        deleted | {None})


@pytest.mark.parametrize("invalid", [{"confirmed": False}, {"confirmed": 1}, {"zero_tokens": []}])
def test_explicit_confirmation_and_snapshot_required(batch_app, invalid):
    app, client = batch_app
    cid = _add(app)
    app.state.credentials.save_quota(cid, observed())
    body = payload(app.state.credentials, [cid]) | invalid
    assert client.post("/api/credentials/batch", json=body).status_code == 400
    assert app.state.credentials.zero_snapshot(cid) is not None


def test_stale_observation_edit_inflight_and_deadline_skip_without_probing(batch_app, monkeypatch):
    app, client = batch_app
    repo = app.state.credentials
    cid = _add(app)
    repo.save_quota(cid, observed())
    body = payload(repo, [cid])

    def skip(expected):
        row = client.post("/api/credentials/batch", json=body).json()["results"][0]
        assert row["skipped"] and expected in row["error"]

    repo.save_quota(cid, observed())
    skip("记录已变化")
    body = payload(repo, [cid])
    lock = repo.edit_lock(cid)
    asyncio.run(lock.acquire())
    skip("正在更新")
    lock.release()
    monkeypatch.setattr(admin_credentials, "ZERO_RECHECK_BUDGET_SECONDS", 0)
    skip("时间已到")
    assert app.state.executor._deps.providers["codebuddy"].probed == []
    assert repo.zero_snapshot(cid) is not None


def test_schema_upgrade_does_not_promote_legacy_zero(repo):
    credentials, db = repo
    cid = credentials.add(provider="miora", credential_data={"uid": "synthetic"})
    credentials.save_quota(cid, observed())
    conn = db.connect()
    conn.execute("ALTER TABLE credentials DROP COLUMN quota_zero_token")
    conn.execute("PRAGMA user_version = 45")
    apply_schema(conn)
    apply_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    row = credentials.zero_snapshot(cid)
    assert row["quota_remaining"] == 0 and row["quota_zero_token"] is None
