"""媒体自动工具只读取可信鉴权上下文，不从客户端 payload 推断用户或 Key。"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

from ..request_context import get_api_key_id

if TYPE_CHECKING:
    from ..api.deps import ApiKeyPrincipal

_principal: ContextVar[ApiKeyPrincipal | None] = ContextVar("media_principal", default=None)


def set_media_principal(principal: ApiKeyPrincipal | None) -> None:
    _principal.set(principal)


def get_media_principal() -> ApiKeyPrincipal | None:
    principal = _principal.get()
    if principal is None or not principal.key_id or principal.key_id != get_api_key_id():
        return None
    return principal
