import httpx
from anyio import CancelScope, sleep

from src.provider.miora.acp import AcpConnection


async def test_cancelled_asgi_scope_still_closes_context_and_deletes_connection():
    deleted, closed = [], []

    def response(request):
        deleted.append(request.method)
        return httpx.Response(200)

    class Context:
        async def __aexit__(self, *_args):
            await sleep(0)
            closed.append(1)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        connection = AcpConnection(client, "https://example.com/acp", "synthetic")
        connection._context = Context()
        connection.headers["Acp-Connection-Id"] = "synthetic-id"
        with CancelScope() as scope:
            scope.cancel()
            await connection.__aexit__(None, None, None)
    assert closed == [1]
    assert deleted == ["DELETE"]
