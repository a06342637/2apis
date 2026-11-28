"""Private OSS and host-key-verified SFTP transport for a single instance namespace."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
import posixpath
import stat
from contextlib import contextmanager, suppress
from pathlib import Path
from shutil import copyfileobj

import oss2
import paramiko

from .config import BackupError, Oss, Sftp

logging.getLogger("oss2").setLevel(logging.CRITICAL + 1)

TIMEOUT = 12
MAX_BYTES = 512 * 1024 * 1024


def copy_bounded(source, target: Path) -> int:
    size = 0
    with target.open("wb") as out:
        while data := source.read(1024 * 1024):
            size += len(data)
            if size > MAX_BYTES:
                raise BackupError("远端备份超过 512 MB 恢复上限")
            out.write(data)
    return size


class HostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint

    def missing_host_key(self, client, hostname, key):
        digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if not hmac.compare_digest("SHA256:" + digest, self.fingerprint):
            raise BackupError("SFTP 主机公钥指纹不匹配，连接已拒绝")


class OssFiles:
    def __init__(self, config: Oss, namespace: str):
        suffix = "-internal" if config.internal else ""
        domain = config.endpoint or f"oss-{config.region}{suffix}.aliyuncs.com"
        session = oss2.Session()
        session.session.trust_env = False
        self.bucket = oss2.Bucket(oss2.AuthV4(config.key_id, config.key_secret),
                                  "https://" + domain, config.bucket,
                                  session=session, region=config.region, connect_timeout=TIMEOUT)
        path = config.directory.strip("/")
        self.prefix = f"{path}/{namespace}/" if path else f"{namespace}/"

    def entries(self):
        for item in oss2.ObjectIterator(self.bucket, prefix=self.prefix, max_keys=100):
            yield item.key.removeprefix(self.prefix), item.size, item.last_modified

    def put(self, name, source):
        self.bucket.put_object_from_file(self.prefix + name, str(source),
                                         headers={"x-oss-object-acl": "private"})

    def get(self, name, target):
        response = self.bucket.get_object(self.prefix + name)
        try:
            return copy_bounded(response, target)
        finally:
            response.resp.response.close()

    def delete(self, name):
        self.bucket.delete_object(self.prefix + name)


class SftpFiles:
    def __init__(self, client, root: str):
        self.client = client
        self.root = root

    def entries(self):
        for row in self.client.listdir_iter(self.root, read_aheads=4):
            if stat.S_ISREG(row.st_mode or 0):
                yield row.filename, row.st_size, row.st_mtime

    def put(self, name, source):
        target = posixpath.join(self.root, name)
        temporary = target + ".uploading"
        try:
            with self.client.open(temporary, "wx") as destination, source.open("rb") as file:
                self.client.chmod(temporary, 0o600)
                copyfileobj(file, destination, length=64 * 1024)
            self.client.rename(temporary, target)
        except Exception:
            with suppress(OSError):
                self.client.remove(temporary)
            raise

    def get(self, name, target):
        path = posixpath.join(self.root, name)
        if not stat.S_ISREG(self.client.lstat(path).st_mode or 0):
            raise BackupError("远端备份不是普通文件")
        with self.client.open(path, "rb") as source:
            return copy_bounded(source, target)

    def delete(self, name):
        self.client.remove(posixpath.join(self.root, name))


def make_directory(client, path):
    parent = "/" if path.startswith("/") else "."
    for part in filter(None, path.split("/")):
        parent = posixpath.join(parent, part)
        try:
            client.mkdir(parent, mode=0o700)
        except OSError:
            if not stat.S_ISDIR(client.lstat(parent).st_mode or 0):
                raise BackupError("SFTP 保存路径不是普通目录") from None


@contextmanager
def open_target(config: Oss | Sftp, namespace: str):
    if isinstance(config, Oss):
        store = OssFiles(config, namespace)
        try:
            yield store
        finally:
            store.bucket.session.session.close()
        return
    with paramiko.SSHClient() as client:
        client.set_missing_host_key_policy(HostKey(config.fingerprint))
        key = None
        if config.auth == "key":
            for key_type in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                try:
                    key = key_type.from_private_key(io.StringIO(config.private_key),
                                                    password=config.passphrase or None)
                    break
                except paramiko.SSHException:
                    continue
            if key is None:
                raise BackupError("无法读取 SFTP 私钥，请检查格式和口令")
        client.connect(config.host, port=config.port, username=config.username,
                       password=config.password if config.auth == "password" else None,
                       pkey=key, allow_agent=False, look_for_keys=False, timeout=TIMEOUT,
                       banner_timeout=TIMEOUT, auth_timeout=TIMEOUT, channel_timeout=TIMEOUT)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(TIMEOUT)
            root = posixpath.join(config.directory or ".", namespace)
            make_directory(sftp, root)
            yield SftpFiles(sftp, root)
