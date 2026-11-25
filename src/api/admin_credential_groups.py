"""账号分组管理：登录用户只读元数据，operator/admin才可管理成员。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..audit.actions import ACTION_GROUP_CREATE, ACTION_GROUP_DELETE, ACTION_GROUP_UPDATE
from ..auth.account_binding import normalize_credential_ids
from ..auth.rbac import require_operator
from ..compat.openai.request import InvalidRequest
from ..db.groups import GroupConflict, group_name
from .deps import Services, csrf_protected, principal_from_request, request_ip


def _visible(group: dict, operator: bool) -> dict:
    return {key: value for key, value in group.items() if operator or key != "credential_ids"}


def _parse(payload: dict, *, creating: bool) -> dict:
    allowed = {"name", "credential_ids", "expected_updated_at"}
    if creating:
        allowed = {"name", "credential_ids", "provider"}
    if set(payload) - allowed:
        raise ValueError("unsupported credential group field")
    result = {}
    if creating or "name" in payload:
        result["name"] = group_name(payload.get("name"))
    if creating or "credential_ids" in payload:
        result["credential_ids"] = normalize_credential_ids(payload.get("credential_ids", []))
    if "expected_updated_at" in payload:
        expected = payload["expected_updated_at"]
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("expected_updated_at must be an integer timestamp")
        result["expected_updated_at"] = expected
    return result


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    groups = services.credential_groups

    def audit(request: Request, principal, action: str, row: dict) -> None:
        services.audit.record(actor=principal.username, action=action, target=row["id"],
            detail=f"渠道 {row['provider']}；名称 {row['name']}；成员 {row['member_count']}",
            ip=request_ip(request, services.settings))

    @router.get("/api/credential-groups")
    async def list_groups(provider: str = "", principal=Depends(principal_from_request)):
        rows = [_visible(row, principal.is_operator) for row in groups.list_all(provider)]
        return {"groups": rows,
                "is_operator": principal.is_operator}

    @router.get("/api/credential-groups/{group_id}")
    async def get_group(group_id: str, principal=Depends(principal_from_request)):
        row = groups.get(group_id)
        if row is None:
            raise InvalidRequest("credential group not found")
        return _visible(row, principal.is_operator)

    @router.post("/api/credential-groups")
    async def create_group(payload: dict, request: Request,
                           _csrf: None = Depends(csrf_protected),
                           principal=Depends(principal_from_request)):
        require_operator(principal)
        try:
            values = _parse(payload, creating=True)
            provider = payload.get("provider")
            if not isinstance(provider, str) or provider not in services.registry:
                raise ValueError("group provider must be a registered provider ID")
            row = groups.create(provider, **values)
        except ValueError as error:
            raise InvalidRequest(str(error)) from error
        audit(request, principal, ACTION_GROUP_CREATE, row)
        return row

    @router.put("/api/credential-groups/{group_id}")
    async def update_group(group_id: str, payload: dict, request: Request,
                           _csrf: None = Depends(csrf_protected),
                           principal=Depends(principal_from_request)):
        require_operator(principal)
        try:
            before = groups.get(group_id)
            row = groups.update(group_id, **_parse(payload, creating=False))
        except GroupConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise InvalidRequest(str(error)) from error
        if row != before:
            audit(request, principal, ACTION_GROUP_UPDATE, row)
        return row

    @router.delete("/api/credential-groups/{group_id}")
    async def delete_group(group_id: str, request: Request,
                           _csrf: None = Depends(csrf_protected),
                           principal=Depends(principal_from_request)):
        require_operator(principal)
        row = groups.get(group_id)
        if row is None or not groups.delete(group_id):
            raise InvalidRequest("credential group not found")
        audit(request, principal, ACTION_GROUP_DELETE, row)
        return {"ok": True}

    return router
