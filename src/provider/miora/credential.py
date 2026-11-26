"""Miora 网页凭证：长期 JWT 与可轮转的 refresh token。

只保留认证与项目复用需要的字段；预刷新由共享 RefreshTask 独占，
运行时短票不落入长期凭证。JWT 只用于身份与到期元数据，验签由上游完成。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from ...compat.openai.request import InvalidRequest
from ..credential_fields import text_field
from ..token_expiry import credential_expiry, jwt_expiry


def jwt_subject(token: str) -> str:
    """读取 JWT sub；不能解析时不猜身份。"""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return ""
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        return ""
    return text_field(payload, "sub") if isinstance(payload, dict) else ""


def token_from_input(value: str) -> str:
    """整段 Cookie、Bearer 或裸 token；不会保留无关 Cookie。"""
    text = value.strip().strip("\"'").strip()
    if text.lower() == "bearer":
        return ""
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    if "=" not in text:
        return text
    for segment in text.split(";"):
        name, separator, token = segment.strip().partition("=")
        if separator and name == "authToken" and token.strip():
            return token.strip()
    return ""


@dataclass(slots=True)
class MioraCredential:
    access_token: str
    refresh_token: str = ""
    uid: str = ""
    expires_at: int = 0
    project_id: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> MioraCredential:
        if not isinstance(raw, dict):
            return cls(access_token="")
        token = token_from_input(
            text_field(raw, "access_token", "authToken", "accessToken", "token", "cookie", "raw")
        )
        # 公共时间解析已将无效元数据归一为未知0；新JWT的exp优先于旧记录的expires_at。
        expiry = jwt_expiry(token) or credential_expiry({**raw, "access_token": token})
        return cls(
            access_token=token,
            refresh_token=text_field(raw, "refresh_token", "refreshToken"),
            uid=jwt_subject(token) or text_field(raw, "uid", "user_id", "userId"),
            expires_at=expiry,
            project_id=text_field(raw, "project_id", "projectId", "workflowId"),
        )

    @classmethod
    def from_import(cls, raw: Any) -> MioraCredential:
        credential = cls.from_dict(raw)
        if not credential.access_token:
            raise InvalidRequest(
                "Miora 凭证缺少 authToken：请导入 authToken / refreshToken JSON "
                "或包含 authToken 的 Cookie"
            )
        return credential

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"access_token": self.access_token}
        for key in ("refresh_token", "uid", "expires_at", "project_id"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data

    def identity(self) -> str:
        if self.uid:
            return self.uid
        if not self.access_token:
            return ""
        return "token-" + hashlib.sha256(self.access_token.encode()).hexdigest()[:16]

    def needs_refresh(self, skew_seconds: int, now: int | None = None) -> bool:
        if not self.refresh_token or self.expires_at <= 0:
            return False
        moment = int(time.time()) if now is None else now
        return self.expires_at <= moment + max(0, skew_seconds)
