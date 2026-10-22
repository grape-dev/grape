"""Issue #412 回归测试：scenes/skills 与 runtime/runtimes/{id}/tasks 分页参数校验。

缺陷（与 #411 同源）：
  AT-VAL-09（QA 清单）: GET /api/scenes/skills/{agent_type}?page_size=-1 / ?page=0
                        与 GET /api/runtime/runtimes/{id}/tasks?limit=-1
                        → 500 + 回显数据库错误原文（SQL/表结构泄露）
  期望：非法分页参数 → 422（FastAPI Query(ge=1) 参数层拦截），响应体无 SQL 泄露；
        合法分页行为不变；数据库错误原文仅入服务端日志（全局 handler 脱敏）。

运行：python -m pytest tests/test_issue_412_pagination.py -v
（沿用 test_issue_395_bugfixes.py 的 TestClient + 独立测试库模式，不发真实网络请求）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock

client = TestClient(app)
# 异常脱敏测试用：不让 TestClient 把服务端异常重新抛出，走真实 500 响应路径
client_no_raise = TestClient(app, raise_server_exceptions=False)

_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    from src.auth.password import hash_password
    from src.api.jwt_utils import create_access_token
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, token_version FROM grape_accounts WHERE username='test_412'")
        row = c.fetchone()
        if row:
            _uid = {"id": row[0]}
            ver = row[1] or 0
        else:
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                ("test_412", None, hash_password("test123"), "test_412"))
            db.commit()
            _uid = {"id": c.lastrowid}
            ver = 0
    _headers = {"Authorization": "Bearer " + create_access_token(_uid["id"], "test_412", ver)}


def teardown_module():
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE id=?", (_uid["id"],))
            db.commit()


def _ensure_skill(agent_type="vod", count=2):
    """预置 official skill 数据，验证合法分页回归。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name LIKE 'test_412_%'")
        for i in range(count):
            c.execute(
                "INSERT INTO skill (skill_name, display_name, description, agent_type, scope, owner_id, is_active, is_featured) "
                "VALUES (?,?,?,?,?,0,1,0)",
                (f"test_412_skill_{i}", f"Test412 Skill {i}", "issue412 test", agent_type, "official"))
        db.commit()


# ===================== scenes/skills 分页校验（AT-VAL-09 前半） =====================

class TestScenesSkillsPagination422:
    """GET /api/scenes/skills/{agent_type} 越界分页参数 → 422（不再 500 回显 SQL）"""

    def _call(self, params: dict):
        return client.get("/api/scenes/skills/vod", params=params, headers=_headers)

    def test_page_size_negative_rejected_422(self):
        r = self._call({"page_size": -1})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"
        assert "SQL" not in r.text and "1064" not in r.text and "sqlite" not in r.text.lower()

    def test_page_zero_rejected_422(self):
        r = self._call({"page": 0})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"
        assert "SQL" not in r.text and "1064" not in r.text

    def test_page_size_zero_rejected_422(self):
        r = self._call({"page_size": 0})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"

    def test_page_size_over_limit_rejected_422(self):
        r = self._call({"page_size": 201})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"

    def test_valid_pagination_regression(self):
        """合法分页行为不变：默认 / page=1&page_size=1 均返回 200 且结构完整。"""
        _ensure_skill("vod", 2)
        r = self._call({})
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:300]}"
        body = r.json()
        assert "skills" in body and "total" in body
        assert body["total"] >= 2

        r = self._call({"page": 1, "page_size": 1})
        assert r.status_code == 200
        assert len(r.json()["skills"]) == 1

        r = self._call({"page": 2, "page_size": 1})
        assert r.status_code == 200
        assert len(r.json()["skills"]) <= 1


# ===================== runtime/runtimes/{id}/tasks 分页校验（AT-VAL-09 后半） =====================

class TestRuntimeTasksLimit422:
    """GET /api/runtime/runtimes/{id}/tasks 越界 limit → 422（不再 500 回显 SQL）"""

    def _call(self, params: dict):
        return client.get("/api/runtime/runtimes/1/tasks", params=params, headers=_headers)

    def test_limit_negative_rejected_422(self):
        r = self._call({"limit": -1})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"
        assert "SQL" not in r.text and "1064" not in r.text and "sqlite" not in r.text.lower()

    def test_limit_zero_rejected_422(self):
        """limit 无「0=全部」语义（SQL LIMIT 0 即空集），与其他列表端点统一 ge=1。"""
        r = self._call({"limit": 0})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"

    def test_limit_over_limit_rejected_422(self):
        r = self._call({"limit": 501})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"

    def test_valid_limit_regression(self):
        r = self._call({"limit": 10})
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:300]}"
        assert "tasks" in r.json()

        r = self._call({})
        assert r.status_code == 200
        assert "tasks" in r.json()


# ===================== 全局异常脱敏（数据库错误原文不回显） =====================

class TestGlobalExceptionSanitization:
    """数据库异常 → 500 通用文案，原文仅入服务端日志（#380/#395 落地，本 Issue 锁定行为）。"""

    def test_db_exception_sanitized(self):
        """路由层抛出数据库异常（含 SQL 片段/表名）→ 500 通用文案，不回显原文。"""
        from unittest.mock import patch, MagicMock
        import sqlite3
        import src.session_manager as sm

        _mock_db = MagicMock()
        _mock_db.cursor.side_effect = sqlite3.OperationalError(
            'unrecognized token: ":" SELECT secret_col FROM secret_table WHERE x=?')

        with patch.object(sm, "db", _mock_db):
            r = client_no_raise.get("/api/scenes/skills/vod", headers=_headers,
                                    params={"page": 1, "page_size": 5})
        assert r.status_code == 500, f"Got {r.status_code}: {r.text[:300]}"
        assert "secret_table" not in r.text and "secret_col" not in r.text
        assert "SQL" not in r.text
        assert "服务器内部错误" in r.text
