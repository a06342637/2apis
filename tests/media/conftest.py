from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.api.deps import ApiKeyPrincipal
from src.auth.access import model_allowed
from src.engine.inflight import InflightGuard
from src.engine.scheduler import Candidate
from src.media.service import MediaService
from src.media.storage import MediaStore
from src.provider.base import Model
from src.request_context import get_credential_id


@pytest.fixture
def media_env(tmp_path):
    state = SimpleNamespace(
        key={
            "id": "key",
            "username": "root",
            "credential_ids": ["a"],
            "provider_binding": "miora",
            "allowed_models": "",
            "quota": "",
        },
        policy={"allowed_models": "", "quota": ""},
        active=True,
        key_present=True,
        rows=[
            Candidate("b", "miora", health=90, quota_remaining=50),
            Candidate("a", "miora", health=100, quota_remaining=60),
        ],
        account_models={},
        data={"a": {"id": "a"}, "b": {"id": "b"}},
        models=[
            Model(
                "raw-image",
                name="Pretty Picture",
                gateway_endpoints=("/v1/images/generations",),
                output_modalities=("image",),
            ),
            Model(
                "raw-video",
                name="Pretty Video",
                gateway_endpoints=("/v1/videos",),
                output_modalities=("video",),
            ),
        ],
        blocked=False,
        calls=[],
        records=[],
        errors=[],
        submit_error=None,
        failed=False,
        on_force=None,
        statuses=["completed"],
        blobs=[b"image"],
        stats_error=False,
    )
    settings = SimpleNamespace(
        data_dir=str(tmp_path),
        app_secret="x" * 32,
        model_blocklist="",
        model_aliases="",
        media_enabled=True,
        miora_auto_media_enabled=True,
        media_signed_url_seconds=3600,
        media_max_bytes=100,
        media_timeout_seconds=1,
        media_poll_seconds=0.001,
        public_base_url="https://api.example",
    )

    def candidates(providers=None, *, selectable_only=False, model=None):
        return [
            row
            for row in state.rows
            if (providers is None or row.provider in providers)
            and (not selectable_only or row.enabled and not row.disabled)
            and model_allowed(model, state.account_models.get(row.credential_id, ""))
        ]

    def credential_from(data):
        return SimpleNamespace(id=data["id"], identity=lambda: data["id"])

    class Client:
        async def catalog(self, credential, *, force=False):
            state.calls.append(("catalog", get_credential_id(), force))
            if force and state.on_force:
                state.on_force()
            return list(state.models)

        async def submit(self, credential, kind, model, prompt, **options):
            state.calls.append(("submit", credential.id, kind, model, prompt, options))
            if state.submit_error is not None:
                raise state.submit_error
            return {
                "session_id": "session",
                "task_id": "task",
                "kind": kind,
                "model": model,
                "status": "failed" if state.failed else "in_progress",
                "progress": 0,
                "files": [],
                "error": "failure" if state.failed else "",
            }

        async def status(self, credential, session, task):
            state.calls.append(("status", credential.id, session, task))
            status = state.statuses.pop(0) if len(state.statuses) > 1 else state.statuses[0]
            return {
                "status": status,
                "progress": 100 if status == "completed" else 0,
                "files": [{"file_key": str(index)} for index in range(len(state.blobs))]
                if status == "completed"
                else [],
                "error": "",
            }

        async def file_url(self, credential, key):
            state.calls.append(("file", credential.id, key))
            return "https://sign.miorausercontent.com/" + key

    class Downloader:
        async def download(self, url, target, kind, *, max_bytes=None):
            blob = state.blobs[int(url.rsplit("/", 1)[1])]
            if len(blob) > max_bytes:
                raise ValueError("aggregate budget")
            target.write_bytes(blob)
            state.calls.append(("download", max_bytes))
            return {
                "mime_type": "image/jpeg" if kind == "image" else "video/mp4",
                "bytes": len(blob),
            }

    def record(**kwargs):
        state.records.append(kwargs)
        if state.stats_error:
            raise RuntimeError("database failed")

    client = Client()
    inflight = InflightGuard(lambda: 1)
    services = SimpleNamespace(
        settings=settings,
        registry={
            "miora": SimpleNamespace(
                media_client=client, client=None, pacer=None, credential_from=credential_from
            )
        },
        api_keys=SimpleNamespace(
            list_for=lambda user: [state.key] if state.key_present and user == "root" else []
        ),
        user_repo=SimpleNamespace(get=lambda _user: state.policy),
        users=SimpleNamespace(is_active=lambda _user: state.active),
        credentials=SimpleNamespace(
            candidates=candidates,
            credential_data=lambda account: state.data.get(account),
            save_error=lambda account, outcome: state.errors.append((account, outcome)),
        ),
        limit_guard=SimpleNamespace(blocked=lambda *_: state.blocked),
        inflight=inflight,
        key_quota=SimpleNamespace(_check_daily=lambda *_: state.calls.append(("key_quota",))),
        user_quota=SimpleNamespace(_check_daily=lambda *_: state.calls.append(("user_quota",))),
    )
    store = MediaStore(tmp_path / "media", settings.app_secret)
    manager = MediaService(
        services, stats=SimpleNamespace(record=record), store=store, downloader=Downloader()
    )
    principal = ApiKeyPrincipal(
        "root", key_id="key", provider_binding="miora", credential_ids=("a",)
    )
    return SimpleNamespace(
        state=state,
        services=services,
        manager=manager,
        principal=principal,
        client=client,
        settings=settings,
        replace=replace,
    )
