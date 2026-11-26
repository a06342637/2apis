"""批量明文凭证导出：严格选择、逐条解密、完整校验后一次交付。"""

from __future__ import annotations

import json

from ..compat.openai.request import InvalidRequest
from ..db.crypto import CredentialDecryptError
from ..db.repo import CredentialRepository

BATCH_EXPORT_MAX_IDS = 5000
BATCH_EXPORT_MAX_BYTES = 16 * 1024 * 1024
_ENCODE_CHUNK_CHARS = 8192


def validate_export_ids(payload: dict) -> list[str]:
    """拒绝混入无效值的整批请求，按第一次出现的顺序去重。"""
    raw = payload.get("ids")
    if not isinstance(raw, list) or not raw:
        raise InvalidRequest("请选择至少一个凭证账号后重试")
    # 按请求条数限制，不能用大量重复 ID 绕过上限。
    if len(raw) > BATCH_EXPORT_MAX_IDS:
        raise InvalidRequest(f"最多导出 {BATCH_EXPORT_MAX_IDS} 个凭证账号，请减少选择后重试")
    ids: list[str] = []
    seen: set[str] = set()
    for credential_id in raw:
        if not isinstance(credential_id, str) or not credential_id.strip():
            raise InvalidRequest("所选凭证无效，请刷新列表后重新选择")
        if credential_id not in seen:
            seen.add(credential_id)
            ids.append(credential_id)
    return ids


def _append_json(buffer: bytearray, text: str) -> None:
    """UTF-8 容量检查；分块编码避免大字符串生成额外的无界 bytes 副本。"""
    if len(text) > BATCH_EXPORT_MAX_BYTES - len(buffer):
        raise InvalidRequest("导出文件超过 16 MiB，请减少选择后重试")
    for offset in range(0, len(text), _ENCODE_CHUNK_CHARS):
        encoded = text[offset:offset + _ENCODE_CHUNK_CHARS].encode("utf-8")
        if len(encoded) > BATCH_EXPORT_MAX_BYTES - len(buffer):
            raise InvalidRequest("导出文件超过 16 MiB，请减少选择后重试")
        buffer.extend(encoded)


def encode_credential_export(repository: CredentialRepository, ids: list[str]) -> bytes:
    """不构造全量明文列表；任意条目出错时丢弃整份未发送的响应。"""
    buffer = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    _append_json(buffer, "[")
    for index, credential_id in enumerate(ids):
        try:
            # 与单条导出共用密码 / 网页 Cookie 的移除规则，且不触发刷新或探测。
            entry = repository.export_entry(credential_id)
            if entry is None:
                raise InvalidRequest("所选凭证不存在，请刷新列表后重新选择并重试")
            if index:
                _append_json(buffer, ",")
            for text in encoder.iterencode(entry):
                _append_json(buffer, text)
        except InvalidRequest:
            raise
        except (CredentialDecryptError, ValueError, TypeError, AttributeError,
                RecursionError) as error:
            # 损坏密文 / 存储结构 / 编码的异常原文可能包含凭证，不能回显或记日志。
            raise InvalidRequest("无法导出所选凭证，请检查凭证状态后重试") from error
    _append_json(buffer, "]")
    return bytes(buffer)
