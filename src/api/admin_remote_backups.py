from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from ..auth.rbac import require_admin
from ..compat.openai.request import InvalidRequest
from ..remote_backup.config import BackupError
from ..remote_backup.service import RemoteBackups
from .deps import csrf_protected, principal_from_request


def create_router(backups: RemoteBackups) -> APIRouter:
    router = APIRouter(prefix="/api/remote-backups")

    async def call(function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except ValidationError as error:
            errors = error.errors(include_input=False, include_context=False, include_url=False)
            details = "；".join(f"{'.'.join(map(str, row['loc']))}: {row['msg']}" for row in errors)
            raise InvalidRequest(details) from None
        except BackupError as error:
            raise InvalidRequest(str(error)) from None
        except (OSError, ValueError):
            raise InvalidRequest("远端备份配置或状态无法读取，请检查数据目录") from None

    @router.get("")
    async def status(response: Response, principal=Depends(principal_from_request)):
        require_admin(principal)
        response.headers["Cache-Control"] = "no-store"
        return await call(backups.status)

    @router.put("")
    async def save(payload: dict, _csrf=Depends(csrf_protected),
                   principal=Depends(principal_from_request)):
        require_admin(principal)
        await call(backups.save, payload)
        return await call(backups.status)

    @router.post("/run", status_code=202)
    async def run(_csrf=Depends(csrf_protected), principal=Depends(principal_from_request)):
        require_admin(principal)
        try:
            backups.start()
        except BackupError as error:
            raise InvalidRequest(str(error)) from None
        return {"accepted": True}

    @router.get("/{target}/files")
    async def files(target: str, principal=Depends(principal_from_request)):
        require_admin(principal)
        return await call(backups.operate, target, "list")

    @router.post("/{target}/test")
    async def test(target: str, _csrf=Depends(csrf_protected),
                   principal=Depends(principal_from_request)):
        require_admin(principal)
        return await call(backups.operate, target, "test")

    @router.get("/{target}/files/{name}")
    async def download(target: str, name: str, principal=Depends(principal_from_request)):
        require_admin(principal)
        await call(backups.namespace)
        directory = TemporaryDirectory(dir=backups.store.directory)
        path = Path(directory.name) / "snapshot.sqlite3"
        try:
            await call(backups.download, target, name, path)
        except BaseException:
            directory.cleanup()
            raise
        return FileResponse(path, media_type="application/x-sqlite3", filename=name,
                            headers={"Cache-Control": "no-store"},
                            background=BackgroundTask(directory.cleanup))

    @router.post("/{target}/files/{name}/restore")
    async def restore(target: str, name: str, _csrf=Depends(csrf_protected),
                      principal=Depends(principal_from_request)):
        require_admin(principal)
        return await call(backups.operate, target, "restore", name)

    @router.delete("/{target}/files/{name}")
    async def delete(target: str, name: str, _csrf=Depends(csrf_protected),
                     principal=Depends(principal_from_request)):
        require_admin(principal)
        return await call(backups.operate, target, "delete", name)

    return router
