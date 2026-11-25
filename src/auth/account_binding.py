"""API Key 的账号集合：空数组不限制，非空集合始终严格收窄。"""

from __future__ import annotations

import json


def normalize_credential_ids(raw: object) -> list[str]:
    """校验 JSON string[] 并按输入顺序去重；null 不是清空的合法形态。"""
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        raise ValueError("credential_ids must be an array of non-empty credential IDs")
    return list(dict.fromkeys(item.strip() for item in raw))


def credential_ids_from_text(raw: str) -> list[str]:
    """存储值按写入时同一规则读回，脏策略不得退化成全池权限。"""
    return normalize_credential_ids(json.loads(raw))
