"""私有、原子 JSON 任务与短期 HMAC 资源地址，不存上游鉴权 Token。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

from ..security.private_files import secure_dir, secure_file


class MediaStore:
    def __init__(self, directory: Path, secret: str, *, ttl: int = 3600, clock=time.time):
        self.directory = directory
        self.secret = secret.encode()
        self.ttl = ttl
        self.clock = clock
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        secure_dir(directory)

    def path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not re.fullmatch(r"media_[a-f0-9]{32}", job_id):
            raise HTTPException(404, "media job not found")
        return self.directory / f"{job_id}.json"

    def load(self, job_id: str) -> dict:
        path = self.path(job_id)
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "media job not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, job: dict) -> dict:
        target = self.path(job["id"])
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        job = {**job, "updated_at": int(self.clock())}
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(job, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            secure_file(temp)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return job

    def create(self, *, job_id: str | None = None, **values) -> dict:
        return self.save(
            {
                "id": job_id or "media_" + uuid.uuid4().hex,
                "created_at": int(self.clock()),
                "status": "submitting",
                "progress": 0,
                "files": [],
                "error": "",
                **values,
            }
        )

    def intent_id(self, key_id: str, nonce: str) -> str:
        if not isinstance(nonce, str) or not 1 <= len(nonce) <= 128 or not nonce.isascii():
            raise HTTPException(400, "Idempotency-Key must be 1..128 ASCII characters")
        digest = hmac.new(self.secret, f"intent:{key_id}:{nonce}".encode(), hashlib.sha256)
        return "media_" + digest.hexdigest()[:32]

    def asset(self, job_id: str, index: int) -> Path:
        self.path(job_id)
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 16:
            raise HTTPException(404, "media file not found")
        return self.directory / f"{job_id}-{index}.bin"

    def signature(self, job: dict, index: int, expires: int) -> str:
        message = f"{job['id']}:{index}:{expires}:{job['key_id']}".encode()
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def signed_url(self, job: dict, base_url: str, index: int = 0) -> str:
        expires = int(self.clock()) + self.ttl
        signature = self.signature(job, index, expires)
        return (
            f"{base_url.rstrip('/')}/v1/media/{job['id']}/content"
            f"?index={index}&expires={expires}&signature={signature}"
        )

    def verify_signature(self, job: dict, index: int, expires: int, signature: str) -> None:
        now = int(self.clock())
        if (
            not now < expires <= now + self.ttl
            or not isinstance(signature, str)
            or not re.fullmatch(r"[a-f0-9]{64}", signature)
            or not hmac.compare_digest(signature, self.signature(job, index, expires))
        ):
            raise HTTPException(403, "media link expired or invalid")
