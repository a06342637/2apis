"""生命周期先排空取消后的探测，再关闭 provider 和数据库。"""

import asyncio

from src import main
from tests.helpers.auth import isolated_settings


async def test_cancelled_probe_finishes_before_resources_close(tmp_path, monkeypatch):
    # 本例只验证生命周期顺序，平台门禁由平台测试单独验证。
    monkeypatch.setattr(main, "require_supported_platform", lambda: None)
    monkeypatch.setattr("src.db.conn.secure_dir", lambda *_args, **_kwargs: True)
    finished = asyncio.Event()
    entered = asyncio.Event()
    closed = []

    class Provider:
        async def aclose(self):
            assert finished.is_set()
            closed.append(True)

    app = main.build_app(isolated_settings(tmp_path), providers={"dummy": Provider()})

    async def probe():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            finished.set()

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(probe())
        app.state.pending_probes.append(task)
        await entered.wait()
    assert task.cancelled()
    assert closed == [True]
    assert app.state.pending_probes == []
