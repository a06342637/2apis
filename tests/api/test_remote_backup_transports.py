"""Exercise the actual OSS/SFTP adapters without uploading credentials or making network calls."""
from contextlib import contextmanager
from io import BytesIO
from stat import S_IFDIR, S_IFLNK, S_IFREG
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.remote_backup import transports as t
from src.remote_backup.config import BackupError, Oss, Sftp


@pytest.mark.parametrize(("override", "internal", "expected", "path"), [
    ("", False, "https://oss-cn-hongkong.aliyuncs.com", ""),
    ("", True, "https://oss-cn-hongkong-internal.aliyuncs.com", "/root/"),
    ("oss-custom.aliyuncs.com", False, "https://oss-custom.aliyuncs.com", "root"),
])
def test_oss_transport_context(monkeypatch, tmp_path, override, internal, expected, path):
    sdk = MagicMock()
    session, bucket = sdk.Session.return_value, sdk.Bucket.return_value
    sdk.ObjectIterator.return_value = [SimpleNamespace(
        key=("root/" if path else "") + "instance/name", size=3, last_modified=42)]
    result = BytesIO(b"abc")
    result.resp = SimpleNamespace(response=MagicMock())
    bucket.get_object.return_value = result
    monkeypatch.setattr(t, "oss2", sdk)
    target = tmp_path / "snapshot"
    with t.open_target(Oss(bucket="example", endpoint=override, internal=internal,
                           directory=path), "instance") as files:
        assert list(files.entries()) == [("name", 3, 42)]
        files.put("name", target)
        assert files.get("name", target) == 3
        assert target.read_bytes() == b"abc"
        files.delete("name")
    assert sdk.Bucket.call_args.args[1:] == (expected, "example")
    assert sdk.Bucket.call_args.kwargs["region"] == "cn-hongkong"
    assert session.session.trust_env is False
    assert bucket.put_object_from_file.call_args.kwargs["headers"]["x-oss-object-acl"] == "private"
    result.resp.response.close.assert_called_once()
    bucket.session.session.close.assert_called_once()


class SftpClient:
    def __init__(self):
        self.calls = []
        self.mode = S_IFREG
        self.fails = False

    def listdir_iter(self, path, *, read_aheads):
        assert read_aheads == 4 and path == "/root"
        return [SimpleNamespace(st_mode=mode, filename=name, st_size=3, st_mtime=42)
                for mode, name in [(S_IFDIR, "folder"), (S_IFREG, "name"), (None, "unknown")]]

    def lstat(self, path):
        return SimpleNamespace(st_mode=self.mode)

    @contextmanager
    def open(self, path, mode):
        if mode == "rb":
            yield BytesIO(b"abc")
        else:
            assert mode == "wx"
            file = BytesIO()
            yield file
            self.calls.append((path, file.getvalue()))

    def chmod(self, path, mode):
        self.calls.append((path, mode))

    def rename(self, old, new):
        if self.fails:
            raise OSError("failed rename")
        self.calls.append((old, new))

    def remove(self, path):
        self.calls.append((path, "removed"))
        if self.fails:
            raise OSError("failed cleanup")

    def mkdir(self, path, *, mode):
        self.calls.append((path, mode))
        if path.endswith("old"):
            raise OSError("exists")


def test_sftp_files_atomic_write_and_no_symlink_download(tmp_path):
    client = SftpClient()
    remote = t.SftpFiles(client, "/root")
    assert list(remote.entries()) == [("name", 3, 42)]
    path = tmp_path / "snapshot"
    assert remote.get("name", path) == 3
    remote.put("name", path)
    assert client.calls == [("/root/name.uploading", 0o600), ("/root/name.uploading", b"abc"),
                            ("/root/name.uploading", "/root/name")]
    remote.delete("name")
    assert client.calls[-1] == ("/root/name", "removed")
    client.fails = True
    with pytest.raises(OSError, match="rename"):
        remote.put("name", path)
    assert client.calls[-1] == ("/root/name.uploading", "removed")
    client.mode = S_IFLNK
    with pytest.raises(BackupError, match="普通文件"):
        remote.get("name", path)
    with pytest.raises(BackupError, match="普通目录"):
        t.make_directory(client, "/old/sub")
    client.mode = S_IFDIR
    t.make_directory(client, "old/sub")
    assert client.calls[-2:] == [("./old", 0o700), ("./old/sub", 0o700)]


@pytest.mark.parametrize("auth", ["password", "key", "invalid-key"])
def test_sftp_connect_is_pinned_and_never_uses_ambient_keys(monkeypatch, auth):
    sdk = MagicMock()
    sdk.SSHException = ValueError
    sdk.Ed25519Key.from_private_key.side_effect = ValueError("not Ed25519")
    sdk.ECDSAKey.from_private_key.side_effect = ValueError("not ECDSA")
    if auth == "invalid-key":
        sdk.RSAKey.from_private_key.side_effect = ValueError("not RSA")
    client = sdk.SSHClient.return_value.__enter__.return_value
    channel = client.open_sftp.return_value.__enter__.return_value.get_channel.return_value
    monkeypatch.setattr(t, "paramiko", sdk)
    target = Sftp(host="example.test", username="fixture", password="synthetic",
                  auth="password" if auth == "password" else "key", private_key="fixture",
                  passphrase="fixture-key-phrase", directory="/root")
    if auth == "invalid-key":
        with pytest.raises(BackupError, match="私钥"), t.open_target(target, "instance"):
            pytest.fail("invalid credentials should not open SFTP")
        client.connect.assert_not_called()
    else:
        with t.open_target(target, "instance") as remote:
            assert remote.root == "/root/instance"
        assert isinstance(client.set_missing_host_key_policy.call_args.args[0], t.HostKey)
        args = client.connect.call_args.kwargs
        assert args["allow_agent"] is args["look_for_keys"] is False
        assert args["password"] == ("synthetic" if auth == "password" else None)
        channel.settimeout.assert_called_once_with(t.TIMEOUT)
