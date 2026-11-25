"""指定账号集合的最少在途与轮询策略，未指定集合仍沿用原选号。"""

import pytest

from src.compat.openai.request import parse_chat_request
from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential, ProviderAccessFrozen
from src.engine.model_resolver import ModelTarget
from src.engine.scheduler import Candidate, Scheduler
from src.provider.base import Event, EventKind, Model
from src.provider.miora.client import MioraHTTPError


def account(cid, **fields):
    return Candidate(credential_id=cid, provider="miora", **fields)


def test_bound_round_robin_shares_sequential_requests_and_honors_load():
    scheduler = Scheduler()
    pool = [
        account("a", health=80, quota_remaining=10),
        account("b", health=100, quota_remaining=1000),
    ]
    assert [scheduler.select(pool, set(), 10, round_robin=True) for _ in range(4)] == [
        "a",
        "b",
        "a",
        "b",
    ]
    loaded = [account("a", health=80, inflight=1), account("b", health=80)]
    assert scheduler.select(loaded, set(), 10, round_robin=True) == "b"
    assert scheduler.select(pool, {"a", "b"}, 10, round_robin=True) is None
    assert scheduler.select(pool, set(), 10) == "b"


def test_bound_round_robin_does_not_promote_unknown_exhausted_or_paused_accounts():
    scheduler = Scheduler()
    pool = [
        account("a", health=-1),
        account("b", health=None),
        account("c", health=10, inflight=5),
        account("d", health=100, enabled=False),
    ]
    assert scheduler.select(pool, set(), 10, round_robin=True) == "c"
    exhausted = [
        account("a", health=100, quota_remaining=0),
        account("b", health=0),
        account("c", health=None),
        account("d", health=20, inflight=1),
    ]
    assert scheduler.select(exhausted, set(), 10, round_robin=True) == "d"


class ScopedProvider:
    def __init__(self):
        self.calls = []

    async def stream_chat(self, data, payload, model):
        self.calls.append((data["uid"], model))
        if data["uid"] == "selected":
            raise MioraHTTPError(503, b"temporary")
        yield Event(kind=EventKind.CONTENT, content="foreign account escaped")
        yield Event(kind=EventKind.FINISH, finish_reason="stop")


@pytest.mark.parametrize("stream", [False, True])
async def test_bound_fallback_and_foreign_affinity_never_escape_selected_ids(repo, stream):
    credentials, _db = repo
    one = credentials.add(provider="miora", credential_data={"uid": "selected"})
    two = credentials.add(provider="miora", credential_data={"uid": "foreign"})
    provider = ScopedProvider()

    class ForeignAffinity:
        def pin_for(self, _request, _username):
            return two

    executor = Executor(
        ExecutorDeps(
            credentials=credentials,
            providers={"miora": provider},
            scheduler=Scheduler(),
            default_model="first",
            fallback_groups={"fast": ["first", "second"]},
            affinity=ForeignAffinity(),
        )
    )
    request = parse_chat_request(
        {"model": "first", "messages": [{"role": "user", "content": "hi"}]}
    )
    catalog = {one: {"first": Model(id="first", name="First")}}
    if stream:
        frames = [
            frame
            async for frame in executor.stream_guarded(
                request, credential_ids=(one,), credential_models=catalog
            )
        ]
        assert b"no_healthy_credential" in b"".join(frames)
    else:
        with pytest.raises(NoHealthyCredential):
            await executor.complete(request, credential_ids=(one,), credential_models=catalog)
    assert provider.calls == [("selected", "first")]
    assert {
        row.credential_id
        for row in executor._new_scope(credential_ids=(one,), credential_models=catalog).rows
    } == {one}
    assert {
        row.credential_id for row in executor._new_scope("first", credential_ids=(one,)).rows
    } == {one}


def test_bound_model_matching_and_preflight_fail_closed(repo):
    credentials, _db = repo
    one = credentials.add(provider="miora", credential_data={"uid": "selected"})
    two = credentials.add(provider="miora", credential_data={"uid": "foreign"})
    executor = Executor(
        ExecutorDeps(
            credentials=credentials,
            providers={"miora": ScopedProvider()},
            scheduler=Scheduler(),
            default_model="first",
        )
    )
    catalog = {
        one: {"raw": Model(id="raw", name="Chosen model")},
        two: {"different": Model(id="different", name="Different")},
    }
    scope = executor._new_scope(
        "chosen-model", credential_ids=(one, two), credential_models=catalog
    )
    assert [row.credential_id for row in scope.rows] == [one]
    request = parse_chat_request(
        {"model": "absent", "messages": [{"role": "user", "content": "hi"}]}
    )
    with pytest.raises(NoHealthyCredential):
        executor.preflight(request, credential_ids=(one,), credential_models=catalog)
    assert (
        executor._new_scope(
            "chosen-model",
            credential_ids=(one,),
            credential_models=catalog,
            credential_model_policy=("different", ""),
        ).rows
        == ()
    )
    assert (
        executor._new_scope(
            "chosen-model",
            credential_ids=(one,),
            credential_models=catalog,
            credential_model_policy=("", "different"),
        ).rows
        == ()
    )


def test_exact_account_model_id_wins_and_ambiguous_names_do_not_guess(repo):
    credentials, _db = repo
    one = credentials.add(provider="miora", credential_data={"uid": "one"})
    executor = Executor(
        ExecutorDeps(
            credentials=credentials,
            providers={"miora": ScopedProvider()},
            scheduler=Scheduler(),
            default_model="chosen-model",
        )
    )
    catalog = {
        one: {
            "free": Model(id="free", name="Chosen model"),
            "standard": Model(id="standard", name="Chosen model"),
        }
    }
    assert (
        executor._new_scope("chosen-model", credential_ids=(one,), credential_models=catalog).rows
        == ()
    )
    executor._deps.upstream_model_name = lambda provider, model: "standard"
    assert executor._new_scope(
        "chosen-model", credential_ids=(one,), credential_models=catalog
    ).upstream_models == {one: "standard"}


@pytest.mark.parametrize("stream", [False, True])
async def test_bound_automatic_continuation_stays_on_selected_account(repo, stream):
    credentials, _db = repo
    one = credentials.add(provider="miora", credential_data={"uid": "one"})
    credentials.add(provider="miora", credential_data={"uid": "foreign"})

    class Continues:
        def __init__(self):
            self.calls = []

        async def stream_chat(self, data, payload, model):
            self.calls.append((data["uid"], model))
            yield Event(kind=EventKind.CONTENT, content="reply")
            yield Event(
                kind=EventKind.FINISH, finish_reason="length" if len(self.calls) == 1 else "stop"
            )

    provider = Continues()
    executor = Executor(
        ExecutorDeps(
            credentials=credentials,
            providers={"miora": provider},
            scheduler=Scheduler(),
            default_model="chosen-model",
            max_auto_continues=1,
        )
    )
    request = parse_chat_request(
        {"model": "chosen-model@miora", "messages": [{"role": "user", "content": "hi"}]}
    )
    scope = {
        "credential_ids": (one,),
        "credential_models": {one: {"raw": Model(id="raw", name="Chosen model")}},
    }
    if stream:
        assert [frame async for frame in executor.stream_guarded(request, **scope)]
    else:
        assert (await executor.complete(request, **scope))["choices"]
    assert provider.calls == [("one", "raw"), ("one", "raw")]


async def test_bound_catalog_is_not_overruled_by_foreign_provider_catalog_or_freeze(repo):
    credentials, _db = repo
    one = credentials.add(provider="miora", credential_data={"uid": "one"})

    class Good:
        async def stream_chat(self, data, payload, model):
            assert data["uid"] == "one" and model == "raw"
            yield Event(kind=EventKind.CONTENT, content="allowed")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    class Freezes:
        def is_frozen(self, *, subject, provider, threshold):
            return provider == "codearts"

    executor = Executor(
        ExecutorDeps(
            credentials=credentials,
            providers={"miora": Good(), "codearts": ScopedProvider()},
            scheduler=Scheduler(),
            default_model="chosen-model",
            model_aliases={"codearts": {"chosen-model": "foreign"}},
            access_freezes=Freezes(),
            access_freeze_threshold=1,
        )
    )
    request = parse_chat_request(
        {"model": "chosen-model", "messages": [{"role": "user", "content": "hi"}]}
    )
    scope = {
        "credential_ids": (one,),
        "credential_models": {one: {"raw": Model(id="raw", name="Chosen model")}},
    }
    assert executor.preflight(request, "miora", **scope).providers == ("miora",)
    assert (await executor.complete(request, provider_binding="miora", **scope))["choices"]
    assert (await executor.complete(request, **scope))["choices"]
    executor._ensure_access_allowed(
        (ModelTarget(model="chosen-model", providers=("codearts",)),), "root", **scope
    )
    executor._deps.providers.clear()
    with pytest.raises(NoHealthyCredential):
        executor.preflight(request, **scope)


@pytest.mark.parametrize("policy", [("first", ""), ("", "first")])
@pytest.mark.parametrize("stream", [False, True])
async def test_unauthorized_fallback_cannot_bypass_bound_caller_freeze(repo, policy, stream):
    credentials, _db = repo
    one = credentials.add(provider="miora", credential_data={"uid": "one"})
    two = credentials.add(provider="codearts", credential_data={"uid": "two"})

    class Freezes:
        def is_frozen(self, *, subject, provider, threshold):
            return provider == "miora"

    executor = Executor(
        ExecutorDeps(
            credentials=credentials,
            providers={"miora": object(), "codearts": object()},
            scheduler=Scheduler(),
            default_model="first",
            fallback_groups={"fast": ["first", "second"]},
            access_freezes=Freezes(),
            access_freeze_threshold=1,
        )
    )
    request = parse_chat_request(
        {"model": "first", "messages": [{"role": "user", "content": "hi"}]}
    )
    scope = {
        "credential_ids": (one, two),
        "credential_model_policy": policy,
        "credential_models": {
            one: {"first": Model(id="first", name="First")},
            two: {"second": Model(id="second", name="Second")},
        },
    }
    with pytest.raises(ProviderAccessFrozen):
        if stream:
            executor.preflight(request, **scope)
        else:
            await executor.complete(request, **scope)
