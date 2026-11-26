import asyncio

import pytest

from src.api.deps import ServicePaused
from src.compat.openai.request import InvalidRequest
from src.media.auto import NAMES
from tests.media.test_auto import call, identity, run
from tests.media.test_service import request, submissions


class HookPacer:
    def __init__(self, hook):
        self.hook = hook
        self.releases = 0

    async def wait_turn(self, _key):
        self.hook()

    def release(self, _key):
        self.releases += 1


@pytest.mark.parametrize("field", ["gateway_paused", "media_enabled"])
async def test_media_start_rechecks_operator_pause_after_pacer_wait(media_env, field):
    env = media_env
    pacer = HookPacer(lambda: setattr(env.settings, field, field == "gateway_paused"))
    env.manager.provider.pacer = pacer
    expected = ServicePaused if field == "gateway_paused" else InvalidRequest
    with pytest.raises(expected):
        await env.manager.submit(env.principal, request(), idempotency_key="paused-before-post")
    assert not submissions(env)
    assert pacer.releases == 1
    assert not list(env.manager.store.directory.glob("*.json"))
    setattr(env.settings, field, field == "media_enabled")
    env.manager.provider.pacer = HookPacer(lambda: None)
    await env.manager.submit(env.principal, request(), idempotency_key="paused-before-post")
    assert len(submissions(env)) == 1


async def test_fixed_image_size_cannot_be_overridden_by_model_arguments(media_env):
    env = media_env
    payload = {"miora_image_generation": {"size": "1024x1024"}}
    arguments = {"model": "raw-image", "prompt": "cup", "aspect_ratio": "16:9",
                 "resolution": "4K"}
    with identity(env):
        await run(env, payload, [call(NAMES["image"], arguments=arguments)])
    assert submissions(env)[0][-1] == {"count": 1, "aspect_ratio": "1:1", "resolution": "1K"}


@pytest.mark.parametrize("fixed,expected", [
    ({"size": "1024x1024", "resolution": "2K", "aspect_ratio": "4:3"},
     {"count": 1, "resolution": "2K", "aspect_ratio": "4:3"}),
    ({"resolution": "1K"}, {"count": 1, "resolution": "1K", "aspect_ratio": "16:9"}),
    ({"aspect_ratio": "1:1"}, {"count": 1, "resolution": "4K", "aspect_ratio": "1:1"}),
    ({"size": "auto"}, {"count": 1, "resolution": "4K", "aspect_ratio": "16:9"}),
])
async def test_fixed_explicit_options_override_only_model_generated_conflicts(
    media_env, fixed, expected
):
    env = media_env
    arguments = {"model": "raw-image", "prompt": "cup", "size": "1280x720",
                 "aspect_ratio": "16:9", "resolution": "4K"}
    with identity(env):
        await run(env, {"miora_image_generation": fixed},
                  [call(NAMES["image"], arguments=arguments)])
    assert submissions(env)[0][-1] == expected


async def test_cancel_before_submit_does_not_leave_unknown_submission_intent(media_env):
    env = media_env
    waiting = asyncio.Event()

    class Pacer:
        async def wait_turn(self, _key):
            waiting.set()
            await asyncio.Event().wait()

        def release(self, _key):
            pytest.fail("no Pacer slot was acquired")

    env.manager.provider.pacer = Pacer()
    nonce = "cancel-before-post"
    pending = asyncio.create_task(
        env.manager.submit(env.principal, request(), idempotency_key=nonce))
    await waiting.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not submissions(env)
    assert not env.manager.store.path(env.manager.store.intent_id("key", nonce)).exists()


@pytest.mark.parametrize("field", ["gateway_paused", "media_enabled"])
async def test_pause_during_catalog_refresh_still_prevents_generation_post(media_env, field):
    env = media_env
    env.state.on_force = lambda: setattr(env.settings, field, field == "gateway_paused")
    expected = ServicePaused if field == "gateway_paused" else InvalidRequest
    with pytest.raises(expected):
        await env.manager.submit(env.principal, request())
    assert not submissions(env)
    assert not list(env.manager.store.directory.glob("*.json"))


async def test_cancel_during_submit_preserves_unknown_job_and_never_replays(media_env):
    env = media_env
    started = asyncio.Event()
    count = 0

    async def submit(*_args, **_kwargs):
        nonlocal count
        count += 1
        started.set()
        await asyncio.Event().wait()

    env.manager.client.submit = submit
    nonce = "cancel-after-post"
    pending = asyncio.create_task(
        env.manager.submit(env.principal, request(), idempotency_key=nonce))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    job = await env.manager.submit(env.principal, request(), idempotency_key=nonce)
    assert job["status"] == "submitting"
    with pytest.raises(InvalidRequest, match="unknown"):
        await env.manager.refresh(job, env.principal)
    assert count == 1
    assert env.services.inflight.count("a") == 0
