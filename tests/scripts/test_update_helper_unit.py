"""systemd 重启助手不能替换应用已挂载的 socket 目录 inode。"""

from configparser import ConfigParser
from pathlib import Path


def test_helper_runtime_directory_survives_stop_and_restart():
    unit = ConfigParser(interpolation=None)
    unit.optionxform = str
    unit.read(Path(__file__).resolve().parents[2] / "scripts" / "system-update-helper.service",
              encoding="utf-8")
    service = unit["Service"]
    assert service["RuntimeDirectory"] == "2apis-update"
    # restart 只涵盖 restart，不涵盖停机后重新启动；yes 两种情况均保留原目录。
    assert service["RuntimeDirectoryPreserve"] == "yes"
    assert service["RuntimeDirectoryMode"] == "0750"
    assert service["Group"] == "2apis-update"
    assert "/run/2apis-update" in service["ReadWritePaths"].split()
