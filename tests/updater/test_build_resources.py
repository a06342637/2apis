"""更新只能由受限专属BuildKit构建，校验/构建失败也释放其常驻内存。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest

from src.updater.deployment import Deployment
from tests.updater.test_deployment import SHA

LIMITS = {"Memory": 268435456, "MemorySwap": 536870912, "CpuQuota": 25000, "CpuPeriod": 100000}
DESCRIPTION = "Name:          apis-update-2apis\nDriver:        docker-container\nNodes:\n"
BUILDKIT = ("moby/buildkit:buildx-stable-1@sha256:"
            "baf6138eb8c37644800c7c913fba051e840ebb9ba13eaf92d92ef9a1712d0b4c")


def runner(stage, *, exists=True, description=DESCRIPTION, limits=None,
           failure="", stop_failure=False, post_description=None, post_limits=None):
    calls = []
    bootstrapped = False

    def run(argv, **kwargs):
        nonlocal bootstrapped
        calls.append((argv, kwargs))
        if argv[0] == "git":
            if "clone" in argv:
                stage.mkdir()
                (stage / "README.md").write_text("fixed commit")
            return SHA if "rev-parse" in argv else ""
        if argv[:3] == ["docker", "buildx", "inspect"]:
            if "--bootstrap" not in argv:
                if not exists:
                    raise RuntimeError("builder is not registered")
                return description
            bootstrapped = True
            if failure == "bootstrap":
                raise RuntimeError("bootstrap failed")
            return description if post_description is None else post_description
        if argv[:2] == ["docker", "inspect"]:
            value = LIMITS if limits is None else limits
            if bootstrapped and post_limits is not None:
                value = post_limits
            return value if isinstance(value, str) else json.dumps(value)
        if argv[:3] == ["docker", "buildx", "create"] and failure == "create":
            raise RuntimeError("create failed")
        if argv[:3] == ["docker", "buildx", "build"]:
            if failure == "build":
                raise RuntimeError("build failed")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 3600)
        if argv[:3] == ["docker", "buildx", "stop"] and stop_failure:
            raise subprocess.TimeoutExpired(argv, 60)
        return ""

    return run, calls


@pytest.mark.parametrize("exists", [False, True])
def test_dedicated_builder_enforces_limits_before_build_and_stops_without_pruning(
        host, tmp_path, exists):
    stage = tmp_path / "stage"
    run, calls = runner(stage, exists=exists)
    cfg = replace(host, project="tenant-1")
    assert Deployment(cfg, run=run).build("a/b", SHA, stage) == f"2apis-update:{SHA}"
    argv = [call[0] for call in calls]
    builder = "apis-update-tenant-1"
    expected_build = ["docker", "buildx", "build", "--builder", builder,
                      "--load", "--tag", f"2apis-update:{SHA}", str(stage)]
    assert argv[-2] == expected_build and calls[-2][1]["timeout"] == 3600
    assert argv[-1] == ["docker", "buildx", "stop", builder]
    assert calls[-1][1]["timeout"] == 60
    assert not any(call[:2] == ["docker", "build"] or "rm" in call or "prune" in call
                   for call in argv)
    bootstrap = ["docker", "buildx", "inspect", builder, "--bootstrap"]
    inspect = ["docker", "inspect", "--format", "{{json .HostConfig}}",
               f"buildx_buildkit_{builder}0"]
    if exists:
        assert argv.index(inspect) < argv.index(bootstrap) < len(argv) - 2
        assert argv.count(inspect) == 2
        assert not any("create" in call for call in argv)
    else:
        assert argv[5] == ["docker", "buildx", "create", "--name", builder,
                          "--driver", "docker-container", "--driver-opt", f"image={BUILDKIT}",
                          "--driver-opt", "memory=256m", "--driver-opt", "memory-swap=512m",
                          "--driver-opt", "cpu-quota=25000", "--driver-opt", "cpu-period=100000"]
        assert argv.index(bootstrap) < argv.index(inspect) < len(argv) - 2


@pytest.mark.parametrize("description", ["Driver: docker\n", "Name: missing driver\n", ""])
def test_existing_wrong_driver_cannot_bootstrap_or_build(host, tmp_path, description):
    stage = tmp_path / "stage"
    run, calls = runner(stage, description=description)
    with pytest.raises(ValueError, match="docker-container驱动"):
        Deployment(host, run=run).build("a/b", SHA, stage)
    argv = [call[0] for call in calls]
    assert argv[-1] == ["docker", "buildx", "stop", "apis-update-2apis"]
    assert not any("--bootstrap" in call or "build" in call for call in argv)


@pytest.mark.parametrize("key", list(LIMITS))
@pytest.mark.parametrize("value", [0, -1, "256m", None])
def test_existing_resource_mismatch_is_rejected_before_bootstrap(host, tmp_path, key, value):
    limits = dict(LIMITS, **{key: value})
    stage = tmp_path / "stage"
    run, calls = runner(stage, limits=limits)
    with pytest.raises(ValueError, match="内存256MiB"):
        Deployment(host, run=run).build("a/b", SHA, stage)
    assert not any("--bootstrap" in argv or "build" in argv for argv, _kwargs in calls)
    assert calls[-1][0] == ["docker", "buildx", "stop", "apis-update-2apis"]


@pytest.mark.parametrize("limits", ["malformed", "[]", "null", {}])
def test_unreadable_or_missing_resource_metadata_fails_closed(host, tmp_path, limits):
    stage = tmp_path / "stage"
    run, calls = runner(stage, limits=limits)
    with pytest.raises(ValueError, match="资源限制|内存256MiB"):
        Deployment(host, run=run).build("a/b", SHA, stage)
    assert calls[-1][0] == ["docker", "buildx", "stop", "apis-update-2apis"]


@pytest.mark.parametrize("mode", ["driver", "limits"])
@pytest.mark.parametrize("exists", [True, False])
def test_bootstrapped_builder_is_reverified_before_build(host, tmp_path, mode, exists):
    stage = tmp_path / "stage"
    run, calls = runner(stage, exists=exists,
        post_description="Driver: remote\n" if mode == "driver" else DESCRIPTION,
        post_limits={} if mode == "limits" else LIMITS)
    with pytest.raises(ValueError, match="docker-container驱动|内存256MiB"):
        Deployment(host, run=run).build("a/b", SHA, stage)
    assert any("--bootstrap" in argv for argv, _kwargs in calls)
    assert not any("build" in argv for argv, _kwargs in calls)
    assert calls[-1][0] == ["docker", "buildx", "stop", "apis-update-2apis"]


@pytest.mark.parametrize("failure", ["create", "bootstrap", "build", "timeout"])
@pytest.mark.parametrize("stop_failure", [False, True])
def test_failed_build_always_stops_builder_and_retains_primary_failure(
        host, tmp_path, failure, stop_failure):
    stage = tmp_path / "stage"
    run, calls = runner(stage, exists=failure != "create", failure=failure,
                        stop_failure=stop_failure)
    expected = subprocess.TimeoutExpired if failure == "timeout" else RuntimeError
    with pytest.raises(expected) as caught:
        Deployment(host, run=run).build("a/b", SHA, stage)
    assert (caught.value.timeout == 3600 if failure == "timeout"
            else str(caught.value) == f"{failure} failed")
    assert calls[-1][0] == ["docker", "buildx", "stop", "apis-update-2apis"]
    assert not any(argv[:2] == ["docker", "build"] for argv, _kwargs in calls)


def test_successful_build_with_failed_stop_is_not_reported_as_complete(host, tmp_path):
    stage = tmp_path / "stage"
    run, calls = runner(stage, stop_failure=True)
    with pytest.raises(RuntimeError, match="无法停止受限构建器"):
        Deployment(host, run=run).build("a/b", SHA, stage)
    assert calls[-2][0][:3] == ["docker", "buildx", "build"]
    assert calls[-1][0] == ["docker", "buildx", "stop", "apis-update-2apis"]


@pytest.mark.parametrize("existing", ["", "buildx_buildkit_apis-update-tenant.10"])
def test_restart_cleanup_does_not_need_buildx_registration_or_delete_caches(host, existing):
    calls = []
    container = "buildx_buildkit_apis-update-tenant.10"

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return existing if argv[1] == "ps" else ""

    Deployment(replace(host, project="tenant.1"), run=run).stop_interrupted_build()
    assert calls[0] == (["docker", "ps", "--all", "--filter",
                         r"name=^buildx_buildkit_apis\-update\-tenant\.10$",
                         "--format", "{{.Names}}"], {"timeout": 60})
    assert [argv for argv, _kwargs in calls[1:]] == (
        [["docker", "stop", "--time", "10", container]] if existing else [])
    assert all("buildx" not in argv and "rm" not in argv and "prune" not in argv
               for argv, _kwargs in calls)


@pytest.mark.parametrize("failure", ["query", "stop", "name"])
def test_restart_cleanup_failure_never_stops_another_project(host, failure):
    calls = []
    container = "buildx_buildkit_apis-update-2apis0"

    def run(argv, **_kwargs):
        calls.append(argv)
        if (failure == "query" and argv[1] == "ps") or (failure == "stop" and argv[1] == "stop"):
            raise RuntimeError("Docker unavailable")
        return "buildx_buildkit_apis-update-another0" if failure == "name" else container

    with pytest.raises(RuntimeError):
        Deployment(host, run=run).stop_interrupted_build()
    assert len(calls) == (2 if failure == "stop" else 1)
    assert not any("buildx_buildkit_apis-update-another0" in argv for argv in calls)
