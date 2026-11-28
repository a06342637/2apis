"""绑定账号的目录缓存与模型 union；不使用其它账号的目录兜底。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import replace
from typing import Any

from ..auth.access import model_allowed
from ..db.crypto import CredentialDecryptError
from ..engine.scheduler import Candidate
from ..model_alias import parse_model_aliases
from ..provider.base import Model
from ..provider.miora.subscription import subscription_signature
from ..provider.naming import normalize_model_key
from ..request_context import credential_scope
from .deps import Services
from .model_merge import _build_aliases, _merge_provider, _model_label, _sort_key, _visible

logger = logging.getLogger(__name__)
ACCOUNT_MODEL_TTL_SECONDS = 300
MAX_ACCOUNT_MODEL_CACHES = 128
MIORA_ACCOUNT_CATALOG_SECONDS = 8.0


def _forget(services: Services, account_id: str) -> None:
    services.credential_model_cache.pop(account_id, None)
    services.credential_model_versions.pop(account_id, None)
    services.credential_model_display_cache.pop(account_id, None)


def _remember(services: Services, account_id: str, entry: tuple[float, dict[str, Model]],
               version: str, display: dict[str, Model]) -> None:
    """按最近访问保留热目录，淘汰仅丢缓存，不改变 Key 的绑定集合。"""
    services.credential_model_cache.pop(account_id, None)
    services.credential_model_cache[account_id] = entry
    services.credential_model_versions[account_id] = version
    services.credential_model_display_cache[account_id] = display
    while len(services.credential_model_cache) > MAX_ACCOUNT_MODEL_CACHES:
        _forget(services, next(iter(services.credential_model_cache)))


async def account_model_tables(
    services: Services, credential_ids: tuple[str, ...], provider_binding: str = "", *,
    include_unavailable: bool = False, force: bool = False,
) -> dict[str, dict[str, Model]]:
    """只拉选中且启用的账号；同账号并发拉取共用编辑锁和 5 分钟缓存。"""
    selected = set(credential_ids)
    current = services.credentials.candidates()
    live = {candidate.credential_id for candidate in current}
    remembered = set(services.credential_model_cache) | set(services.credential_model_versions)
    for removed in remembered - live:
        _forget(services, removed)
    # 当前请求保留已有128份引用，批量超过缓存上限时不让逐个淘汰引起全量重拉。
    cached = dict(services.credential_model_cache)
    versions = dict(services.credential_model_versions)
    displays = dict(services.credential_model_display_cache)
    candidates = [candidate for candidate in current
                  if candidate.credential_id in selected
                  and candidate.enabled and not candidate.disabled
                  and (not provider_binding or candidate.provider == provider_binding)
                  and candidate.provider in services.registry]
    async def load(candidate: Candidate) -> dict[str, Model] | None:
        account_id = candidate.credential_id
        async with services.credentials.edit_lock(account_id):
            previous = services.credential_model_cache.get(account_id, cached.get(account_id))
            previous_version = services.credential_model_versions.get(
                account_id, versions.get(account_id))
            now = time.monotonic()
            try:
                data = services.credentials.credential_data(account_id)
                if data is None:
                    _forget(services, account_id)
                    return None
                version_data = data
                if candidate.provider == "miora":
                    version_data = {"credential": data, "subscription": subscription_signature(
                        services.credentials.quota_detail(account_id), now=int(time.time()))}
                version = hashlib.sha256(json.dumps(version_data, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()
            except Exception as error:  # noqa: BLE001 - 解密/读票失败不得使用旧账号目录
                reason = ("凭证解密失败" if isinstance(error, CredentialDecryptError)
                          else type(error).__name__)
                logger.warning("绑定账号目录凭证读取失败 %s: %s", account_id, reason)
                _forget(services, account_id)
                return {}
            same = previous_version == version
            previous_display = services.credential_model_display_cache.get(
                account_id, displays.get(account_id, previous[1] if previous else {}))
            if (not force and previous is not None and same
                    and now - previous[0] < ACCOUNT_MODEL_TTL_SECONDS):
                _remember(services, account_id, previous, version, previous_display)
                return previous_display if include_unavailable else previous[1]
            try:
                with credential_scope(account_id):
                    provider = services.registry[candidate.provider]
                    if candidate.provider == "miora":
                        async with services.miora_catalog_slots:
                            async with asyncio.timeout(MIORA_ACCOUNT_CATALOG_SECONDS):
                                models = await provider.list_models(data)
                    else:
                        models = await provider.list_models(data)
                display = {model.id.lower(): model for model in models}
                table = {lower: model for lower, model in display.items()
                         if model.account_available is not False}
            except Exception as error:  # noqa: BLE001 - 单账号失败不能扩大账号范围
                logger.warning("绑定账号目录拉取失败 %s: %s", account_id, type(error).__name__)
                table = previous[1] if previous is not None and same else {}
                display = previous_display if previous is not None and same else {}
            _remember(services, account_id, (now, table), version, display)
            return display if include_unavailable else table

    # 账号锁仍去重；不同 Miora 账号不再串行等待前一个运行时。
    # 其他渠道保持原有顺序，同一 HTTP 请求被取消时同时关闭所有子任务。
    async with asyncio.TaskGroup() as group:
        miora_tasks = {candidate.credential_id: group.create_task(load(candidate))
                       for candidate in candidates if candidate.provider == "miora"}
        other = {candidate.credential_id: await load(candidate) for candidate in candidates
                 if candidate.provider != "miora"}
    values = {**other, **{account_id: task.result() for account_id, task in miora_tasks.items()}}
    return {row.credential_id: table for row in candidates
            if (table := values[row.credential_id]) is not None}


def account_provider_tables(
    services: Services, tables: dict[str, dict[str, Model]]
) -> dict[str, dict[str, Model]]:
    """按账号模型白名单过滤后合并渠道表；账号删除或暂停立即从 union 抽掉。"""
    rows = {row["id"]: row for row in services.credentials.list_all()
            if row["enabled"] and not row["disabled"]}
    combined: dict[str, dict[str, Model]] = {}
    for account_id, table in tables.items():
        row = rows.get(account_id)
        if row is None:
            continue
        target = combined.setdefault(row["provider"], {})
        for lower, model in table.items():
            spec = row["allowed_models"]
            if model_allowed(normalize_model_key(_model_label(model)), spec):
                target[lower] = (
                    merge_account_model(target[lower], model) if lower in target else model)
    return combined


def merge_account_model(first: Model, second: Model) -> Model:
    """任一允许账号可用即可用，未知不能覆盖明确的拒绝；VIP要求仍保留。"""
    states = (first.account_available, second.account_available)
    available = True if True in states else False if False in states else None
    return replace(first, account_available=available,
                   required_account_tier=(
                       first.required_account_tier or second.required_account_tier),
                   account_unavailable_reason=None if available is True else (
                       first.account_unavailable_reason or second.account_unavailable_reason))


def restricted_entries(
    services: Services, tables: dict[str, dict[str, Model]],
    allowed_models: str = "", user_allowed_models: str = ""
) -> list[dict[str, Any]]:
    """为单个 Key 构造目录，不覆盖执行器共享的全池别名表。"""
    grouped: dict[str, dict[str, Any]] = {}
    failed = services.model_probes.failed()
    for provider, table in tables.items():
        visible = {lower: model
                   for lower, model in _visible(table, services.settings.blocklist_patterns).items()
                   if (provider, lower) not in failed}
        _merge_provider(grouped, provider, visible)
    entries = sorted(grouped.values(), key=_sort_key)
    _build_aliases(entries, parse_model_aliases(
        str(getattr(services.settings, "model_aliases", ""))))
    return [entry for entry in entries
            if model_allowed(entry["id"], allowed_models)
            and model_allowed(entry["id"], user_allowed_models)]
