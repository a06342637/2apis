"""Typed configuration and atomic encrypted persistence for remote backups."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..db.crypto import CredentialCipher
from ..security.private_files import secure_dir, secure_file


class BackupError(ValueError):
    """A user-facing message that never contains transport details or secrets."""


def clean_path(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_./-]*", value) or ".." in value.split("/"):
        raise ValueError("仅支持普通目录名，不能包含上级路径")
    return value.rstrip("/") or value[:1]


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    keep: int = Field(10, ge=1, le=100)
    directory: str = Field("", max_length=240)

    _path = field_validator("directory")(clean_path)


class Oss(Target):
    region: str = Field("cn-hongkong", pattern=r"^[a-z0-9-]{2,40}$")
    bucket: str = Field("", pattern=r"^(?:[a-z0-9][a-z0-9-]{1,61}[a-z0-9])?$")
    key_id: str = Field("", max_length=128, repr=False)
    key_secret: str = Field("", max_length=512, repr=False)
    internal: bool = False
    endpoint: str = Field("", pattern=r"^(?:oss-[a-z0-9-]+\.aliyuncs\.com)?$")

    @model_validator(mode="after")
    def validate_target(self):
        if self.enabled and not (self.bucket and self.key_id and self.key_secret):
            raise ValueError("启用 OSS 需要 Bucket、AccessKey ID 和 Secret")
        return self


class Sftp(Target):
    host: str = Field("", max_length=253, pattern=r"^[A-Za-z0-9.:-]*$")
    port: int = Field(22, ge=1, le=65535)
    username: str = Field("", max_length=128)
    password: str = Field("", max_length=1024, repr=False)
    private_key: str = Field("", max_length=24000, repr=False)
    passphrase: str = Field("", max_length=1024, repr=False)
    fingerprint: str = Field("", pattern=r"^(?:SHA256:[A-Za-z0-9+/]{43})?$")
    auth: str = Field("password", pattern=r"^(password|key)$")

    @model_validator(mode="after")
    def validate_target(self):
        credential = self.password if self.auth == "password" else self.private_key
        if self.enabled and not (self.host and self.username and credential and self.fingerprint):
            raise ValueError("启用 SFTP 需要主机、用户名、认证信息和主机公钥指纹")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    days: int = Field(1, ge=1, le=365)
    time: str = Field("04:30", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field("Asia/Shanghai", max_length=80)
    oss: Oss = Field(default_factory=Oss)
    sftp: Sftp = Field(default_factory=Sftp)

    @field_validator("timezone")
    @classmethod
    def validate_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("请输入有效时区，例如 Asia/Shanghai") from error
        return value

    @model_validator(mode="after")
    def require_target(self):
        if self.enabled and not (self.oss.enabled or self.sftp.enabled):
            raise ValueError("启用定时备份前，请至少启用一个目标")
        return self


PRIVATE = {"oss": ("key_secret",), "sftp": ("password", "private_key", "passphrase")}


def public_config(config: Config) -> dict:
    result = config.model_dump()
    for name, keys in PRIVATE.items():
        for key in keys:
            result[name][f"has_{key}"] = bool(result[name].pop(key))
    return result


def merge_config(config: Config, payload: dict) -> Config:
    merged = config.model_dump()
    for name, fields in payload.items():
        if name in PRIVATE and isinstance(fields, dict):
            merged[name].update({key: value for key, value in fields.items()
                                 if key not in PRIVATE[name] or value != ""})
        else:
            merged[name] = fields
    return Config.model_validate(merged)


def write_private(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        secure_file(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, directory: Path, cipher: CredentialCipher):
        self.directory = directory
        self.cipher = cipher

    def config(self) -> Config:
        path = self.directory / "config.enc"
        return (Config.model_validate_json(self.cipher.decrypt(path.read_bytes()))
                if path.is_file() else Config())

    def save(self, config: Config) -> None:
        write_private(self.directory / "config.enc",
                      self.cipher.encrypt(config.model_dump_json().encode()))

    def state(self) -> dict:
        path = self.directory / "state.json"
        return json.loads(path.read_bytes()) if path.is_file() else {}

    def save_state(self, state: dict) -> None:
        write_private(self.directory / "state.json", json.dumps(state).encode())
