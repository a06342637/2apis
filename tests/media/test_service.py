import asyncio
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException

from src.auth.rbac import UnauthorizedError
from src.media.request import parse_media_request
from src.media.service import MediaRateLimited, MediaService
from src.media.streaming import encode_media_json
from src.provider.base import UpstreamProtocolViolation
from src.provider.miora.client import MioraHTTPError


def request(kind="image", model=None):
    return parse_media_request(
        {
            "model": model or ("pretty-picture" if kind == "image" else "pretty-video"),
            "prompt": "draw a cat",
        },
        kind,
    )


def submissions(env):
    return [call for call in env.state.calls if call[0] == "submit"]


async def test_standard_id_maps_to_real_account_and_b64_with_cache(media_env):
    env = media_env
    job = await env.manager.submit(env.principal, request(), idempotency_key="once")
    assert job["model"] == "raw-image" and job["public_model"] == "pretty-picture"
    assert submissions(env)[0][1:4] == ("a", "image", "raw-image")
    assert env.state.records[0]["model"] == "pretty-picture"
    complete = await env.manager.wait(job, env.principal)
    payload = await env.manager.image_response(complete, "b64_json")
    decoded = json.loads(b"".join([frame async for frame in encode_media_json(payload)]))
    assert decoded == {
        "created": job["created_at"],
        "data": [{"b64_json": "aW1hZ2U="}],
    }
    links = await env.manager.image_response(complete, "url")
    assert "signature=" in links["data"][0]["url"]
    await env.manager.refresh(complete, env.principal)
    assert len([call for call in env.state.calls if call[0] == "download"]) == 1
    previous = await env.manager.submit(env.principal, request(), idempotency_key="once")
    assert previous["id"] == job["id"] and len(submissions(env)) == 1
    assert not env.manager.locks
    assert env.services.inflight.count("a") == 0


async def test_force_provider_raw_names_and_bound_fair_selection(media_env):
    env = media_env
    env.principal = replace(env.principal, credential_ids=("a", "b"))
    env.state.key["credential_ids"] = ["a", "b"]
    one = await env.manager.submit(env.principal, request(model="raw-image@miora"))
    two = await env.manager.submit(env.principal, request(model="Pretty Picture"))
    assert {one["credential_id"], two["credential_id"]} == {"a", "b"}
    assert (await env.manager.models(env.principal, "a", None))[0].id == "raw-image"
    env.services.inflight = None
    env.state.key["credential_ids"] = []
    unbound = replace(env.principal, credential_ids=())
    assert (await env.manager.submit(unbound, request()))["credential_id"] == "a"
    env.manager.stats = None
    env.manager.record(unbound, one, ok=False)
    default = MediaService(env.services)
    assert default.client is env.client and default.downloader.hosts == (
        "sign.miorausercontent.com",
    )


@pytest.mark.parametrize(
    "field,value",
    [("key_present", False), ("active", False), ("expires_at", 1), ("other_id", True)],
)
def test_key_revalidation(media_env, field, value):
    env = media_env
    if field == "expires_at":
        env.state.key[field] = value
    elif field == "other_id":
        env.state.key["id"] = "other"
    else:
        setattr(env.state, field, value)
    with pytest.raises(UnauthorizedError):
        env.manager.fresh_principal(env.principal)


@pytest.mark.parametrize(
    "mutation",
    [
        "binding",
        "ids",
        "key_models",
        "user_models",
        "missing",
        "health",
        "remaining",
        "cooling",
        "disabled",
        "limit",
        "account_models",
        "old_models",
    ],
)
async def test_strict_key_user_account_policy(media_env, mutation):
    env = media_env
    if mutation == "binding":
        env.state.key["provider_binding"] = "codearts"
    elif mutation == "ids":
        env.state.key["credential_ids"] = ["b"]
    elif mutation in {"key_models", "old_models"}:
        if mutation == "key_models":
            env.state.key["allowed_models"] = "pretty-video"
        else:
            env.principal = replace(env.principal, allowed_models="pretty-video")
    elif mutation == "user_models":
        env.state.policy["allowed_models"] = "pretty-video"
    elif mutation == "missing":
        env.state.rows = []
    elif mutation in {"health", "remaining", "cooling", "disabled"}:
        updates = {
            "health": {"health": 0},
            "remaining": {"quota_remaining": 0},
            "cooling": {"cooling_until": 9999999999},
            "disabled": {"disabled": True},
        }
        env.state.rows[1] = replace(env.state.rows[1], **updates[mutation])
    elif mutation == "limit":
        env.state.blocked = True
    else:
        env.state.account_models["a"] = "pretty-video"
    with pytest.raises(ValueError):
        await env.manager.submit(env.principal, request(), account_id="a")
    assert not submissions(env)


async def test_original_scope_remains_narrow_if_key_widens(media_env):
    env = media_env
    env.state.key["credential_ids"] = []
    with pytest.raises(ValueError):
        await env.manager.submit(env.principal, request(), account_id="b")
    env.state.policy = None
    assert env.manager.fresh_principal(env.principal).user_allowed_models == ""


async def test_idempotency_conflicts_are_per_key_and_unknown_submit_never_replayed(media_env):
    env = media_env
    assert env.manager.existing(env.principal, request(), "missing") is None
    job = await env.manager.submit(env.principal, request(), idempotency_key="once")
    changed = request()
    changed["prompt"] = "different"
    with pytest.raises(HTTPException) as error:
        env.manager.existing(env.principal, changed, "once")
    assert error.value.status_code == 409
    env.manager.store.save({**job, "status": "submitting"})
    with pytest.raises(ValueError, match="unknown"):
        await env.manager.refresh(job, env.principal)
    assert len(submissions(env)) == 1


async def test_catalog_changes_after_pacer_and_empty_or_blocked_models(media_env):
    env = media_env
    with pytest.raises(ValueError, match="only"):
        await env.manager.submit(env.principal, request(model="raw-image@codearts"))
    with pytest.raises(ValueError, match="no selected"):
        await env.manager.submit(env.principal, request(model="missing"))
    env.settings.model_blocklist = " , raw-image, "
    with pytest.raises(ValueError):
        await env.manager.submit(env.principal, request())
    env.settings.model_blocklist = ""
    env.state.on_force = lambda: env.state.models.clear()
    with pytest.raises(ValueError, match="no longer available"):
        await env.manager.submit(env.principal, request())
    assert not submissions(env)


async def test_missing_credential_at_all_read_points(media_env, monkeypatch):
    env = media_env
    env.state.data.pop("a")
    with pytest.raises(ValueError, match="credential"):
        await env.manager.describe(env.principal, "a")
    env.state.data["a"] = {"id": "a"}
    original = env.services.credentials.credential_data
    calls = 0

    def read(account):
        nonlocal calls
        calls += 1
        # direct: describe→data before paced→describe(force)→generate data。
        return None if calls == 2 else original(account)

    monkeypatch.setattr(env.services.credentials, "credential_data", read)
    with pytest.raises(ValueError, match="credential"):
        await env.manager.submit(env.principal, request(), account_id="a")
    calls = 0

    def final_read(account):
        nonlocal calls
        calls += 1
        return None if calls == 4 else original(account)

    monkeypatch.setattr(env.services.credentials, "credential_data", final_read)
    with pytest.raises(ValueError, match="credential"):
        await env.manager.submit(env.principal, request(), account_id="a")


@pytest.mark.parametrize("status", [429, 500, "network", "rejected"])
async def test_submit_failures_record_unknown_usage_and_no_replay(media_env, status):
    env = media_env
    if isinstance(status, int):
        env.state.submit_error = MioraHTTPError(status, b"failure", retry_after_seconds=120)
    elif status == "network":
        env.state.submit_error = OSError("network")
    else:
        env.state.failed = True
    if status == 429:
        with pytest.raises(MediaRateLimited) as error:
            await env.manager.submit(env.principal, request())
        assert error.value.retry_after == 120 and len(env.state.errors) == 1
        assert env.state.errors[0][1].cooling_kind == "soft"
    else:
        job = await env.manager.submit(env.principal, request())
        assert job["status"] == "failed"
        assert env.manager.public_job(job)["error"]["code"] == "media_generation_failed"
    assert len(submissions(env)) == 1 and env.state.records[-1]["ok"] is False
    assert "credit" not in env.state.records[-1]
    assert env.services.inflight.count("a") == 0


async def test_auto_uses_existing_pacer_lease_rechecks_daily_and_stats_write_failure(media_env):
    env = media_env
    env.state.stats_error = True
    await env.manager.submit(env.principal, request("video"), account_id="a", already_paced=True)
    assert ("key_quota",) in env.state.calls and ("user_quota",) in env.state.calls
    assert env.services.inflight.count("a") == 0
    env.settings.media_enabled = False
    with pytest.raises(ValueError, match="disabled"):
        await env.manager.submit(env.principal, request())


async def test_inflight_guard_denies_selected_account_after_pacer(media_env, monkeypatch):
    env = media_env
    env.services.inflight.acquire("a")
    with pytest.raises(ValueError, match="no selected"):
        await env.manager.submit(env.principal, request())
    with pytest.raises(ValueError, match="concurrency"):
        await env.manager.submit(env.principal, request(), account_id="a")


async def test_private_job_ownership_and_paused_account_readonly(media_env):
    env = media_env
    job = await env.manager.submit(env.principal, request("video"))
    for principal in (
        replace(env.principal, username="guest"),
        replace(env.principal, key_id="other"),
    ):
        with pytest.raises(HTTPException) as error:
            env.manager.owned(job["id"], principal)
        assert error.value.status_code == 404
    env.state.rows[1] = replace(env.state.rows[1], enabled=False, health=0, quota_remaining=0)
    ready = await env.manager.wait(job, env.principal)
    assert ready["status"] == "completed"
    assert env.manager.public_job(ready)["seconds"] is None
    ready["options"]["duration"] = 1
    assert env.manager.public_job(ready)["seconds"] == "1"


async def test_polling_then_failure_missing_assets_and_timeout(media_env, monkeypatch):
    env = media_env
    env.state.statuses = ["in_progress", "completed"]
    job = await env.manager.submit(env.principal, request())
    assert (await env.manager.wait(job, env.principal))["status"] == "completed"
    env.manager.store.save({**job, "status": "failed"})
    with pytest.raises(ValueError, match="job failed"):
        await env.manager.wait(job, env.principal)
    env.manager.store.save({**job, "status": "completed", "files": []})
    with pytest.raises(UpstreamProtocolViolation, match="没有资源"):
        await env.manager.refresh(job, env.principal)
    env.manager.store.save({**job, "status": "in_progress", "files": []})
    env.state.statuses = ["in_progress"]
    env.settings.media_timeout_seconds = 0.002
    with pytest.raises(TimeoutError):
        await env.manager.wait(job, env.principal)


async def test_poll_and_download_missing_credentials_and_aggregate_limits(media_env):
    env = media_env
    job = await env.manager.submit(env.principal, request())
    env.state.data.pop("a")
    with pytest.raises(ValueError, match="credential"):
        await env.manager.refresh(job, env.principal)
    env.manager.store.save({**job, "status": "completed", "files": [{"file_key": "0"}]})
    with pytest.raises(ValueError, match="credential"):
        await env.manager.refresh(job, env.principal)
    env.state.data["a"] = {"id": "a"}
    env.state.blobs = [b"one", b"two"]
    env.manager.store.save({**job, "status": "in_progress"})
    ready = await env.manager.wait(job, env.principal)
    assert [call for call in env.state.calls if call[0] == "download"] == [
        ("download", 100),
        ("download", 97),
    ]
    env.settings.media_max_bytes = 1
    with pytest.raises(ValueError, match="aggregate"):
        await env.manager.refresh(ready, env.principal)
    with pytest.raises(ValueError, match="aggregate"):
        await env.manager.image_response(ready, "b64_json")


async def test_task_result_cannot_replace_identity_or_authorized_model(media_env):
    env = media_env
    job = await env.manager.submit(env.principal, request())
    changed = env.manager.save_result(
        job,
        {
            "id": "other",
            "username": "guest",
            "credential_id": "b",
            "key_id": "other",
            "model": "foreign",
            "status": "completed",
        },
    )
    assert changed["id"] == job["id"] and changed["credential_id"] == "a"
    assert changed["model"] == "raw-image" and changed["public_model"] == "pretty-picture"


async def test_lock_waiter_cleanup_and_cancelled_waiter(media_env):
    manager = media_env.manager
    first = asyncio.Event()
    released = asyncio.Event()

    async def hold():
        async with manager.job_lock("same"):
            first.set()
            await released.wait()

    task = asyncio.create_task(hold())
    await first.wait()
    waiter = asyncio.create_task(hold())
    await asyncio.sleep(0)
    assert manager.locks["same"]["users"] == 2
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert manager.locks["same"]["users"] == 1
    released.set()
    await task
    assert not manager.locks


async def test_nullable_health_remaining_and_old_binding(media_env):
    env = media_env
    env.state.rows[1] = replace(env.state.rows[1], health=None, quota_remaining=None)
    assert (await env.manager.submit(env.principal, request(), account_id="a"))[
        "credential_id"
    ] == "a"
    with pytest.raises(ValueError):
        await env.manager.submit(
            replace(env.principal, provider_binding="trae"), request(), account_id="a"
        )
