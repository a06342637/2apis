"""Miora 独立请求分摊、模型额度准入与同会话工具亲和。"""

import asyncio
from collections import Counter

import pytest

from src.compat.openai.request import parse_chat_request
from src.engine.affinity import ConversationAffinity
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.inflight import InflightGuard
from src.engine.model_resolver import ModelTarget
from src.engine.scheduler import Scheduler
from src.provider.base import Event, EventKind, Model, Quota
from src.provider.miora.client import MioraHTTPError


class Accounts:
    def __init__(self):
        self.calls = []
        self.failures = {}
        self.partial = False
        self.tools = False
        self.release = None
        self.ready = asyncio.Event()
        self.wait_count = 0

    async def stream_chat(self, data, payload, model):
        uid = data["uid"]
        self.calls.append((uid, payload, model))
        if self.release is not None:
            if len(self.calls) == self.wait_count:
                self.ready.set()
            await self.release.wait()
        if uid in self.failures:
            if self.partial:
                yield Event(kind=EventKind.CONTENT, content="first account partial")
            raise self.failures[uid]
        if self.tools and payload["messages"][-1]["role"] == "user":
            yield Event(kind=EventKind.TOOL_CALLS, tool_calls=[{
                "index": 0, "id": "call-1", "type": "function",
                "function": {"name": "lookup", "arguments": '{"id":1}'},
            }])
            yield Event(kind=EventKind.FINISH, finish_reason="tool_calls")
        else:
            yield Event(kind=EventKind.CONTENT, content="account " + uid)
            yield Event(kind=EventKind.FINISH, finish_reason="stop")


def engine(repo, *, affinity=True, count=3):
    credentials, _db = repo
    provider = Accounts()
    ids = [credentials.add(provider="miora", credential_data={"uid": str(index)})
           for index in range(count)]
    ids.sort()
    for index, cid in enumerate(ids):
        credentials.save_quota(cid, Quota(remaining=10 + index * 10, total=100, probed_at=1_000))
    executor = Executor(ExecutorDeps(
        credentials=credentials, providers={"miora": provider}, scheduler=Scheduler(),
        default_model="chat", inflight=InflightGuard(lambda: 0),
        affinity=ConversationAffinity(ttl_seconds=3600) if affinity else None,
    ))
    uids = [credentials.credential_data(cid)["uid"] for cid in ids]
    return executor, credentials, provider, ids, uids


def request(*, session=None, messages=None):
    raw = {"model": "chat@miora", "messages": messages or [{"role": "user", "content": "hi"}]}
    if session is not None:
        raw["conversation_id"] = session
    return parse_chat_request(raw)


def catalog(ids, rate=1):
    return {cid: {"chat": Model(id="chat", credit_rate=rate)} for cid in ids}


async def run(executor, stream, message=None, **scope):
    message = message or request()
    if stream:
        return b"".join([frame async for frame in executor.stream_guarded(message, **scope)])
    return await executor.complete(message, **scope)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_miora_independent_requests_share_accounts_without_draining_one(repo, bound, stream):
    executor, _credentials, provider, ids, uids = engine(repo)
    scope = {"credential_ids": tuple(ids), "credential_models": catalog(ids)} if bound else {}
    for _ in range(6):
        await run(executor, stream, **scope)
    assert [call[0] for call in provider.calls] == uids * 2
    assert Counter(call[0] for call in provider.calls) == dict.fromkeys(uids, 2)
    assert executor._deps.inflight.total() == 0


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_miora_parallel_requests_reserve_distinct_accounts_and_release(repo, bound, stream):
    executor, _credentials, provider, ids, uids = engine(repo)
    provider.release = asyncio.Event()
    provider.wait_count = 3
    scope = {"credential_ids": tuple(ids), "credential_models": catalog(ids)} if bound else {}
    tasks = [asyncio.create_task(run(executor, stream, **scope)) for _ in ids]
    try:
        await provider.ready.wait()
        assert {call[0] for call in provider.calls} == set(uids)
        assert [executor._deps.inflight.count(cid) for cid in ids] == [1, 1, 1]
    finally:
        provider.release.set()
        await asyncio.gather(*tasks)
    assert executor._deps.inflight.total() == 0


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("rate", [None, 1])
async def test_miora_zero_credit_is_skipped_but_unknown_or_small_positive_balance_is_usable(
    repo, stream, rate,
):
    executor, credentials, provider, ids, uids = engine(repo)
    credentials.save_quota(ids[0], Quota(remaining=0, total=100, probed_at=1_000))
    credentials.save_quota(ids[1], Quota(
        remaining=None, total=100, probe_failed=True, probed_at=1_000))
    credentials.save_quota(ids[2], Quota(remaining=0.01, total=100, probed_at=1_000))
    for cid in ids[1:]:
        await run(executor, stream, credential_ids=(ids[0], cid),
                  credential_models=catalog([ids[0], cid], rate))
    assert [call[0] for call in provider.calls] == uids[1:]
    assert next(c for c in credentials.candidates() if c.credential_id == ids[2]).health == 0
    with executor._request_scope("chat", credential_ids=(ids[0],),
                                 credential_models=catalog([ids[0]], rate)):
        target = ModelTarget(model="chat", providers=("miora",))
        assert executor._no_pick_breakdown(target, set()) == {"quota_exhausted": 1}


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_zero_balance_free_exception_uses_account_catalog_not_pool_union(repo, stream):
    executor, credentials, provider, ids, uids = engine(repo, count=2)
    for cid in ids:
        credentials.save_quota(cid, Quota(remaining=0, total=100, probed_at=1_000))
    executor._deps.model_aliases = {"miora": {"chat": "chat"}}
    executor._deps.model_list_cache = {"miora": {"chat": Model(id="chat", credit_rate=0)}}
    tables = catalog(ids)
    tables[ids[1]]["chat"].credit_rate = 0
    await run(executor, stream, credential_ids=tuple(ids), credential_models=tables)
    assert [call[0] for call in provider.calls] == [uids[1]]
    # 新实测扣费覆盖陈旧 x0 目录：不能继续借免费例外撞空余额。
    executor._deps.paid_models = lambda: {("miora", "chat"): 0.2}
    if stream:
        assert b"no_healthy_credential" in await run(
            executor, stream, credential_ids=tuple(ids), credential_models=tables)
    else:
        with pytest.raises(NoHealthyCredential):
            await run(executor, stream, credential_ids=tuple(ids), credential_models=tables)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_confirmed_free_provider_model_can_use_zero_balance_without_account_binding(
    repo, stream,
):
    executor, credentials, provider, _ids, _uids = engine(repo, count=1)
    credentials.save_quota(_ids[0], Quota(remaining=0, total=0, probed_at=1_000))
    executor._deps.model_aliases = {"miora": {"chat": "chat"}}
    executor._deps.model_list_cache = {"miora": {"chat": Model(id="chat", credit_rate=0)}}
    await run(executor, stream)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("status", [401, 402, 429, 503])
@pytest.mark.parametrize("stream", [False, True])
async def test_miora_upstream_failure_before_content_switches_to_another_scoped_account(
    repo, stream, status,
):
    executor, credentials, provider, ids, uids = engine(repo)
    provider.failures[uids[0]] = MioraHTTPError(status, b'{"message":"account unavailable"}')
    await run(executor, stream, credential_ids=tuple(ids[:2]), credential_models=catalog(ids))
    assert [call[0] for call in provider.calls] == uids[:2]
    failed = next(candidate for candidate in credentials.candidates()
                  if candidate.credential_id == ids[0])
    assert failed.disabled if status == 401 else failed.cooling_until is not None
    await run(executor, stream, credential_ids=tuple(ids[:2]), credential_models=catalog(ids))
    assert [call[0] for call in provider.calls] == [uids[0], uids[1], uids[1]]


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_partial_stream_is_not_spliced_with_another_account(repo, stream):
    executor, _credentials, provider, ids, uids = engine(repo)
    provider.failures[uids[0]] = MioraHTTPError(402, b'{"message":"credit exhausted"}')
    provider.partial = True
    result = await run(executor, stream, credential_ids=tuple(ids), credential_models=catalog(ids))
    if stream:
        assert b"first account partial" in result and b"error" in result
        assert [call[0] for call in provider.calls] == uids[:1]
    else:
        assert result["choices"][0]["message"]["content"] == "account " + uids[1]
        assert [call[0] for call in provider.calls] == uids[:2]


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_tool_roundtrip_keeps_scoped_affinity_and_new_session_rotates(repo, stream):
    executor, _credentials, provider, ids, uids = engine(repo, affinity=True)
    provider.tools = True
    scope = {"credential_ids": tuple(ids), "credential_models": catalog(ids)}
    await run(executor, stream, request(session="tools"), **scope)
    tool_call = {"id": "call-1", "type": "function",
                 "function": {"name": "lookup", "arguments": '{"id":1}'}}
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "tool_calls": [tool_call]},
                {"role": "tool", "tool_call_id": "call-1", "content": "found"}]
    await run(executor, stream, request(session="tools", messages=messages), **scope)
    await run(executor, stream, request(session="different"), **scope)
    assert [call[0] for call in provider.calls] == [uids[0], uids[0], uids[1]]
    assert provider.calls[1][1]["messages"] == messages


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_ineligible_affinity_respects_catalog_binding_pin_and_inflight(repo, stream):
    executor, credentials, provider, ids, uids = engine(repo, affinity=True)
    message = request(session="one")
    executor._deps.affinity.remember(message.raw, "unknown", ids[0])
    tables = catalog(ids)
    tables[ids[0]] = {"other": Model(id="other")}
    await run(executor, stream, message, credential_ids=tuple(ids[:2]), credential_models=tables)
    assert [call[0] for call in provider.calls] == [uids[1]]
    credentials.set_pinned(ids[0])
    await run(executor, stream, message, credential_ids=tuple(ids), credential_models=catalog(ids))
    assert provider.calls[-1][0] == uids[0]
    credentials.set_pinned(None)
    executor._deps.inflight = InflightGuard(lambda: 1)
    executor._deps.inflight.acquire(ids[0])
    try:
        await run(executor, stream, message, credential_ids=tuple(ids),
                  credential_models=catalog(ids))
        assert provider.calls[-1][0] != uids[0]
    finally:
        executor._deps.inflight.release(ids[0])
    await run(executor, stream, message, credential_ids=(ids[2],), credential_models=catalog(ids))
    assert provider.calls[-1][0] == uids[2]


async def test_miora_zero_balance_does_not_wait_for_short_cooldown(repo, monkeypatch):
    executor, credentials, _provider, ids, _uids = engine(repo, count=1)
    credentials.save_quota(ids[0], Quota(remaining=0, total=100, probed_at=1_000))
    executor._deps.short_cooldown_wait_seconds = 60
    from src.engine.scheduler import ErrorOutcome

    credentials.save_error(ids[0], ErrorOutcome(cooling_until=1_100))
    monkeypatch.setattr("src.engine.executor.time.time", lambda: 1_050)
    with executor._request_scope("chat", credential_ids=tuple(ids), credential_models=catalog(ids)):
        target = ModelTarget(model="chat", providers=("miora",))
        assert await executor._wait_short_cooldown(target) is None


def test_miora_unbound_admin_pin_keeps_explicit_selection(repo):
    executor, credentials, _provider, ids, _uids = engine(repo)
    credentials.set_pinned(ids[2])
    target = ModelTarget(model="chat", providers=("miora",))
    assert [executor._select(target, set()) for _ in range(3)] == [ids[2]] * 3


def test_round_robin_shares_small_positive_balance_and_confirmed_free_model():
    from src.engine.scheduler import Candidate

    scheduler = Scheduler()
    pool = [Candidate(credential_id="a", provider="miora", health=0, quota_remaining=0.01),
            Candidate(credential_id="b", provider="miora", health=-1,
                      quota_remaining=0, credit_rate=0),
            Candidate(credential_id="c", provider="miora", health=80, quota_remaining=100)]
    assert [scheduler.select(pool, set(), 100, round_robin=True) for _ in range(6)] == [
        "a", "b", "c", "a", "b", "c",
    ]


@pytest.mark.parametrize("rate", [None, 0, 1])
@pytest.mark.parametrize("stream", [False, True])
async def test_miora_confirmed_exhausted_health_without_remaining_still_blocks_paid_models(
    repo, stream, rate,
):
    executor, credentials, provider, ids, _uids = engine(repo, count=1)
    credentials.save_quota(ids[0], Quota(remaining=None, total=0, probed_at=1_000))
    scope = {"credential_ids": tuple(ids), "credential_models": catalog(ids, rate)}
    if rate == 0:
        await run(executor, stream, **scope)
        assert len(provider.calls) == 1
    else:
        if stream:
            assert b"no_healthy_credential" in await run(executor, stream, **scope)
        else:
            with pytest.raises(NoHealthyCredential):
                await run(executor, stream, **scope)
        assert provider.calls == []


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_free_model_shares_zero_and_positive_balance_accounts(repo, stream):
    executor, credentials, provider, ids, uids = engine(repo, count=2)
    credentials.save_quota(ids[0], Quota(remaining=0, total=0, probed_at=1_000))
    for _ in range(4):
        await run(executor, stream, credential_ids=tuple(ids), credential_models=catalog(ids, 0))
    assert [call[0] for call in provider.calls] == uids * 2


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_failed_affinity_switches_then_remembers_replacement(repo, stream):
    executor, _credentials, provider, ids, uids = engine(repo, affinity=True)
    message = request(session="sticky")
    scope = {"credential_ids": tuple(ids), "credential_models": catalog(ids)}
    await run(executor, stream, message, **scope)
    provider.failures[uids[0]] = MioraHTTPError(402, b'{"message":"credit exhausted"}')
    await run(executor, stream, message, **scope)
    await run(executor, stream, message, **scope)
    assert [call[0] for call in provider.calls] == [uids[0], uids[0], uids[1], uids[1]]


async def test_other_provider_bound_round_robin_keeps_ignoring_affinity(repo):
    credentials, _db = repo
    provider = Accounts()
    ids = sorted(credentials.add(provider="codebuddy", credential_data={"uid": str(index)})
                 for index in range(2))
    executor = Executor(ExecutorDeps(
        credentials=credentials, providers={"codebuddy": provider}, scheduler=Scheduler(),
        default_model="chat", affinity=ConversationAffinity(ttl_seconds=3600),
    ))
    message = parse_chat_request({"model": "chat@codebuddy", "conversation_id": "same",
                                  "messages": [{"role": "user", "content": "hi"}]})
    for _ in range(2):
        await executor.complete(message, credential_ids=tuple(ids), credential_models=catalog(ids))
    assert [call[0] for call in provider.calls] == [
        credentials.credential_data(cid)["uid"] for cid in ids]


@pytest.mark.parametrize("anthropic", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_miora_implicit_real_tool_history_keeps_affinity_after_another_new_conversation(
    repo, stream, anthropic,
):
    executor, _credentials, provider, ids, uids = engine(repo)
    scope = {"credential_ids": tuple(ids), "credential_models": catalog(ids)}
    await run(executor, stream, **scope)
    await run(executor, stream, request(messages=[{"role": "user", "content": "other"}]), **scope)
    if anthropic:
        from src.compat.anthropic.request import parse_messages_request

        follow = parse_messages_request({"model": "chat@miora", "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": [{"type": "tool_use",
                             "id": "call-1", "name": "lookup", "input": {"id": 1}}]},
                         {"role": "user", "content": [{"type": "tool_result",
                             "tool_use_id": "call-1", "content": "found"}]}]})
    else:
        follow = request(messages=[{"role": "user", "content": "hi"},
                                   {"role": "assistant", "tool_calls": [{"id": "call-1",
                                       "type": "function", "function": {
                                           "name": "lookup", "arguments": '{"id":1}'}}]},
                                   {"role": "tool", "tool_call_id": "call-1", "content": "found"}])
    await run(executor, stream, follow, **scope)
    assert [call[0] for call in provider.calls] == [uids[0], uids[1], uids[0]]
    assert provider.calls[-1][1]["messages"][-1] == {
        "role": "tool", "tool_call_id": "call-1", "content": "found"}


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_session_header_keeps_first_turn_affinity_but_another_header_rotates(
    repo, stream,
):
    from src.request_context import set_session_id

    executor, _credentials, provider, ids, uids = engine(repo)
    scope = {"credential_ids": tuple(ids), "credential_models": catalog(ids)}
    try:
        set_session_id("header-one")
        await run(executor, stream, **scope)
        await run(executor, stream, **scope)
        set_session_id("header-two")
        await run(executor, stream, **scope)
    finally:
        set_session_id(None)
    assert [call[0] for call in provider.calls] == [uids[0], uids[0], uids[1]]


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_paid_credit_error_does_not_block_confirmed_free_model_or_clear_paid_cooldown(
    repo, stream,
):
    executor, credentials, provider, ids, uids = engine(repo, count=2)
    tables = {cid: {"paid": Model(id="paid", credit_rate=1),
                    "free": Model(id="free", credit_rate=0)} for cid in ids}
    scope = {"credential_ids": tuple(ids), "credential_models": tables}
    paid = parse_chat_request({"model": "paid@miora", "messages": request().messages})
    free = parse_chat_request({"model": "free@miora", "messages": request().messages})
    provider.failures[uids[0]] = MioraHTTPError(402, b'{"message":"credit exhausted"}')
    await run(executor, stream, paid, **scope)
    failed = next(c for c in credentials.candidates() if c.credential_id == ids[0])
    assert failed.cooling_kind == "credit" and failed.cooling_until is not None
    provider.failures.clear()
    await run(executor, stream, free, credential_ids=(ids[0],), credential_models=tables)
    assert provider.calls[-1][0] == uids[0]
    await run(executor, stream, paid, **scope)
    assert provider.calls[-1][0] == uids[1]
    after = next(c for c in credentials.candidates() if c.credential_id == ids[0])
    assert (after.cooling_until, after.cooling_kind) == (failed.cooling_until, "credit")


@pytest.mark.parametrize("blocked_by", [
    "soft", "model", "disabled", "paused", "inflight", "catalog",
])
async def test_miora_free_credit_exception_never_bypasses_other_guards(repo, blocked_by):
    from src.engine.scheduler import ErrorOutcome, ModelCooldown

    executor, credentials, provider, ids, _uids = engine(repo, count=1)
    credentials.save_quota(ids[0], Quota(remaining=0, total=0, probed_at=1_000))
    credentials.save_error(ids[0], ErrorOutcome(cooling_until=2_000_000_000, cooling_kind="credit"))
    tables = catalog(ids, 0)
    if blocked_by == "soft":
        credentials.save_error(ids[0], ErrorOutcome(
            cooling_until=2_000_000_000, cooling_kind="soft"))
    elif blocked_by == "model":
        credentials.save_error(ids[0], ErrorOutcome(model_cooldowns={
            "chat": ModelCooldown(cooling_until=2_000_000_000)}))
    elif blocked_by == "disabled":
        credentials.mark_relogin_required(ids[0], "expired session")
    elif blocked_by == "paused":
        credentials.set_enabled(ids[0], False)
    elif blocked_by == "inflight":
        executor._deps.inflight = InflightGuard(lambda: 1)
        executor._deps.inflight.acquire(ids[0])
    else:
        tables[ids[0]] = {"other": Model(id="other", credit_rate=0)}
    try:
        with pytest.raises(NoHealthyCredential):
            await executor.complete(request(), credential_ids=tuple(ids), credential_models=tables)
    finally:
        executor._deps.inflight.release(ids[0])
    assert provider.calls == []


def test_miora_local_credit_exception_is_model_specific_and_does_not_bypass_provider_pause(repo):
    from src.engine.scheduler import ErrorOutcome

    executor, _credentials, _provider, ids, _uids = engine(repo, count=1)
    target = ModelTarget(model="chat", providers=("miora",))
    with executor._request_scope("chat", credential_ids=tuple(ids),
                                 credential_models=catalog(ids, 0)) as scope:
        scope.exclude(ids[0], ErrorOutcome(cooling_until=2_000_000_000, cooling_kind="credit"))
        assert executor._select(target, set()) == ids[0]
        assert executor._no_pick_breakdown(target, set()) == {"selectable": 1}
        assert executor._recovery_seconds(target) is None
        scope.exclude_provider("miora", until=2_000_000_000)
        assert executor._select(target, set()) is None
        assert executor._no_pick_breakdown(target, set()) == {"cooling": 1}


@pytest.mark.parametrize("stream", [False, True])
async def test_miora_short_cooldown_retries_only_bound_account_within_three_attempt_budget(
    repo, monkeypatch, stream,
):
    from tests.engine.test_cooldown_wait import _Clock

    executor, _credentials, provider, ids, uids = engine(repo)
    clock = _Clock()
    monkeypatch.setattr("src.engine.executor.time.time", clock.time)
    monkeypatch.setattr("src.engine.executor.asyncio.sleep", clock.sleep)
    clock.on_sleep = lambda: assert_no_inflight(executor)
    executor._deps.short_cooldown_wait_seconds = 60
    provider.failures[uids[0]] = MioraHTTPError(503, b"temporarily unavailable")
    scope = {"credential_ids": (ids[0],), "credential_models": catalog(ids)}
    if stream:
        assert b"no_healthy_credential" in await run(executor, stream, **scope)
    else:
        with pytest.raises(NoHealthyCredential):
            await run(executor, stream, **scope)
    assert executor._deps.scheduler.max_rotate == 3
    assert [call[0] for call in provider.calls] == [uids[0]] * 3
    assert clock.sleeps == [60, 60]
    assert executor._deps.inflight.total() == 0


def assert_no_inflight(executor):
    assert executor._deps.inflight.total() == 0
