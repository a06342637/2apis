import httpx
import pytest

from src.media import download as mod
from src.media.download import MediaDownloader
from src.provider.base import UpstreamProtocolViolation

HOST = "sign.miorausercontent.com"
URL = f"https://{HOST}/file/user/sample?sign=test"


@pytest.fixture
def private_files(monkeypatch):
    calls = []
    # 私有文件保护的 OS 细节另有完整测试，下载层须在原子替换前调用该入口。
    monkeypatch.setattr(mod, "secure_file", lambda path: calls.append(path))

    async def public(host):
        assert host == HOST

    monkeypatch.setattr(mod, "ensure_public_host", public)
    return calls


@pytest.mark.parametrize(
    "url",
    [
        "http://" + HOST,
        "https://example.com/a",
        f"https://{HOST}:444/a",
        f"https://u@{HOST}/a",
        f"https://u:p@{HOST}/a",
        f"https://{HOST}/a#x",
        f"https://{HOST}:bad/a",
        "https://[bad",
    ],
)
async def test_untrusted_urls_are_rejected_before_network(url, private_files):
    with pytest.raises(UpstreamProtocolViolation):
        await MediaDownloader((HOST,)).validate(url)


async def test_public_dns_check_is_required_even_for_allowlisted_hostname(
    monkeypatch, private_files
):
    async def reject(host):
        raise UpstreamProtocolViolation("private address")

    monkeypatch.setattr(mod, "ensure_public_host", reject)
    with pytest.raises(UpstreamProtocolViolation, match="private address"):
        await MediaDownloader((HOST,)).validate(URL)


@pytest.mark.parametrize(
    "kind, mime", [("image", "image/png; charset=binary"), ("video", "video/mp4")]
)
async def test_download_is_atomic_and_carries_no_authentication_headers(
    tmp_path, private_files, kind, mime
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": mime}, content=b"real-media-content")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        target = tmp_path / "sample.bin"
        target.write_bytes(b"old")
        result = await MediaDownloader((HOST,), client=client).download(URL, target, kind)
        assert target.read_bytes() == b"real-media-content"
        assert result == {"mime_type": mime.split(";")[0], "bytes": 18}
        assert not client.is_closed
    assert len(requests) == 1
    assert requests[0].headers["accept-encoding"] == "identity"
    assert "authorization" not in requests[0].headers and "cookie" not in requests[0].headers
    assert len(private_files) == 1 and private_files[0].suffix == ".tmp"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "status, mime, data, error",
    [
        (302, "image/png", b"x", UpstreamProtocolViolation),
        (404, "image/png", b"x", httpx.HTTPStatusError),
        (200, "text/html", b"html", UpstreamProtocolViolation),
        (200, "video/mp4", b"x", UpstreamProtocolViolation),
        (200, "image/png", b"", UpstreamProtocolViolation),
    ],
)
async def test_redirects_errors_bad_mime_and_empty_files_cannot_replace_asset(
    tmp_path,
    private_files,
    status,
    mime,
    data,
    error,
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"content-type": mime, "location": "https://example.com"}, content=data
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        target = tmp_path / "sample.bin"
        target.write_bytes(b"old")
        with pytest.raises(error):
            await MediaDownloader((HOST,), client=client).download(URL, target, "image")
        assert target.read_bytes() == b"old"
        assert list(tmp_path.iterdir()) == [target]
    assert len(calls) == 1


@pytest.mark.parametrize("remaining", [None, 2, 100])
async def test_per_file_and_remaining_response_budget_enforced(tmp_path, private_files, remaining):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"large media",
            )
        )
    ) as client:
        target = tmp_path / "sample.bin"
        with pytest.raises(UpstreamProtocolViolation, match="大小限制"):
            await MediaDownloader((HOST,), max_bytes=4, client=client).download(
                URL, target, "image", max_bytes=remaining
            )
        assert not target.exists() and list(tmp_path.iterdir()) == []


async def test_owned_http_client_has_no_env_proxy_or_redirects_and_is_closed(
    tmp_path,
    monkeypatch,
    private_files,
):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"image",
            )
        )
    )
    options = []

    def factory(**values):
        options.append(values)
        return client

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    await MediaDownloader((HOST,)).download(URL, tmp_path / "sample.bin", "image")
    assert options == [{"follow_redirects": False, "trust_env": False, "timeout": 120}]
    assert client.is_closed


async def test_partial_stream_failure_cleans_temp_and_preserves_original(tmp_path, private_files):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 65536
            raise httpx.ReadError("truncated")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/png"},
                stream=Broken(),
            )
        )
    ) as client:
        target = tmp_path / "sample.bin"
        target.write_bytes(b"old")
        with pytest.raises(httpx.ReadError):
            await MediaDownloader((HOST,), client=client).download(URL, target, "image")
        assert target.read_bytes() == b"old"
        assert list(tmp_path.iterdir()) == [target]
