"""helper 入口只获取一次实例锁，并在退出时释放。"""

from types import SimpleNamespace

import pytest

from scripts import update_host
from tests.updater.conftest import host as host


def test_helper_lock_is_acquired_once_and_released(monkeypatch, host):
    calls = []
    parser = SimpleNamespace(add_argument=lambda *_a, **_k: None,
        parse_args=lambda _: SimpleNamespace(config=SimpleNamespace(
            stat=lambda: SimpleNamespace(st_uid=0, st_mode=0o600))))
    monkeypatch.setattr(update_host.argparse, "ArgumentParser", lambda **_: parser)
    monkeypatch.setattr(update_host, "os", SimpleNamespace(name="posix", geteuid=lambda: 0,
                                                         chmod=lambda *_: None))
    monkeypatch.setattr(update_host.HostConfig, "load", lambda _: host)
    host.secret_file.write_text("s" * 32)
    monkeypatch.setattr(update_host, "UpdateService",
                        lambda _: SimpleNamespace(recover=lambda: None))

    class Lock:
        def __init__(self, path):
            assert path == host.state_dir
            self.locked = False

        def acquire(self):
            assert not self.locked
            self.locked = True
            calls.append("acquired")

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_args):
            self.locked = False
            calls.append("released")

    class Server:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def serve_forever(self):
            raise OSError("controlled shutdown")

    monkeypatch.setattr(update_host, "InstanceLock", Lock)
    monkeypatch.setattr(update_host, "UnixHTTPServer", Server)
    with pytest.raises(OSError, match="controlled shutdown"):
        update_host.main([])
    assert calls == ["acquired", "released"]
