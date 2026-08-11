"""数据库后端开关（configs/db-backend.json）逻辑测试。

覆盖 src/mysql_compat.py 的 load_db_backend_config / get_db_backend / mysql_config 纯逻辑，
不连接真实数据库（用 tmp_path 伪造配置文件）。运行：pytest tests/test_db_backend_switch.py
"""
import json
import os

import pytest

import src.mysql_compat as mc


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离环境变量与配置文件路径。"""
    monkeypatch.delenv("DEVOPS_DB_BACKEND", raising=False)
    monkeypatch.delenv("DEVOPS_DB_URL", raising=False)
    for k in ("DEVOPS_DB_HOST", "DEVOPS_DB_PORT", "DEVOPS_DB_USER",
              "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME"):
        monkeypatch.delenv(k, raising=False)


def _write_cfg(tmp_path, backend="sqlite", mysql=None):
    cfg = {"backend": backend, "mysql": mysql or {
        "host": "10.0.0.1", "port": 3307, "user": "u",
        "password": "p", "database": "db"}}
    p = tmp_path / "db-backend.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


@pytest.mark.parametrize("backend,expect", [
    ("sqlite", "sqlite"),
    ("mysql", "mysql"),
    ("MySQL", "mysql"),   # 大小写不敏感
    ("rds", "rds"),       # 未知值原样返回（session_manager 只认 mysql）
])
def test_load_db_backend_config_reads_backend(tmp_path, backend, expect):
    p = _write_cfg(tmp_path, backend=backend)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
    try:
        assert mc.load_db_backend_config()["backend"] == expect
    finally:
        monkeypatch.undo()


def test_load_db_backend_config_missing_file(tmp_path):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(tmp_path / "nope.json"))
    try:
        assert mc.load_db_backend_config() == {"backend": "sqlite", "mysql": {}}
    finally:
        monkeypatch.undo()


def test_get_db_backend_default_sqlite(tmp_path):
    """无环境变量、配置文件缺失 → 默认 sqlite。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(tmp_path / "nope.json"))
    try:
        assert mc.get_db_backend() == "sqlite"
    finally:
        monkeypatch.undo()


def test_get_db_backend_env_overrides_config(tmp_path):
    """环境变量 DEVOPS_DB_BACKEND 优先级高于配置文件。"""
    p = _write_cfg(tmp_path, backend="mysql")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    try:
        assert mc.get_db_backend() == "sqlite"
    finally:
        monkeypatch.undo()


def test_get_db_backend_config_wins_when_no_env(tmp_path):
    p = _write_cfg(tmp_path, backend="mysql")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
    try:
        assert mc.get_db_backend() == "mysql"
    finally:
        monkeypatch.undo()


def test_mysql_config_reads_file(tmp_path):
    """mysql_config() 从配置文件的 mysql 段读取连接参数。"""
    p = _write_cfg(tmp_path, backend="mysql")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
    try:
        c = mc.mysql_config()
        assert c["host"] == "10.0.0.1"
        assert c["port"] == 3307
        assert c["user"] == "u"
        assert c["password"] == "p"
        assert c["database"] == "db"
    finally:
        monkeypatch.undo()


def test_mysql_config_env_overrides_file(tmp_path):
    p = _write_cfg(tmp_path, backend="mysql")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
    monkeypatch.setenv("DEVOPS_DB_HOST", "10.9.9.9")
    monkeypatch.setenv("DEVOPS_DB_PASSWORD", "secret")
    try:
        c = mc.mysql_config()
        assert c["host"] == "10.9.9.9"
        assert c["password"] == "secret"
        assert c["database"] == "db"  # 未覆盖项仍取配置文件
    finally:
        monkeypatch.undo()


def test_mysql_config_url_shortcut(tmp_path):
    """DEVOPS_DB_URL 优先于配置文件。"""
    p = _write_cfg(tmp_path, backend="mysql")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mc, "_DB_BACKEND_CONFIG_PATH", str(p))
    monkeypatch.setenv("DEVOPS_DB_URL", "mysql://u2:p2@10.1.1.1:3308/db2")
    try:
        c = mc.mysql_config()
        assert c["host"] == "10.1.1.1"
        assert c["port"] == 3308
        assert c["user"] == "u2"
        assert c["password"] == "p2"
        assert c["database"] == "db2"
    finally:
        monkeypatch.undo()


def test_example_template_default_sqlite():
    """入库的 example 模板必须默认 sqlite（出厂开关）。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(mc.__file__))),
                     "configs", "db-backend.example.json")
    assert os.path.exists(p), f"missing {p}"
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["backend"] == "sqlite"
    assert "mysql" in cfg
