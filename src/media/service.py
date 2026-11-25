"""选中账号的媒体生成，不重放提交；后续恢复只读取同账号任务状态。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from ..api.deps import ApiKeyPrincipal
from ..api.model_merge import _blocked, _build_aliases, _merge_provider
from ..auth.access import model_allowed
from ..auth.api_key import key_expired
from ..auth.group_binding import resolve_credential_scope
from ..auth.rbac import UnauthorizedError
from ..compat.openai.request import InvalidRequest
from ..engine.scheduler import Scheduler
from ..key_quota import load_key_quota
from ..model_alias import parse_model_aliases
from ..provider.base import ErrKind, UpstreamHTTPError, UpstreamProtocolViolation
from ..provider.naming import normalize_model_key
from ..request_context import credential_scope
from ..tasks.pacer import paced
from .download import MediaDownloader
from .storage import MediaStore
from .streaming import MediaJSONStream

logger = logging.getLogger(__name__)
ENDPOINTS = {"image": "/v1/images/generations", "video": "/v1/videos"}


class MediaRateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("Miora media rate limited; generation was not retried")


class MediaService:
    def __init__(self, services, stats=None, client=None, store=None, downloader=None):
        self.services = services
        self.settings = services.settings
        self.provider = services.registry["miora"]
        self.client = client or self.provider.media_client
        self.stats = stats
        self.store = store or MediaStore(
            Path(self.settings.data_dir) / "media",
            self.settings.app_secret,
            ttl=getattr(self.settings, "media_signed_url_seconds", 3600),
        )
        self.downloader = downloader or MediaDownloader(
            ("sign.miorausercontent.com",),
            max_bytes=getattr(self.settings, "media_max_bytes", 64 * 1024 * 1024),
        )
        self.scheduler = Scheduler()
        self.locks: dict[str, dict] = {}
        self.slots = asyncio.Semaphore(128)
        self.io_slots = asyncio.Semaphore(getattr(self.settings, "media_io_concurrency", 2))

    @asynccontextmanager
    async def job_lock(self, job_id: str):
        # 包含等待者的引用计数，不能清除仍有等待者的锁；上限仅覆盖活跃请求。
        async with self.slots:
            entry = self.locks.setdefault(job_id, {"lock": asyncio.Lock(), "users": 0})
            entry["users"] += 1
            try:
                async with entry["lock"]:
                    yield
            finally:
                entry["users"] -= 1
                if not entry["users"]:
                    self.locks.pop(job_id)

    def fresh_principal(self, principal: ApiKeyPrincipal) -> ApiKeyPrincipal:
        row = next(
            (
                row
                for row in self.services.api_keys.list_for(principal.username)
                if row["id"] == principal.key_id
            ),
            None,
        )
        if (
            row is None
            or key_expired(row.get("expires_at"))
            or not self.services.users.is_active(principal.username)
        ):
            raise UnauthorizedError("media API key missing, expired or disabled")
        policy = self.services.user_repo.get(principal.username) or {}
        return replace(
            principal,
            provider_binding=row.get("provider_binding") or "",
            allowed_models=row.get("allowed_models") or "",
            user_allowed_models=str(policy.get("allowed_models") or ""),
            credential_ids=resolve_credential_scope(
                row, getattr(self.services, "credential_groups", None)),
            group_ids=tuple(row.get("group_ids") or ()),
            quota=load_key_quota(row.get("quota")),
            user_quota=load_key_quota(policy.get("quota")),
        )

    def _candidate(
        self,
        principal: ApiKeyPrincipal,
        account_id: str,
        model: str | None = None,
        *,
        generating: bool = True,
    ):
        fresh = self.fresh_principal(principal)
        for policy in (principal, fresh):
            if policy.provider_binding not in ("", "miora") or (
                policy.credential_ids and account_id not in policy.credential_ids
            ):
                raise InvalidRequest("Miora media account is not allowed for this API key")
            if model and any(
                not model_allowed(model, spec)
                for spec in (policy.allowed_models, policy.user_allowed_models)
            ):
                raise InvalidRequest("media model is not allowed for this key or user")
        candidate = next(
            (
                row
                for row in self.services.credentials.candidates(
                    providers=("miora",), selectable_only=generating, model=model
                )
                if row.credential_id == account_id
            ),
            None,
        )
        if (
            candidate is None
            or generating
            and (
                not candidate.is_selectable(int(time.time()), model)
                or (candidate.health is not None and candidate.health <= 0)
                or (candidate.quota_remaining is not None and candidate.quota_remaining <= 0)
            )
        ):
            raise InvalidRequest("selected Miora media account is not available")
        if generating and model and self.services.limit_guard.blocked(candidate, model):
            raise InvalidRequest("Miora account media quota or model limit reached")
        return candidate

    async def describe(
        self,
        principal: ApiKeyPrincipal,
        account_id: str,
        kind: str | None = None,
        *,
        force: bool = False,
    ):
        self._candidate(principal, account_id)
        data = self.services.credentials.credential_data(account_id)
        if data is None:
            raise InvalidRequest("Miora credential no longer exists")
        with credential_scope(account_id):
            entries = await self.client.catalog(self.provider.credential_from(data), force=force)
        patterns = tuple(
            piece.strip() for piece in self.settings.model_blocklist.split(",") if piece.strip()
        )
        entries = [entry for entry in entries if not _blocked(entry, patterns)]
        grouped = {}
        _merge_provider(grouped, "miora", {entry.id.lower(): entry for entry in entries})
        merged = list(grouped.values())
        aliases = _build_aliases(
            merged, parse_model_aliases(getattr(self.settings, "model_aliases", ""))
        ).get("miora", {})
        canonical = {}
        for row in merged:
            canonical.setdefault(row["raw_ids"]["miora"], row["id"])
        visible = []
        for entry in entries:
            if kind and ENDPOINTS[kind] not in entry.gateway_endpoints:
                continue
            try:
                self._candidate(principal, account_id, canonical[entry.id])
            except InvalidRequest:
                continue
            visible.append(entry)
        return visible, aliases, canonical

    async def models(self, principal: ApiKeyPrincipal, account_id: str, kind: str | None = None):
        return (await self.describe(principal, account_id, kind))[0]

    @staticmethod
    def resolve(entries, aliases, requested: str):
        keys = (requested.lower(), normalize_model_key(requested), requested.lower() + "@miora")
        targets = {aliases.get(key) for key in keys}
        matches = [
            entry
            for entry in entries
            if entry.id in targets or entry.id.lower() == requested.lower()
        ]
        return matches[0] if len(matches) == 1 else None

    async def choose(self, principal: ApiKeyPrincipal, request: dict) -> str:
        candidates = []
        raw = request["model"]
        model, _, suffix = raw.rpartition("@") if "@" in raw else (raw, "", "")
        if suffix not in ("", "miora"):
            raise InvalidRequest("media endpoints currently support Miora models only")
        request["model"] = model
        for candidate in self.services.credentials.candidates(
            providers=("miora",), selectable_only=True
        ):
            account_id = candidate.credential_id
            try:
                entries, aliases, canonical = await self.describe(
                    principal, account_id, request["kind"]
                )
            except InvalidRequest:
                continue
            entry = self.resolve(entries, aliases, model)
            if entry is None:
                continue
            inflight = self.services.inflight
            if inflight is not None and inflight.blocked(account_id):
                continue
            candidates.append(
                replace(
                    candidate,
                    credit_rate=entry.credit_rate,
                    inflight=inflight.count(account_id) if inflight is not None else 0,
                )
            )
            request.setdefault("account_models", {})[account_id] = (entry.id, canonical[entry.id])
        selected = self.scheduler.select(
            candidates, set(), int(time.time()), round_robin=bool(principal.credential_ids),
            round_robin_key=principal.key_id,
        )
        if selected is None:
            raise InvalidRequest("no selected Miora account offers this allowed media model")
        request["model"], request["public_model"] = request.pop("account_models")[selected]
        return selected

    def existing(self, principal: ApiKeyPrincipal, request: dict, nonce: str | None):
        if nonce is None:
            return None
        job_id = self.store.intent_id(principal.key_id, nonce)
        if not self.store.path(job_id).is_file():
            return None
        job = self.owned(job_id, principal)
        if job["request_digest"] != self.request_digest(request):
            raise HTTPException(409, "Idempotency-Key was already used with another request")
        return job

    @staticmethod
    def request_digest(request: dict) -> str:
        return hashlib.sha256(
            json.dumps(request, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def record(self, principal: ApiKeyPrincipal, job: dict, *, ok: bool):
        if self.stats is not None:
            try:
                self.stats.record(
                    username=principal.username,
                    provider="miora",
                    model=job["public_model"],
                    credential_id=job["credential_id"],
                    ok=ok,
                    error_type=None if ok else "media_generation_failed",
                )
            except Exception:
                logger.warning("Miora media usage write failed for job %s", job["id"])

    def rate_limited(self, candidate, error: UpstreamHTTPError) -> MediaRateLimited:
        now = int(time.time())
        outcome = self.scheduler.note_error(candidate, ErrKind.SOFT, now, retry_at=error.retry_at)
        self.services.credentials.save_error(candidate.credential_id, outcome)
        wait = max(
            1, (outcome.cooling_until or now + 60) - now, math.ceil(error.retry_after_seconds or 0)
        )
        return MediaRateLimited(wait)

    def save_result(self, job: dict, result: dict) -> dict:
        # 任务身份与授权模型由我方意图记录固定，上游不能覆盖 owner/key/credential/id。
        fields = ("session_id", "task_id", "status", "progress", "files", "error")
        return self.store.save({**job, **{key: result[key] for key in fields if key in result}})

    async def submit(
        self,
        principal: ApiKeyPrincipal,
        request: dict,
        *,
        account_id=None,
        already_paced: bool = False,
        idempotency_key: str | None = None,
    ) -> dict:
        if not getattr(self.settings, "media_enabled", True):
            raise InvalidRequest("media generation is disabled")
        request = dict(request)
        digest = self.request_digest(request)
        job_id = (
            self.store.intent_id(principal.key_id, idempotency_key)
            if idempotency_key is not None
            else "media_" + uuid.uuid4().hex
        )
        async with self.job_lock(job_id):
            previous = self.existing(principal, request, idempotency_key)
            if previous is not None:
                return previous
            if account_id is None:
                account_id = await self.choose(principal, request)
            else:
                entries, aliases, canonical = await self.describe(
                    principal, account_id, request["kind"]
                )
                entry = self.resolve(entries, aliases, request["model"])
                if entry is None:
                    raise InvalidRequest("selected account does not offer this allowed media model")
                request["model"], request["public_model"] = entry.id, canonical[entry.id]
            self._candidate(principal, account_id, request["public_model"])
            job = self.store.create(
                job_id=job_id,
                username=principal.username,
                key_id=principal.key_id,
                credential_id=account_id,
                model=request["model"],
                public_model=request["public_model"],
                kind=request["kind"],
                prompt=request["prompt"],
                options=request["options"],
                session_id="",
                task_id="",
                request_digest=digest,
            )

            async def generate():
                candidate = self._candidate(principal, account_id, request["public_model"])
                available, _aliases, _canonical = await self.describe(
                    principal, account_id, request["kind"], force=True
                )
                if not any(entry.id == request["model"] for entry in available):
                    raise InvalidRequest(
                        "media model is no longer available for this selected account"
                    )
                data = self.services.credentials.credential_data(account_id)
                if data is None:
                    raise InvalidRequest("Miora credential no longer exists")
                credential = self.provider.credential_from(data)
                inflight = None if already_paced else self.services.inflight
                if inflight is not None:
                    if inflight.blocked(account_id):
                        raise InvalidRequest("Miora account concurrency limit reached")
                    inflight.acquire(account_id)
                try:
                    try:
                        result = await self.client.submit(
                            credential,
                            request["kind"],
                            request["model"],
                            request["prompt"],
                            **request["options"],
                        )
                    except UpstreamHTTPError as error:
                        self.record(principal, job, ok=False)
                        self.store.save(
                            {**job, "status": "failed", "error": "Miora media request rejected"}
                        )
                        if error.status == 429:
                            raise self.rate_limited(candidate, error) from error
                        return self.store.load(job_id)
                    except Exception:
                        self.record(principal, job, ok=False)
                        return self.store.save(
                            {
                                **job,
                                "status": "failed",
                                "error": (
                                    "media submission result is unknown; submission was not retried"
                                ),
                            }
                        )
                    accepted = self.save_result(job, result)
                    self.record(principal, accepted, ok=accepted["status"] != "failed")
                    return accepted
                finally:
                    if inflight is not None:
                        inflight.release(account_id)

            with credential_scope(account_id):
                if already_paced:
                    fresh = self.fresh_principal(principal)
                    moment = time.time()
                    self.services.key_quota._check_daily(fresh.key_id, fresh.quota, moment, "key")
                    self.services.user_quota._check_daily(
                        fresh.username, fresh.user_quota, moment, "user"
                    )
                    return await generate()
                data = self.services.credentials.credential_data(account_id)
                if data is None:
                    raise InvalidRequest("Miora credential no longer exists")
                credential = self.provider.credential_from(data)
                async with paced(self.provider.pacer, credential.identity()):
                    return await generate()

    def owned(self, job_id: str, principal: ApiKeyPrincipal) -> dict:
        job = self.store.load(job_id)
        if job["key_id"] != principal.key_id or job["username"] != principal.username:
            raise HTTPException(404, "media job not found")
        self._candidate(principal, job["credential_id"], job["public_model"], generating=False)
        return job

    async def refresh(self, job: dict, principal: ApiKeyPrincipal) -> dict:
        async with self.job_lock(job["id"]):
            job = self.owned(job["id"], principal)
            if job["status"] == "submitting":
                raise InvalidRequest("media submission result is unknown; it will not be replayed")
            if job["status"] not in {"completed", "failed"}:
                data = self.services.credentials.credential_data(job["credential_id"])
                if data is None:
                    raise InvalidRequest("Miora credential no longer exists")
                with credential_scope(job["credential_id"]):
                    result = await self.client.status(
                        self.provider.credential_from(data), job["session_id"], job["task_id"]
                    )
                job = self.save_result(job, result)
            if job["status"] == "completed":
                if not job["files"]:
                    raise UpstreamProtocolViolation("Miora 完成的媒体任务没有资源")
                total = 0
                maximum = getattr(self.settings, "media_max_bytes", 64 * 1024 * 1024)
                for index, asset in enumerate(job["files"]):
                    target = self.store.asset(job["id"], index)
                    if target.is_file() and not target.is_symlink() and asset.get("bytes"):
                        total += asset["bytes"]
                        if total > maximum:
                            raise InvalidRequest("media response exceeds aggregate size limit")
                        continue
                    data = self.services.credentials.credential_data(job["credential_id"])
                    if data is None:
                        raise InvalidRequest("Miora credential no longer exists")
                    with credential_scope(job["credential_id"]):
                        url = await self.client.file_url(
                            self.provider.credential_from(data), asset["file_key"]
                        )
                    await self.io_slots.acquire()
                    try:
                        info = await self.downloader.download(
                            url, target, job["kind"], max_bytes=maximum - total
                        )
                    finally:
                        self.io_slots.release()
                    total += info["bytes"]
                    job["files"][index] = {**asset, **info}
                job = self.store.save(job)
            return job

    async def wait(self, job: dict, principal: ApiKeyPrincipal) -> dict:
        async with asyncio.timeout(getattr(self.settings, "media_timeout_seconds", 300)):
            while True:
                job = await self.refresh(job, principal)
                if job["status"] == "failed":
                    raise InvalidRequest("Miora media job failed")
                if job["status"] == "completed":
                    return job
                await asyncio.sleep(getattr(self.settings, "media_poll_seconds", 2))

    def public_job(self, job: dict) -> dict:
        return {
            "id": job["id"],
            "object": "video" if job["kind"] == "video" else "image",
            "model": job["public_model"],
            "status": job["status"],
            "progress": job["progress"],
            "created_at": job["created_at"],
            "error": {"code": "media_generation_failed", "message": job["error"]}
            if job.get("error")
            else None,
            "seconds": str(job["options"]["duration"]) if "duration" in job["options"] else None,
        }

    async def image_response(self, job: dict, response_format: str):
        if sum(asset["bytes"] for asset in job["files"]) > getattr(
            self.settings, "media_max_bytes", 64 * 1024 * 1024
        ):
            raise InvalidRequest("media response exceeds aggregate size limit")
        if response_format == "b64_json":
            return MediaJSONStream(
                created=job["created_at"],
                paths=[
                    self.store.asset(job["id"], index) for index, _asset in enumerate(job["files"])
                ],
                maximum=getattr(self.settings, "media_max_bytes", 64 * 1024 * 1024),
                slots=self.io_slots,
                keepalive=getattr(self.settings, "nonstream_keepalive_seconds", 0),
            )
        data = []
        for index, _asset in enumerate(job["files"]):
            item = {"url": self.store.signed_url(job, self.settings.public_base_url, index)}
            data.append(item)
        return {"created": job["created_at"], "data": data}
