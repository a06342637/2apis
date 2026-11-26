import json
import runpy
import sys
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace

import pytest

from scripts import update_host
from src.updater.service import UpdateService
from tests.updater.conftest import host as host


@pytest.mark.parametrize("mode", ["success", "path", "length", "secret", "type", "structure",
                                 "bad-action", "upstream", "unicode", "unicode-wrong"])
def test_socket_request_validation(mode):
    seen = []
    def dispatch(action, payload):
        seen.append((action, payload))
        if mode == "bad-action":
            raise ValueError("CI 未通过")
        if mode == "upstream":
            raise OSError("private error")
        return {"enabled": True}
    secret = "秘" * 32 if mode.startswith("unicode") else "s" * 32
    handler = update_host.handler_for(SimpleNamespace(dispatch=dispatch), secret)
    instance = handler.__new__(handler)
    payload = {"secret": secret, "action": "status"}
    if mode == "secret":
        payload["secret"] = "wrong"
    if mode == "unicode-wrong":
        payload["secret"] = "错" * 32
    if mode == "type":
        payload["secret"] = 1
    if mode == "structure":
        payload = []
    body = json.dumps(payload).encode()
    instance.path = "/evil" if mode == "path" else "/action"
    instance.headers = {"Content-Length": "50000" if mode == "length" else str(len(body))}
    instance.rfile = BytesIO(body)
    instance.wfile = BytesIO()
    instance.send_response = lambda *_: None
    instance.send_header = lambda *_: None
    instance.end_headers = lambda: None
    instance.log_message("must not log secret")
    instance.do_POST()
    response = json.loads(instance.wfile.getvalue())
    assert response["ok"] == (mode in {"success", "unicode"})
    assert secret not in json.dumps(response) and "private error" not in json.dumps(response)
    if mode == "bad-action":
        assert response["error"] == "CI 未通过"


def test_unix_server_bind():
    server = update_host.UnixHTTPServer.__new__(update_host.UnixHTTPServer)
    calls = []
    server.socket = SimpleNamespace(bind=calls.append)
    server.server_address = "/run/2apis.sock"
    server.server_bind()
    assert calls == ["/run/2apis.sock"] and server.server_name == "localhost"


def test_malformed_action_type_has_controlled_structure_error():
    secret = "s" * 32
    handler = update_host.handler_for(UpdateService.__new__(UpdateService), secret)
    instance = handler.__new__(handler)
    body = json.dumps({"secret": secret, "action": [], "payload": {}}).encode()
    instance.path = "/action"
    instance.headers = {"Content-Length": str(len(body))}
    instance.rfile, instance.wfile = BytesIO(body), BytesIO()
    instance.send_response = instance.send_header = lambda *_args: None
    instance.end_headers = lambda: None
    instance.do_POST()
    assert json.loads(instance.wfile.getvalue()) == {
        "ok": False, "error": "无效的助手请求结构",
    }


def test_executable_entry_rejects_non_root_before_touching_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(update_host.os, "geteuid", lambda: 1, raising=False)
    monkeypatch.setattr(sys, "argv", ["update_host", "--config", str(tmp_path / "not-read.json")])
    with pytest.raises(SystemExit, match="Linux root"):
        runpy.run_module("scripts.update_host", run_name="__main__")


@pytest.mark.parametrize("mode", ["windows", "non-root", "owner", "writable", "short", "success"])
def test_helper_main_only_root_protected_config(monkeypatch, tmp_path, host, mode):
    calls = []
    config_path = SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=1 if mode == "owner" else 0,
                                 st_mode=0o662 if mode == "writable" else 0o640))
    parser = SimpleNamespace(add_argument=lambda *_a, **_k: None,
                             parse_args=lambda _: SimpleNamespace(config=config_path))
    monkeypatch.setattr(update_host.argparse, "ArgumentParser", lambda **_: parser)
    monkeypatch.setattr(update_host, "os", SimpleNamespace(name="nt" if mode == "windows" else
                        "posix", geteuid=lambda: 1 if mode == "non-root" else 0,
                        chmod=lambda *_: calls.append("chmod")))
    host.secret_file.write_text("short" if mode == "short" else "s" * 32)
    monkeypatch.setattr(update_host.HostConfig, "load", lambda _: host)
    monkeypatch.setattr(update_host, "InstanceLock", lambda _: nullcontext())
    monkeypatch.setattr(update_host, "UpdateService", lambda _: SimpleNamespace(
                        recover=lambda: calls.append("recover")))
    class Server:
        def __init__(self, address, handler):
            calls.append(address)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return
        def serve_forever(self):
            calls.append("served")
    monkeypatch.setattr(update_host, "UnixHTTPServer", Server)
    if mode == "success":
        update_host.main([])
        assert calls == ["recover", str(host.socket_path), "chmod", "served"]
    else:
        with pytest.raises(SystemExit):
            update_host.main([])
