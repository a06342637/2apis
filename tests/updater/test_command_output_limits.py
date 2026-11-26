"""更新命令的输出不在内存无限累积，错误/超时也关闭临时文件并回收进程。"""

import subprocess
import sys

import pytest

from src.updater import deployment


@pytest.fixture
def output_files(monkeypatch):
    opened = []
    original = deployment.tempfile.TemporaryFile

    def temporary_file(**kwargs):
        handle = original(**kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(deployment.tempfile, "TemporaryFile", temporary_file)
    yield opened
    assert opened and all(handle.closed for handle in opened)


def test_small_stdout_remains_usable_and_stderr_is_not_captured(output_files):
    source = ("import os; os.write(1, b'  container-id\\n'); "
              "os.write(2, b'APP_SECRET=private\\n' * 100000)")
    assert deployment.run_command([sys.executable, "-c", source]) == "container-id"
    assert len(output_files) == 1


@pytest.mark.parametrize("size", [1024, 1025, 8 * 1024 * 1024])
def test_stdout_has_explicit_byte_limit(output_files, monkeypatch, size):
    monkeypatch.setattr(deployment, "MAX_COMMAND_STDOUT_BYTES", 1024)
    argv = [sys.executable, "-c", f"import os; os.write(1, b'x' * {size})"]
    if size == 1024:
        assert deployment.run_command(argv) == "x" * size
    else:
        with pytest.raises(RuntimeError, match="大小限制") as caught:
            deployment.run_command(argv)
        assert "xxx" not in str(caught.value)


def test_failure_output_is_never_returned_or_included_in_errors(output_files):
    source = ("import os; os.write(1, b'APP_SECRET=stdout-secret'); "
              "os.write(2, b'TOKEN=stderr-secret'); raise SystemExit(7)")
    with pytest.raises(RuntimeError, match="退出码 7") as caught:
        deployment.run_command([sys.executable, "-c", source])
    assert "stdout-secret" not in str(caught.value)
    assert "stderr-secret" not in str(caught.value)


def test_timeout_terminates_process_and_cleans_output_file(output_files, monkeypatch):
    processes = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(subprocess.TimeoutExpired):
        deployment.run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                               timeout=0.25)
    assert len(processes) == 1 and processes[0].poll() is not None


def test_invalid_utf8_stdout_decodes_without_unbounded_buffer(output_files):
    assert deployment.run_command([sys.executable, "-c", "import os; os.write(1, b'\\xff')"]) == "�"
