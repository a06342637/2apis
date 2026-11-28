"""明确属于 Miora 的请求在账号排队与响应保活之前执行纯校验。"""

from __future__ import annotations

from ..compat.openai.request import ChatRequest, InvalidRequest
from ..engine.model_resolver import (
    UnknownModelError,
    ordered_fallback_chain,
    parse_fallback_groups,
    resolve,
)
from ..media.auto import builtin_options
from ..provider.base import Model
from ..provider.miora.client import validate_payload
from ..provider.miora.tools import prepare_prompt
from ..provider.model_policy import MIORA_RETIRED_MESSAGE, model_retired
from .chat_media_gate import _lookup
from .deps import ApiKeyPrincipal, Services


def _known_table(value) -> bool:
    return isinstance(value, dict) and bool(value) and all(
        isinstance(key, str) and isinstance(model, Model) for key, model in value.items()
    )


def _scoped_rows(services: Services, principal: ApiKeyPrincipal, tables):
    rows = services.credentials.candidates(selectable_only=True)
    if principal.credential_ids:
        return [row for row in rows if row.credential_id in principal.credential_ids]
    return [row for row in rows if row.credential_id in tables]


def miora_account_scope(services: Services, request: ChatRequest, binding: str) -> tuple[str, ...]:
    """明确走 Miora 的全池请求也逐账号校验目录，不能用池的权限并集选号。"""
    target = resolve(request.model, services.settings.default_model)
    if binding and binding != "miora":
        return ()
    if target.forced and target.providers != ("miora",):
        return ()
    providers = {provider for provider, aliases in services.model_aliases.items()
                 if target.model.lower() in aliases}
    if not providers:
        providers = {row.provider for row in services.credentials.candidates(selectable_only=True)}
    if binding != "miora" and not target.forced and providers != {"miora"}:
        return ()
    return tuple(row.credential_id for row in services.credentials.candidates(
        ["miora"], selectable_only=True))


def reject_retired_miora(services: Services, request: ChatRequest, binding: str) -> None:
    """Fail closed for a retired model on explicitly selected Miora routes."""
    try:
        target = resolve(request.model, services.settings.default_model)
    except UnknownModelError:
        return  # Local token estimates did not require a resolvable upstream.
    provider = target.providers[0] if target.forced else binding
    if provider == "miora":
        _reject_retired_miora_name(services, target.model)


def _reject_retired_miora_name(services: Services, model: str) -> None:
    raw_model = services.model_aliases.get("miora", {}).get(model.lower(), model)
    if model_retired("miora", raw_model):
        raise InvalidRequest(MIORA_RETIRED_MESSAGE)


async def preflight_miora(
    services: Services,
    request: ChatRequest,
    principal: ApiKeyPrincipal,
    *,
    account_tables: dict[str, dict[str, Model]] | None = None,
    provider_binding: str | None = None,
    validate_only: bool = False,
) -> None:
    """只读取已加载的可信目录；空表、缺表和自动模型保留原执行路径。"""
    target = resolve(request.model, services.settings.default_model)
    binding = principal.provider_binding if provider_binding is None else provider_binding
    if binding and binding != "miora":
        return
    if target.forced and target.providers != ("miora",):
        return
    rows = None
    if binding != "miora" and not target.forced:
        if account_tables is None:
            return
        rows = _scoped_rows(services, principal, account_tables)
        if not rows or {row.provider for row in rows} != {"miora"}:
            return
    _reject_retired_miora_name(services, target.model)
    payload = {**request.raw, "messages": request.messages}
    try:
        validate_payload(payload)
        prepare_prompt(payload)
        marker = payload.get("miora_image_generation")
        if marker is not None:
            builtin_options(marker)
    except ValueError as error:
        raise InvalidRequest(str(error)) from error
    if validate_only:
        return
    if (request.model is None or not request.model.strip()
            or request.model.strip().lower() == "auto"):
        return
    groups = parse_fallback_groups(str(getattr(services.settings, "model_fallback_groups", "")))
    if ordered_fallback_chain(target.model, groups):
        return
    if account_tables is not None or principal.credential_ids:
        if rows is None:
            rows = _scoped_rows(services, principal, account_tables)
        selected = [row for row in rows if row.provider == "miora"]
        tables = account_tables or {}
        if not selected or any(not _known_table(tables.get(row.credential_id)) for row in selected):
            return
        if any(_lookup(services, "miora", target.model, tables[row.credential_id]) is not None
               for row in selected):
            return
    else:
        table = services.model_list_cache.get("miora")
        if not _known_table(table) or _lookup(services, "miora", target.model, table) is not None:
            return
    raise InvalidRequest(f"Miora does not offer model {target.model!r}")
