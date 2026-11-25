from types import SimpleNamespace

import httpx
import pytest

from src.updater.client import UpdateClient


async def test_disabled_client():
    assert await UpdateClient("", "").call("status") == {"enabled": False}


@pytest.mark.parametrize("mode", ["success", "refused", "missing", "http", "file"])
async def test_unix_transport_and_controlled_errors(tmp_path, monkeypatch, mode):
    secret = tmp_path / "secret"
    secret.write_text("private-secret\n")
    seen = []
    class Client:
        def __init__(self, **values):
            assert values["timeout"] == 45
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return
        async def post(self, url, json):
            seen.append((url, json))
            if mode == "http":
                raise httpx.ConnectError("private details")
            value = ({"ok": True, "result": {"enabled": True}} if mode == "success"
                     else {"ok": False, "error": "CI failed"} if mode == "refused" else {})
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
    monkeypatch.setattr("src.updater.client.httpx.AsyncClient", Client)
    if mode == "file":
        secret.unlink()
    client = UpdateClient("/run/helper.sock", str(secret))
    if mode == "success":
        assert await client.call("check", {"x": 1}) == {"enabled": True}
        assert seen == [("http://localhost/action", {"secret": "private-secret",
                        "action": "check", "payload": {"x": 1}})]
    else:
        with pytest.raises(ValueError, match="CI failed" if mode == "refused" else "不可用"):
            await client.call("status")
