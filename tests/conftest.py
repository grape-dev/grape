"""Shared fixtures for all tests."""
import pytest
import os
import sys

# Ensure src/ is in path
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# 测试库隔离：所有本地测试使用独立测试库，绝不触碰生产库 data/sessions.db
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_db():
    """确保测试库存在且已初始化（创建表）。"""
    from src.api.db_setup import init_database
    init_database()
    yield
