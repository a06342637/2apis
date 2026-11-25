"""媒体目录不走聊天探活，手工探活也不产生生成请求或假 failed。"""

from __future__ import annotations

import pytest

from src.db.repo import ModelProbeRepository
from src.provider.base import Model
from src.tasks.model_probe import NO_CHAT_PROBE, ModelProbeTask
from tests.tasks.test_model_probe import FakeProvider, _add, _seed_blocked


@pytest.mark.parametrize("endpoints", [("/v1/images/generations",), ("/v1/videos",), ()])
async def test_periodic_media_model_probe_never_calls_stream_or_generation(repo, endpoints):
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="media")
    probes = ModelProbeRepository(db)
    probes.record(provider="codebuddy", model="media", state="failed")
    provider = FakeProvider()
    cache = {"codebuddy": {"media": Model(id="media", gateway_endpoints=endpoints)}}
    task = ModelProbeTask(credentials, {"codebuddy": provider}, probes, model_list_cache=cache)
    report = await task.run_once()
    assert report.attempted == 0 and report.skipped == 1 and report.failed == 0
    assert provider.calls == []
    assert probes.get("codebuddy", "media")["failures"] == 1
    assert credentials.blocked_cooldowns()


async def test_manual_media_probe_is_inconclusive_and_cannot_write_failed(repo):
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="image")
    probes = ModelProbeRepository(db)
    probes.record(provider="codebuddy", model="image", state="failed")
    provider = FakeProvider()
    cache = {"codebuddy": {"raw": Model(id="raw", gateway_endpoints=("/v1/images/generations",))}}
    task = ModelProbeTask(
        credentials,
        {"codebuddy": provider},
        probes,
        model_list_cache=cache,
        upstream_model=lambda pid, name: "raw",
    )
    assert await task.probe_now("codebuddy", "image") == ("inconclusive", NO_CHAT_PROBE)
    row = probes.get("codebuddy", "image")
    assert row["state"] == "inconclusive" and row["failures"] == 0
    assert row["last_error"] == NO_CHAT_PROBE
    assert provider.calls == [] and credentials.blocked_cooldowns()
    # 使用共享目录的最新引用：后续明确恢复聊天接口才允许真正推理探活。
    cache["codebuddy"]["raw"] = Model(id="raw", gateway_endpoints=("/v1/chat/completions",))
    assert await task.probe_now("codebuddy", "image") == ("ok", "")
    assert provider.calls[0][0] == "raw"


@pytest.mark.parametrize(
    "entry",
    [None, Model(id="legacy"), Model(id="legacy", gateway_endpoints=("/v1/chat/completions",))],
)
async def test_legacy_or_chat_capable_model_metadata_retains_chat_probe(repo, entry):
    credentials, db = repo
    _add(credentials)
    provider = FakeProvider()
    cache = {"codebuddy": {"legacy": entry}} if entry is not None else {}
    task = ModelProbeTask(
        credentials, {"codebuddy": provider}, ModelProbeRepository(db), model_list_cache=cache
    )
    assert await task.probe_now("codebuddy", "legacy") == ("ok", "")
    assert len(provider.calls) == 1


@pytest.mark.parametrize("state", ["paused", "disabled"])
async def test_actual_model_inference_respects_soft_pause_and_hard_disable(repo, state):
    credentials, db = repo
    cid = _add(credentials)
    _seed_blocked(credentials, credential_id=cid, model="chat")
    if state == "paused":
        credentials.set_enabled(cid, False)
    else:
        credentials.mark_relogin_required(cid, "expired")
    provider = FakeProvider()
    task = ModelProbeTask(credentials, {"codebuddy": provider}, ModelProbeRepository(db))
    assert (await task.run_once()).attempted == 0
    assert (await task.probe_now("codebuddy", "chat"))[0] == "inconclusive"
    assert provider.calls == []
