"""聊天出口在发起推理前拒绝目录明确声明的媒体专用模型。"""

from __future__ import annotations

from dataclasses import replace

from ..compat.openai.request import ChatRequest, InvalidRequest
from ..engine.model_resolver import resolve
from ..model_alias import parse_model_aliases
from ..provider.base import Model
from .deps import ApiKeyPrincipal, Services
from .model_merge import _build_aliases, _merge_provider, _sort_key

CHAT_ENDPOINT = "/v1/chat/completions"


def force_image_request(services: Services, request: ChatRequest,
                        principal: ApiKeyPrincipal) -> tuple[ChatRequest, str]:
    """显式 image_generation 选择只进入 Miora，先拒绝渠道/开关/账号冲突。"""
    if request.raw.get("miora_image_generation_forced") is not True:
        return request, principal.provider_binding
    if principal.provider_binding not in ("", "miora"):
        raise InvalidRequest("forced image_generation requires a Miora API key binding")
    target = resolve(request.model, services.settings.default_model)
    if request.model and target.forced and target.providers != ("miora",):
        raise InvalidRequest("forced image_generation requires a Miora model; "
                             "remove other provider suffix")
    if (services.registry.get("miora") is None or services.media is None
            or not getattr(services.settings, "media_enabled", True)
            or not getattr(services.settings, "miora_auto_media_enabled", True)):
        raise InvalidRequest("forced image_generation is unavailable or disabled on this gateway")
    rows = services.credentials.candidates(("miora",), selectable_only=True)
    if not any(not principal.credential_ids or row.credential_id in principal.credential_ids
               for row in rows):
        raise InvalidRequest("no allowed Miora account is available for forced image_generation")
    # 保留模型名；明确要求媒体工具时只覆盖本次请求的默认渠道，不修改 Key 策略。
    model = f"{target.model}@miora"
    return replace(request, model=model, raw={**request.raw, "model": model}), "miora"


def _lookup(services: Services, provider: str, model: str, table: dict[str, Model]) -> Model | None:
    direct = table.get(model.lower())
    if direct is not None or not table:
        return direct
    grouped: dict = {}
    _merge_provider(grouped, provider, table)
    custom = parse_model_aliases(str(getattr(services.settings, "model_aliases", "")))
    relevant = {model.lower(): custom[model.lower()]} if model.lower() in custom else {}
    aliases = _build_aliases(sorted(grouped.values(), key=_sort_key), relevant)
    raw = aliases.get(provider, {}).get(model.lower())
    if raw is None:
        raw = services.model_aliases.get(provider, {}).get(model.lower(), model)
    return table.get(raw.lower())


def ensure_chat_endpoint(services: Services, request: ChatRequest, principal: ApiKeyPrincipal,
                         account_tables: dict[str, dict[str, Model]] | None = None) -> None:
    """只据当前允许账号的目录判断；None 和未知目录继续交给原执行路径。"""
    cache = services.model_list_cache
    if not principal.credential_ids and not cache:
        return
    target = resolve(request.model, services.settings.default_model)
    binding = principal.provider_binding
    if binding and target.forced and target.providers != (binding,):
        return
    providers = {binding} if binding else set(target.providers)
    rows = services.credentials.candidates(providers, selectable_only=True, model=target.model)
    matches: list[Model] = []
    unknown = False
    if principal.credential_ids:
        tables = account_tables or {}
        for row in rows:
            if row.credential_id not in principal.credential_ids:
                continue
            entry = _lookup(services, row.provider, target.model,
                            tables.get(row.credential_id, {}))
            if entry is not None:
                matches.append(entry)
    else:
        active = {row.provider for row in rows if row.provider in services.registry}
        if not target.forced and not binding:
            known = {provider for provider, aliases in services.model_aliases.items()
                     if target.model.lower() in aliases}
            if known:
                active &= known
        for provider in active:
            entry = _lookup(services, provider, target.model, cache.get(provider, {}))
            if entry is None:
                unknown = True
            else:
                matches.append(entry)
    if unknown or not matches or any(
        entry.gateway_endpoints is None or CHAT_ENDPOINT in entry.gateway_endpoints
        for entry in matches
    ):
        return
    endpoints = sorted({endpoint for entry in matches for endpoint in entry.gateway_endpoints})
    hint = ", ".join(endpoints) or "a supported media endpoint"
    raise InvalidRequest(
        f"model {target.model!r} does not support chat; use media endpoint: {hint}")


def chat_account_tables(tables: dict[str, dict[str, Model]]) -> dict[str, dict[str, Model]]:
    """同账号混合目录保留文本候选，fallback 不会把媒体模型发到 ACP。"""
    return {account: {lower: model for lower, model in table.items()
                      if model.gateway_endpoints is None
                      or CHAT_ENDPOINT in model.gateway_endpoints}
            for account, table in tables.items()}
