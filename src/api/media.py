"""OpenAI 生图与异步视频接口，复用 Key/用户鉴权和额度准入。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ..compat.openai.request import ChatRequest, InvalidRequest
from ..key_quota import QuotaExceeded
from ..media.request import parse_media_request
from ..media.service import MediaRateLimited
from ..media.streaming import encode_media_json
from .deps import ApiKeyPrincipal, Services, _ensure_not_paused, api_key_user, read_json_body
from .endpoints import _admit_quota, quota_exceeded_response
from .streaming import respond_with_encoded_json, respond_with_keepalive


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    def service():
        if services.media is None:
            raise InvalidRequest("media generation is disabled")
        return services.media

    async def submit(request: Request, principal: ApiKeyPrincipal, kind: str):
        if kind == "video" and request.headers.get("content-type", "").lower().startswith(
            "multipart/form-data"
        ):
            async with request.form(max_files=0, max_fields=16, max_part_size=1024 * 1024) as form:
                items = form.multi_items()
                if len(items) != len({key for key, _value in items}):
                    raise InvalidRequest("duplicate media form fields are not supported")
                if "input_reference" in form:
                    raise InvalidRequest("Miora input_reference/video editing is not supported")
                raw = dict(items)
        else:
            raw = await read_json_body(request)
        body = parse_media_request(raw, kind)
        manager = service()
        nonce = request.headers.get("Idempotency-Key")
        previous = manager.existing(principal, body, nonce)
        lease = None
        if previous is None:
            chat = ChatRequest(
                model=body["model"],
                messages=[{"role": "user", "content": body["prompt"]}],
                stream=False,
                raw={},
            )
            try:
                lease = _admit_quota(request, principal, chat)
            except QuotaExceeded as error:
                return quota_exceeded_response(error)

        async def work():
            job = previous
            if job is None:
                job = await manager.submit(principal, body, idempotency_key=nonce)
            job = await manager.wait(job, principal)
            return await manager.image_response(job, body["response_format"])

        if kind == "image":
            try:
                grace = services.settings.nonstream_keepalive_seconds
                encoder = encode_media_json if body["response_format"] == "b64_json" else None
                if grace > 0:
                    return await respond_with_keepalive(
                        work,
                        grace=grace,
                        release=lease.release if lease is not None else None,
                        encode=encoder,
                    )
                if encoder is not None:
                    try:
                        payload = await work()
                    except BaseException:
                        if lease is not None:
                            lease.release()
                        raise
                    return respond_with_encoded_json(
                        payload,
                        encode=encoder,
                        release=lease.release if lease is not None else None,
                    )
                try:
                    return JSONResponse(await work())
                finally:
                    if lease is not None:
                        lease.release()
            except TimeoutError as error:
                raise InvalidRequest(
                    "Miora media job is still processing; submission was not repeated"
                ) from error
            except MediaRateLimited as error:
                return JSONResponse(
                    status_code=429,
                    content={"error": {"type": "rate_limit_error", "message": str(error)}},
                    headers={"Retry-After": str(error.retry_after)},
                )
        try:
            job = previous
            if job is None:
                job = await manager.submit(principal, body, idempotency_key=nonce)
            return JSONResponse(manager.public_job(job))
        except MediaRateLimited as error:
            return JSONResponse(
                status_code=429,
                content={"error": {"type": "rate_limit_error", "message": str(error)}},
                headers={"Retry-After": str(error.retry_after)},
            )
        finally:
            if lease is not None:
                lease.release()

    @router.post("/v1/images/generations")
    async def images(request: Request, principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await submit(request, principal, "image")

    @router.post("/v1/videos")
    async def videos(request: Request, principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await submit(request, principal, "video")

    @router.get("/v1/videos/{job_id}")
    async def video_status(job_id: str, principal: ApiKeyPrincipal = Depends(api_key_user)):
        manager = service()
        job = manager.owned(job_id, principal)
        if job["kind"] != "video":
            raise HTTPException(404, "video not found")
        return manager.public_job(await manager.refresh(job, principal))

    async def content(job, principal, index):
        manager = service()
        job = await manager.refresh(job, principal)
        if job["status"] != "completed" or index < 0 or index >= len(job["files"]):
            raise HTTPException(409, "media content is not ready")
        asset = job["files"][index]
        return FileResponse(
            manager.store.asset(job["id"], index),
            media_type=asset["mime_type"],
            headers={
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "private, no-store",
            },
        )

    @router.get("/v1/videos/{job_id}/content")
    async def video_content(job_id: str, principal: ApiKeyPrincipal = Depends(api_key_user)):
        manager = service()
        job = manager.owned(job_id, principal)
        if job["kind"] != "video":
            raise HTTPException(404, "video not found")
        return await content(job, principal, 0)

    @router.get("/v1/media/{job_id}/content")
    async def signed_content(
        request: Request, job_id: str, expires: int, signature: str, index: int = 0
    ):
        _ensure_not_paused(services)
        manager = service()
        job = manager.store.load(job_id)
        manager.store.verify_signature(job, index, expires, signature)
        principal = ApiKeyPrincipal(username=job["username"], key_id=job["key_id"])
        principal = manager.fresh_principal(principal)
        manager.owned(job_id, principal)
        return await content(job, principal, index)

    return router
