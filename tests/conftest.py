"""Shared fixtures for all tests."""
from __future__ import annotations

import json
import pytest
import os
import sys

# Ensure src/ is in path（唯一权威路径引导；pytest.ini pythonpath=src 兜底）
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# 测试库隔离：所有本地测试使用独立测试库，绝不触碰生产库 data/sessions.db
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")
# 测试强制 SQLite 后端：即使 configs/db-backend.json 或 .env 配了 mysql，测试也绝不连 RDS
# 使用 os.environ[...] = ... 强制覆盖（不使用 setdefault，防止 .env 已设置 mysql 时失效）
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
# 清除可能被 .env 设置的 MySQL 连接参数，防止 mysql_compat 误连生产库
for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_PORT", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME"):
    os.environ.pop(_k, None)
# 测试隔离工作区（config.WORKSPACE_DIR 解析优先级 env > 家目录 > 代码目录）
os.environ["DEVOPS_WORKSPACE_DIR"] = os.path.join(ROOT, "data", "test_workspaces")

# tests/playwright/*.py 是手工 E2E 脚本，不是 pytest 用例。
# 这些脚本在 import 期就启动浏览器并访问 localhost:3000，pytest 收集会直接执行。
# 手工运行方式：python tests/playwright/test_r12_fullscreen_zindex.py
collect_ignore_glob = ["playwright/*.py"]


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


@pytest.fixture(scope="session", autouse=True)
def _ensure_help_page_generated():
    """测试前先把 help/ markdown 渲染为 src/api/static/help.html。

    help.html 已是生成物（.gitignore）；全新检查/克隆环境未启动服务时文件可能缺失，
    测试前调用生成器保证 test_help_directory.py 等对 help.html 的断言有确定输入。
    """
    from src.help_page_gen import generate as _gen_help_page

    _gen_help_page()
    yield


@pytest.fixture(scope="function", autouse=True)
def _reset_db_path():
    """每个测试前重置 DEVOPS_DB_PATH。

    防止其他测试模块 import 时改 env 导致后续测试拿到错误路径。
    注意：不重绑 session_manager.db 连接——73 个模块在 module level 导入了 db，
    重绑 session_manager.db 不会更新它们的引用，反而导致写读不一致。
    依赖 _ensure_test_db session fixture 在 session 开始时用正确 env 初始化表。
    """
    _test_db = os.path.join(ROOT, "data", "test_sessions.db")
    os.environ["DEVOPS_DB_PATH"] = _test_db
    yield


@pytest.fixture
def a2a_event_repo():
    """A2A 事件测试仓库隔离助手（Issue #1667 / #1806 共用）。"""
    from src.session_manager import db, db_lock

    class _A2AEventRepo:
        def clean(self, repo_key):
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM events WHERE repo_key=?", (repo_key,))
                for table in ("issue", "managed_repos"):
                    try:
                        c.execute(f"DELETE FROM {table} WHERE repo_key=?", (repo_key,))
                    except Exception:
                        pass
                db.commit()

        def seed(self, repo_key, owner, name):
            with db_lock:
                c = db.cursor()
                c.execute(
                    "INSERT OR IGNORE INTO managed_repos "
                    "(repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                    (repo_key, owner, name, f"{owner}/{name}"))
                db.commit()

        def count_events(self, repo_key, event_type="a2a_event"):
            with db_lock:
                return db.cursor().execute(
                    "SELECT COUNT(*) FROM events WHERE event_type=? AND repo_key=?",
                    (event_type, repo_key)).fetchone()[0]

        def event(self, seq):
            with db_lock:
                row = db.cursor().execute(
                    "SELECT seq, status, agent_type, issue_id, payload FROM events WHERE seq=?",
                    (seq,)).fetchone()
            if not row:
                return None
            try:
                payload = json.loads(row[4] or "{}")
            except Exception:
                payload = {}
            return {"seq": row[0], "status": row[1], "agent": row[2],
                    "issue_id": row[3], "payload": payload}

    return _A2AEventRepo()
