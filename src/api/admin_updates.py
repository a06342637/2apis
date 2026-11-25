"""更新系统只允许管理员，写操作沿用 Origin 与 CSRF 防护。"""

from fastapi import APIRouter, Depends

from ..auth.rbac import require_admin
from ..compat.openai.request import InvalidRequest
from ..updater.client import UpdateClient
from .deps import Services, csrf_protected, principal_from_request


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    client = UpdateClient(services.settings.update_socket_path,
                          services.settings.update_secret_file)

    async def call(action: str, payload: dict | None = None):
        try:
            return await client.call(action, payload)
        except ValueError as error:
            raise InvalidRequest(str(error)) from error

    @router.get("/api/system-updates")
    async def status(principal=Depends(principal_from_request)):
        require_admin(principal)
        return await call("status")

    @router.post("/api/system-updates/check")
    async def check(_csrf: None = Depends(csrf_protected),
                    principal=Depends(principal_from_request)):
        require_admin(principal)
        return await call("check")

    @router.put("/api/system-updates/source")
    async def source(payload: dict, _csrf: None = Depends(csrf_protected),
                     principal=Depends(principal_from_request)):
        require_admin(principal)
        result = await call("source", payload)
        services.audit.record(actor=principal.username, action="system_update.source")
        return result

    @router.post("/api/system-updates/start")
    async def start(payload: dict, _csrf: None = Depends(csrf_protected),
                    principal=Depends(principal_from_request)):
        require_admin(principal)
        result = await call("start", payload)
        services.audit.record(actor=principal.username, action="system_update.start")
        return result

    return router
