"""Shared fixtures for all tests."""
import pytest
import os
import sys

# Ensure src/ is in path（唯一权威路径引导；pytest.ini pythonpath=src 兜底）
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# 测试库隔离：所有本地测试使用独立测试库，绝不触碰生产库 data/sessions.db
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))
# 测试强制 SQLite 后端：即使 configs/db-backend.json 配了 mysql，测试也绝不连 RDS
# （可用环境变量 DEVOPS_DB_BACKEND 显式覆盖，用于 MySQL 后端专项测试）
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")
# 测试隔离工作区（config.WORKSPACE_DIR 解析优先级 env > 家目录 > 代码目录）
os.environ.setdefault("DEVOPS_WORKSPACE_DIR", os.path.join(ROOT, "data", "test_workspaces"))


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_db():
    """确保测试库存在且已初始化（创建表）。

    会话开始重建干净测试库：删除历史残留的 data/test_sessions.db，
    保证每次 pytest 从确定的空库基线开始（消除跨运行残留脏行/回收 id 冲突，
    是随机顺序确定性的前提之一）。
    """
    import sqlite3 as _sqlite3
    _db_path = os.environ.get("DEVOPS_DB_PATH")
    if _db_path and os.path.exists(_db_path):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_db_path + suffix)
            except OSError:
                pass
    from src.api.db_setup import init_database
    init_database()
    yield
