import asyncio
import base64

import pytest

from src.api import media as mod
from src.compat.openai.request import InvalidRequest
from src.media.service import MediaRateLimited
from src.media.streaming import MediaJSONStream, encode_media_json
from tests.api.test_media import IMAGE, image_body, released, update_settings
from tests.api.test_media import app as _media_fixture

app = _media_fixture


@pytest.mark.parametrize("existing", [False, True])
async def test_zero_grace_streams_real_file_and_holds_quota_until_body_finishes(
    app, monkeypatch, existing
):
    update_settings(app, nonstream_keepalive_seconds=0)
    slots = asyncio.Semaphore(1)
    if existing:
        app.manager.previous = app.manager.add("image")

    async def image_response(job, response_format):
        assert response_format == "b64_json"
        return MediaJSONStream(
            created=job["created_at"],
            paths=[app.manager.store.asset(job["id"], 0)],
            maximum=100,
            slots=slots,
            keepalive=0,
        )

    async def encode(payload):
        async for chunk in encode_media_json(payload):
            assert all(
                lease.released == 0
                for guard in (app.services.key_quota, app.services.user_quota)
                for lease in guard.leases
            )
            yield chunk

    monkeypatch.setattr(app.manager, "image_response", image_response)
    monkeypatch.setattr(mod, "encode_media_json", encode)
    response = await app.client.post(
        "/v1/images/generations",
        json=image_body(response_format="b64_json"),
        headers={"Idempotency-Key": "same-request"},
    )
    assert response.status_code == 200
    assert base64.b64decode(response.json()["data"][0]["b64_json"]) == IMAGE
    assert response.headers["x-accel-buffering"] == "no"
    assert "content-length" not in response.headers
    assert not slots.locked()
    assert len(app.services.key_quota.leases) == (0 if existing else 1)
    released(app)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "error,code",
    [(InvalidRequest("invalid"), 400), (TimeoutError(), 400), (MediaRateLimited(7), 429)],
)
async def test_zero_grace_b64_work_errors_preserve_status_and_release_quota(
    app, existing, error, code
):
    update_settings(app, nonstream_keepalive_seconds=0)
    if existing:
        app.manager.previous = app.manager.add("image")
    app.manager.wait_error = error
    response = await app.client.post(
        "/v1/images/generations",
        json=image_body(response_format="b64_json"),
        headers={"Idempotency-Key": "same-request"},
    )
    assert response.status_code == code
    assert "error" in response.json()
    released(app)


async def test_zero_grace_b64_cancellation_releases_quota(app):
    update_settings(app, nonstream_keepalive_seconds=0)
    app.manager.wait_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await app.client.post(
            "/v1/images/generations", json=image_body(response_format="b64_json")
        )
    released(app)
