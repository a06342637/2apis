from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.provider.base import UpstreamProtocolViolation
from src.provider.miora import media as mod
from src.provider.miora.client import MioraClient, MioraHTTPError
from src.provider.miora.credential import MioraCredential
from src.provider.miora.media import (
    FILE_HOST,
    FILM_API,
    MEDIA_CATALOG_API,
    SESSION_TITLE,
    MediaModel,
    MioraMediaClient,
    MioraMediaRequestError,
    generation_body,
    normalize_task,
    parse_catalog,
)

pytestmark = pytest.mark.real_upstream("miora_media")

IMAGE_CAPS = {
    "supportedTools": ["text_to_image", "edit_image"],
    "aspectRatios": ["1:1", "16:9"],
    "allowedResolutions": ["1K", "2K"],
    "defaultAspectRatio": "1:1",
    "defaultResolution": "1K",
}
VIDEO_CAPS = {
    "supportsTextToVideo": True,
    "aspectRatios": ["16:9", "1:1"],
    "resolutions": ["720P", "1080P"],
    "defaultAspectRatio": "16:9",
    "defaultResolution": "720P",
    "defaultSeconds": 5,
    "secondsRange": {"min": 1, "max": 8},
    "supportsAudioControl": False,
}


def row(kind="image", **values):
    return {
        "modelId": kind,
        "label": f"Test {kind}",
        "contentType": kind,
        "visible": True,
        "available": True,
        "capabilities": copy.deepcopy(IMAGE_CAPS if kind == "image" else VIDEO_CAPS),
        **values,
    }


def model(kind="image", **caps):
    return MediaModel(
        kind,
        kind,
        kind,
        kind,
        {
            **copy.deepcopy(IMAGE_CAPS if kind == "image" else VIDEO_CAPS),
            **caps,
        },
    )


def task(kind="image", **values):
    return {
        "id": "t1",
        "sessionId": "s1",
        "mediaType": kind,
        "modelName": kind,
        "status": "processing",
        "slots": [],
        **values,
    }


def completed_slot(kind="image", **values):
    return {
        "status": "completed",
        "fileKey": f"file/user/result.{kind}",
        "fileType": kind,
        "assetDeleted": False,
        **values,
    }


class Api:
    def __init__(self):
        self.calls = []
        self.catalogue = {"data": [row(), row("video")]}
        self.sessions = {"items": []}
        self.create_response = {"session": {"id": "s1"}, "task": task()}
        self.progress = [task()]
        self.signed = {"signedUrls": [f"https://{FILE_HOST}/file/user/result?sign=test"]}
        self.failures = {}

    def __call__(self, request):
        payload = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, payload, dict(request.headers)))
        if path in self.failures:
            raise self.failures[path]
        if path == MEDIA_CATALOG_API:
            data = self.catalogue
        elif path == f"{FILM_API}/sessions" and request.method == "GET":
            assert request.url.params["pageSize"] == "50"
            assert request.url.params["keyword"] == SESSION_TITLE
            data = self.sessions
        elif path == f"{FILM_API}/sessions":
            data = self.create_response
        elif path.endswith("/tasks"):
            data = task(
                payload["mediaType"], sessionId=path.split("/")[-2], modelName=payload["model"]
            )
        elif path.endswith("/update"):
            data = {}
        elif path.endswith("/progress"):
            data = self.progress
        elif path.endswith("/cdn-sign-url"):
            data = self.signed
        else:
            raise AssertionError(f"unexpected mocked endpoint {path}")
        return httpx.Response(200, json={"code": 0, "data": data})


@pytest.fixture
async def clients():
    api = Api()
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as short:
        client = MioraClient(short_client=short)
        yield api, MioraMediaClient(client), MioraCredential("test", uid="user")


async def test_full_display_catalog_cannot_submit_locked_models(clients):
    api, client, credential = clients
    api.catalogue = {
        "data": [
            row(),
            row("video", available=False, requiredTier="standard", unavailableReason="tier_locked"),
        ]
    }
    displayed = await client.catalog(credential, include_unavailable=True)
    assert [model.id for model in displayed] == ["image", "video"]
    assert displayed[1].account_available is False
    assert [model.id for model in await client.catalog(credential)] == ["image"]
    with pytest.raises(MioraMediaRequestError):
        await client.submit(credential, "video", "video", "A cup")
    assert all(call[1] == MEDIA_CATALOG_API for call in api.calls)


@pytest.mark.parametrize("data", [None, [], {}, {"data": {}}])
def test_catalog_rejects_invalid_envelope(data):
    with pytest.raises(UpstreamProtocolViolation):
        parse_catalog(data)


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        row(visible=False),
        row(available=False),
        row(contentType="audio"),
        row(contentType=[]),
        row(modelId=""),
        row(capabilities=None),
        row(editOnly=True),
        row(capabilities={}),
        row(capabilities={"supportedTools": "text_to_image"}),
        row("video", capabilities={"supportsTextToVideo": False}),
    ],
)
def test_catalog_only_offers_available_plain_text_generation(invalid):
    assert parse_catalog({"data": [invalid]}) == {}


def test_catalog_variants_defaults_and_names():
    caps = {
        **IMAGE_CAPS,
        "variants": [
            None,
            {},
            {"modelId": "no", "available": False},
            {"modelId": "low", "label": "Low"},
            {"modelId": "high", "isDefault": True},
        ],
        "defaultVariantId": "high",
    }
    parsed = parse_catalog({"data": [row(label="", capabilities=caps), row("video")]})
    assert list(parsed) == ["image", "low", "high", "video"]
    assert parsed["image"].raw_id == "high"
    assert parsed["image"].name == "image"
    assert parsed["low"].name == "Low"
    assert parsed["high"].name == "high"
    del caps["defaultVariantId"]
    assert parse_catalog({"data": [row(capabilities=caps)]})["image"].raw_id == "high"
    caps["variants"][4].pop("isDefault")
    assert parse_catalog({"data": [row(capabilities=caps)]})["image"].raw_id == "low"
    assert (
        parse_catalog({"data": [row(modelId=None, modelKey="fallback")]})["fallback"].raw_id
        == "fallback"
    )


@pytest.mark.parametrize(
    "variants, offered",
    [
        (None, True),
        ({}, True),
        ([], True),
        ([None], False),
        ([{"modelId": "unavailable", "available": False}], False),
    ],
)
def test_empty_or_unavailable_variants(variants, offered):
    assert (
        bool(parse_catalog({"data": [row(capabilities={**IMAGE_CAPS, "variants": variants})]}))
        == offered
    )


def test_public_snapshot_all_available_models_have_working_default_bodies():
    fixture = Path(__file__).parent / "fixtures" / "media-catalog.json"
    parsed = parse_catalog(json.loads(fixture.read_text(encoding="utf-8-sig")))
    assert len(parsed) == 43
    assert {choice.id for choice in parsed.values() if choice.kind == "video"} == {
        "minimax-h3-max",
        "vidu-q2-pro-video",
    }
    for choice in parsed.values():
        body = generation_body(choice, "A ceramic cup")
        assert body["model"] == choice.raw_id
        assert body["mediaType"] == choice.kind
        assert choice.model().gateway_endpoints == mod.ENDPOINTS[choice.kind]


def test_image_body_is_exact_observed_protocol():
    assert generation_body(model(), "hello", count=2, resolution="2K", aspect_ratio="16:9") == {
        "count": 2,
        "prompt": "hello",
        "mediaType": "image",
        "taskType": "image_text",
        "model": "image",
        "editPayload": {
            "imageParams": {
                "type": "text_to_image",
                "model": "image",
                "prompt": "hello",
                "extraParameters": {"aspectRatio": "16:9", "resolution": "2K"},
            }
        },
    }


@pytest.mark.parametrize(
    "options",
    [
        {"count": True},
        {"count": 1.5},
        {"count": 0},
        {"count": 5},
        {"aspect_ratio": 1},
        {"aspect_ratio": "missing"},
        {"resolution": 1},
        {"resolution": "missing"},
        {"duration": 1},
        {"generate_audio": False},
    ],
)
def test_image_invalid_options_are_explicitly_rejected(options):
    with pytest.raises(MioraMediaRequestError):
        generation_body(model(), "hello", **options)


@pytest.mark.parametrize("prompt", [None, "", "  "])
def test_prompt_must_be_nonempty_text(prompt):
    with pytest.raises(MioraMediaRequestError):
        generation_body(model(), prompt)


@pytest.mark.parametrize("limit", [None, False, 0, -1, float("nan"), float("inf")])
def test_missing_or_invalid_prompt_limits_do_not_invent_limits(limit):
    assert generation_body(model(promptLengthLimit=limit), "hello")["prompt"] == "hello"


def test_declared_prompt_limit_is_enforced():
    with pytest.raises(MioraMediaRequestError):
        generation_body(model(promptLengthLimit=2), "hello")


def test_video_declared_defaults_and_audio_capabilities():
    body = generation_body(model("video"), "hello")
    params = body["editPayload"]["videoParams"]
    assert params == {
        "type": "text_to_video",
        "model": "video",
        "prompt": "hello",
        "duration": 5,
        "extraParameters": {"aspectRatio": "16:9", "resolution": "720P"},
    }
    for audio in (None, True, False):
        body = generation_body(
            model("video", supportsAudioControl=True), "hello", generate_audio=audio
        )
        assert body["editPayload"]["videoParams"]["generateAudio"] == (audio or False)
    params = generation_body(model("video", supportsAspectRatioParam=False), "hello")[
        "editPayload"
    ]["videoParams"]
    assert params["extraParameters"] == {"resolution": "720P"}


@pytest.mark.parametrize(
    "options",
    [
        {"duration": True},
        {"duration": 0},
        {"duration": 1.1},
        {"duration": 9},
        {"generate_audio": True},
        {"generate_audio": 1},
    ],
)
def test_video_invalid_parameters(options):
    with pytest.raises(MioraMediaRequestError):
        generation_body(model("video"), "hello", **options)


def test_video_discrete_resolution_and_range_constraints():
    with pytest.raises(MioraMediaRequestError):
        generation_body(model("video", allowedSeconds=[4, 5]), "hello", duration=1)
    with pytest.raises(MioraMediaRequestError):
        generation_body(model("video", secondsRange={"min": 2, "max": 8}), "hello", duration=1)
    with pytest.raises(MioraMediaRequestError):
        generation_body(
            model("video", allowedSecondsByResolution={"1080P": [4]}), "hello", resolution="1080P"
        )
    assert generation_body(model("video", allowedSecondsByResolution=[]), "hello")
    with pytest.raises(MioraMediaRequestError):
        generation_body(model("video", supportsAspectRatioParam=False), "hello", aspect_ratio="1:1")


@pytest.mark.parametrize(
    "caps",
    [
        {"allowedSeconds": [True]},
        {"allowedSeconds": ["bad"]},
        {"secondsRange": {"min": None, "max": 8}},
        {"secondsRange": {"min": 1, "max": None}},
        {"secondsRange": {"min": 8, "max": 1}},
        {"secondsRange": None},
    ],
)
def test_video_malformed_duration_metadata_is_protocol_error(caps):
    with pytest.raises(UpstreamProtocolViolation):
        generation_body(model("video", **caps), "hello")


def test_video_discrete_seconds_without_range_are_supported():
    assert generation_body(model("video", secondsRange=None, allowedSeconds=[5]), "hello")


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        task(id=""),
        task(sessionId=""),
        task(mediaType="audio"),
        task(mediaType=[]),
        task(status="new"),
        task(status=[]),
        task(slots={}),
        task(slots=[None]),
        task(slots=[{}]),
        task(slots=[{"status": []}]),
        task(slots=[{"status": "new"}]),
        task(slots=[completed_slot(fileKey="")]),
        task(slots=[completed_slot(fileType="audio")]),
        task(slots=[completed_slot(fileType="video")]),
    ],
)
def test_malformed_tasks_never_become_success(invalid):
    with pytest.raises(UpstreamProtocolViolation):
        normalize_task(invalid)


def test_task_identity_is_verified_before_exposing_files():
    with pytest.raises(UpstreamProtocolViolation):
        normalize_task(task(), session_id="another")
    with pytest.raises(UpstreamProtocolViolation):
        normalize_task(task(), task_id="another")


def test_completed_file_metadata_and_missing_optional_numbers():
    result = normalize_task(
        task(
            status="completed",
            slots=[
                completed_slot(width=1024, height=1024, duration=1),
                completed_slot(width=0, height=True, duration=float("inf")),
            ],
        ),
        session_id="s1",
        task_id="t1",
    )
    assert result["status"] == "completed"
    assert result["progress"] == 100
    assert result["files"][0] == {
        "file_key": "file/user/result.image",
        "kind": "image",
        "width": 1024,
        "height": 1024,
        "duration": 1,
    }
    assert result["files"][1] == {"file_key": "file/user/result.image", "kind": "image"}
    assert "credit" not in result and "error" not in result


def test_failed_or_deleted_tasks_remain_failed():
    result = normalize_task(
        task(status="failed", slots=[{"status": "failed", "errorMessage": "额度不足"}])
    )
    assert result["error"] == "额度不足" and result["status"] == "failed"
    assert normalize_task(task(status="failed"))["error"] == "Miora 媒体生成失败"
    result = normalize_task(task(status="completed", slots=[completed_slot(assetDeleted=True)]))
    assert result["status"] == "failed" and result["files"] == []


def test_progress_is_finite_clamped_and_averaged():
    result = normalize_task(
        task(
            status="pending",
            slots=[
                {"status": "pending", "progress": 30},
                {"status": "processing", "progress": 300},
                {"status": "processing", "progress": -10},
                {"status": "processing", "progress": "bad"},
            ],
        )
    )
    assert result["status"] == "queued" and result["progress"] == 32
    assert normalize_task(task())["progress"] == 0


async def test_catalog_cache_and_token_rotation_do_not_create_another_account(clients, monkeypatch):
    api, media, credential = clients
    clock = [0]
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    models = await media.catalog(credential)
    assert len(models) == 2 and models[0].gateway_endpoints == ("/v1/images/generations",)
    await media.catalog(MioraCredential("rotated", uid="user"))
    assert len(api.calls) == 1
    await media.catalog(credential, force=True)
    assert len(api.calls) == 2
    clock[0] = 301
    await media.catalog(credential)
    assert len(api.calls) == 3
    api.catalogue = {"data": []}
    clock[0] = 602
    assert await media.catalog(credential) == []
    assert await media.catalog(credential) == []
    assert len(api.calls) == 4


async def test_account_cache_bounds_preserve_active_and_new_accounts(clients, monkeypatch):
    _, media, _ = clients
    monkeypatch.setattr(mod, "MAX_ACCOUNTS", 2)
    first = media._account(MioraCredential("a", uid="a"))
    second = media._account(MioraCredential("b", uid="b"))
    await first.lock.acquire()
    await second.lock.acquire()
    assert media._account(MioraCredential("c", uid="c"))
    assert len(media._accounts) == 3
    first.lock.release()
    second.lock.release()
    media._account(MioraCredential("d", uid="d"))
    assert list(media._accounts) == ["c", "d"]


async def test_first_submission_and_following_request_reuse_one_session(clients):
    api, media, credential = clients
    first = await media.submit(credential, "image", "image", "hello")
    second = await media.submit(credential, "video", "video", "world", duration=1)
    assert first["task_id"] == "t1" and second["kind"] == "video"
    creates = [call for call in api.calls if call[:2] == ("POST", f"{FILM_API}/sessions")]
    assert len(creates) == 1 and creates[0][2]["autoTitle"] is True
    renames = [call for call in api.calls if call[1].endswith("/update")]
    assert renames[0][2] == {"title": SESSION_TITLE}
    assert all(call[3]["authorization"] == "Bearer test" for call in api.calls)


async def test_parallel_first_requests_create_only_one_named_session(clients):
    api, media, credential = clients
    await asyncio.gather(
        media.submit(credential, "image", "image", "one"),
        media.submit(credential, "image", "image", "two"),
    )
    assert sum(call[:2] == ("POST", f"{FILM_API}/sessions") for call in api.calls) == 1


async def test_existing_named_session_is_reused_after_process_restart(clients):
    api, media, credential = clients
    api.sessions = {
        "items": [
            None,
            {"id": "other", "title": "unrelated"},
            {"id": "existing", "title": SESSION_TITLE},
        ]
    }
    result = await media.submit(credential, "image", "image", "hello")
    assert result["session_id"] == "existing"
    assert not any(call[:2] == ("POST", f"{FILM_API}/sessions") for call in api.calls)


@pytest.mark.parametrize(
    "kind, model_id, options",
    [
        ("image", "missing", {}),
        ("image", "video", {}),
        ("audio", "image", {}),
        ("image", "image", {"seed": 1}),
    ],
)
async def test_invalid_generation_never_creates_task(clients, kind, model_id, options):
    api, media, credential = clients
    with pytest.raises(MioraMediaRequestError):
        await media.submit(credential, kind, model_id, "hello", **options)
    assert all(call[0] == "GET" for call in api.calls)


@pytest.mark.parametrize(
    "listing", [None, [], {}, {"items": {}}, {"items": [{"id": "", "title": SESSION_TITLE}]}]
)
async def test_invalid_session_listing_never_submits_generation(clients, listing):
    api, media, credential = clients
    api.sessions = listing
    with pytest.raises(UpstreamProtocolViolation):
        await media.submit(credential, "image", "image", "hello")
    assert all(call[0] == "GET" for call in api.calls)


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"session": []},
        {"session": {}, "task": task()},
        {"session": {"id": "s1"}, "task": None},
        {"session": {"id": "different"}, "task": task()},
    ],
)
async def test_invalid_submission_response_is_not_replayed(clients, response):
    api, media, credential = clients
    api.create_response = response
    with pytest.raises(UpstreamProtocolViolation):
        await media.submit(credential, "image", "image", "hello")
    assert sum(call[:2] == ("POST", f"{FILM_API}/sessions") for call in api.calls) == 1


async def test_network_failure_on_generation_is_never_retried(clients):
    api, media, credential = clients
    media._account(credential).session_id = "s1"
    api.failures[f"{FILM_API}/sessions/s1/tasks"] = httpx.ReadTimeout("uncertain submission")
    with pytest.raises(httpx.ReadTimeout):
        await media.submit(credential, "image", "image", "hello")
    assert sum(call[0] == "POST" for call in api.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("rename failed"),
        MioraHTTPError(500, b"rename failed"),
        UpstreamProtocolViolation("rename invalid"),
    ],
)
async def test_failed_optional_rename_does_not_lose_submitted_task(clients, error):
    api, media, credential = clients
    api.failures[f"{FILM_API}/sessions/s1/update"] = error
    result = await media.submit(credential, "image", "image", "hello")
    assert result["task_id"] == "t1"
    assert media._account(credential).session_id == "s1"


async def test_progress_selects_exact_task_and_verifies_session(clients):
    api, media, credential = clients
    api.progress = [None, task(id="different"), task(status="completed", slots=[completed_slot()])]
    result = await media.status(credential, "s1", "t1")
    assert result["status"] == "completed"
    assert api.calls[-1][2] == {"taskIds": ["t1"]}
    with pytest.raises(UpstreamProtocolViolation):
        await media.status(credential, "other", "t1")


@pytest.mark.parametrize(
    "progress, error",
    [
        (None, UpstreamProtocolViolation),
        ({}, UpstreamProtocolViolation),
        ([], MioraMediaRequestError),
    ],
)
async def test_progress_failure_is_explicit(clients, progress, error):
    api, media, credential = clients
    api.progress = progress
    with pytest.raises(error):
        await media.status(credential, "s1", "t1")


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "img/key",
        "file//key",
        "file/../key",
        "file/./key",
        "file/key?x",
        "file/key#x",
        "file/key\\x",
        "file/key\x00",
        "file/key\x7f",
    ],
)
async def test_invalid_file_keys_never_reach_signing_service(clients, key):
    api, media, credential = clients
    with pytest.raises(MioraMediaRequestError):
        await media.file_url(credential, key)
    assert api.calls == []


@pytest.mark.parametrize(
    "signed",
    [
        None,
        [],
        {},
        {"signedUrls": []},
        {"signedUrls": ["https://example.com/a", "https://example.com/b"]},
        {"signedUrls": f"http://{FILE_HOST}/a"},
        {"signedUrls": "https://example.com/a"},
        {"signedUrls": f"https://{FILE_HOST}:444/a"},
        {"signedUrls": f"https://u@{FILE_HOST}/a"},
        {"signedUrls": f"https://u:p@{FILE_HOST}/a"},
        {"signedUrls": f"https://{FILE_HOST}/a#x"},
        {"signedUrls": f"https://{FILE_HOST}:bad/a"},
        {"signedUrls": "https://[bad"},
    ],
)
async def test_download_url_must_be_official_https_without_userinfo_or_redirects(clients, signed):
    api, media, credential = clients
    api.signed = signed
    with pytest.raises(UpstreamProtocolViolation):
        await media.file_url(credential, "file/user/key")


@pytest.mark.parametrize("array", [True, False])
async def test_signed_url_array_and_scalar_are_supported(clients, monkeypatch, array):
    api, media, credential = clients
    url = f"https://{FILE_HOST}:443/file/user/key?sign=test"
    api.signed = {"signedUrls": [url] if array else url}
    hosts = []

    async def check(host):
        hosts.append(host)

    monkeypatch.setattr(mod, "ensure_public_host", check)
    assert await media.file_url(credential, "file/user/key") == url
    assert api.calls[-1][2] == {"filePaths": ["file/user/key"]}
    assert hosts == [FILE_HOST]


async def test_media_request_timeout_override_is_applied(clients, monkeypatch):
    _, media, credential = clients
    options = []
    original = media.client._short.request

    async def capture(*args, **kwargs):
        options.append(kwargs["timeout"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(media.client._short, "request", capture)
    await media.submit(credential, "image", "image", "hello")
    assert 120 in options and options[0] == 30
