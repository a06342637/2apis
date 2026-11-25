import sqlite3
from contextlib import closing

import pytest

from src.updater.deployment import HostConfig


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "deployment"
    root.mkdir()
    data, secrets = root / "data", root / "secrets"
    data.mkdir()
    secrets.mkdir()
    (data / "extra.txt").write_text("persistent")
    with closing(sqlite3.connect(data / "coding2api.sqlite3")) as conn, conn:
        conn.execute("create table accounts (name text)")
    (secrets / "users.txt").write_text("private")
    (root / ".env").write_text("APP_SECRET=original")
    (root / "compose.yml").write_text("original deployment")
    (root / "README.md").write_text("old code")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("old app")
    cfg = HostConfig(source_root=root, state_dir=tmp_path / "state", data_dir=data,
                     secrets_dir=secrets, compose_files=(root / "compose.yml",),
                     env_file=root / ".env", socket_path=tmp_path / "runtime" / "update.sock",
                     secret_file=tmp_path / "secret", current_commit="a" * 40, health_timeout=2)
    cfg.state_dir.mkdir()
    return cfg


@pytest.fixture
def new_source(tmp_path):
    path = tmp_path / "candidate"
    path.mkdir()
    (path / "README.md").write_text("new code")
    return path
