"""让异步流的拉取与关闭使用同一 Context，保留跨 yield 的上下文作用域。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextvars import copy_context
from typing import Any

from ..provider.base import aclose_stream


class ContextBoundIterator:
    """按消费方要求逐帧拉取，不预读；调用方先结束在途拉取，再关闭。"""

    def __init__(self, source: AsyncIterator[Any]) -> None:
        self._source = source.__aiter__()
        self._context = copy_context()

    def __aiter__(self) -> ContextBoundIterator:
        return self

    async def __anext__(self) -> Any:
        # Task 默认复制 Context，跨 Task 恢复 ContextVar token 会失败；复用原对象。
        # 先进入源再等待，极早取消时已进入的租约包装器也能执行 finally。
        return await asyncio.Task(anext(self._source), loop=asyncio.get_running_loop(),
                                  context=self._context, eager_start=True)

    async def aclose(self) -> None:
        await asyncio.create_task(aclose_stream(self._source), context=self._context)
