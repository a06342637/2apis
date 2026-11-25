"""绑定账号的目录缓存与模型 union；不使用其它账号的目录兜底。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from ..auth.access import model_allowed
from ..db.crypto import CredentialDecryptError
from ..model_alias import parse_model_aliases
from ..provider.base import Model
from ..provider.naming import normalize_model_key
from ..request_context import credential_scope
from .deps import Services
from .model_merge import _build_aliases, _merge_provider, _model_label, _sort_key, _visible

logger = logging.getLogger(__name__)
ACCOUNT_MODEL_TTL_SECONDS = 300
MAX_ACCOUNT_MODEL_CACHES = 128


def _forget(services: Services, account_id: str) -> None:
    services.credential_model_cache.pop(account_id, None)
    services.credential_model_versions.pop(account_id, None)


def _remember(services: Services, account_id: str, entry: tuple[float, dict[str, Model]],
               version: str) -> None:
    """按最近访问保留热目录，淘汰仅丢缓存，不改变 Key 的绑定集合。"""
    services.credential_model_cache.pop(account_id, None)
    services.credential_model_cache[account_id] = entry
    services.credential_model_versions[account_id] = version
    while len(services.credential_model_cache) > MAX_ACCOUNT_MODEL_CACHES:
        _forget(services, next(iter(services.credential_model_cache)))


async def account_model_tables(
    services: Services, credential_ids: tuple[str, ...], provider_binding: str = ""
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
    candidates = [candidate for candidate in current
                  if candidate.credential_id in selected
                  and candidate.enabled and not candidate.disabled
                  and (not provider_binding or candidate.provider == provider_binding)
                  and candidate.provider in services.registry]
    tables: dict[str, dict[str, Model]] = {}
    for candidate in candidates:
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
                    continue
                version = hashlib.sha256(json.dumps(data, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()
            except Exception as error:  # noqa: BLE001 - 解密/读票失败不得使用旧账号目录
                reason = ("凭证解密失败" if isinstance(error, CredentialDecryptError)
                          else type(error).__name__)
                logger.warning("绑定账号目录凭证读取失败 %s: %s", account_id, reason)
                _forget(services, account_id)
                tables[account_id] = {}
                continue
            same = previous_version == version
            if previous is not None and same and now - previous[0] < ACCOUNT_MODEL_TTL_SECONDS:
                _remember(services, account_id, previous, version)
                tables[account_id] = previous[1]
                continue
            try:
                with credential_scope(account_id):
                    models = await services.registry[candidate.provider].list_models(data)
                table = {model.id.lower(): model for model in models}
            except Exception as error:  # noqa: BLE001 - 单账号失败不能扩大账号范围
                logger.warning("绑定账号目录拉取失败 %s: %s", account_id, type(error).__name__)
                table = previous[1] if previous is not None and same else {}
            _remember(services, account_id, (now, table), version)
            tables[account_id] = table
    return tables


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
                target[lower] = model
    return combined


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
