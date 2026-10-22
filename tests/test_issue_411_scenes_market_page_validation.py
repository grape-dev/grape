"""Issue #411（AT-VAL-06）：GET /api/scenes/market 非正分页参数返回 500 并回显数据库错误原文。

缺陷：list_scenes_market 端点 page/page_size 无 ge=1 约束，负数/零值经 list_scene_market
进入 SQL `LIMIT {page_size} OFFSET {(page-1)*page_size}`（src/user/db.py:3655）触发 SQLite
语法错误 → 500 + 数据库错误原文回显。

修复期望（Acceptance Criteria）：
- page/page_size 负值、零、非整数 → 422，响应体无 SQL/表结构信息；
- 合法分页请求行为不变（200 回归）；
- limit>0 跳过分页的精选场景逻辑不受影响；
- 全局异常处理对任意未捕获 DB 异常返回脱敏 message（不含 str(e)）。

运行：python -m pytest tests/test_issue_411_scenes_market_page_validation.py -v
（沿用 tests/test_scene_skill_market.py 的 TestClient + 独立测试库模式，不发真实网络请求）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient  # noqa: E402
from src.api.main import app  # noqa: E402
from src.auth import grape_account_db  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402

client = TestClient(app)

_uid = None
_headers = {}


def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def setup_module():
    global _uid, _headers
    _uid = grape_account_db.create_grape_account("test_411_market", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test_411_market")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}


def teardown_module():
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM scene_likes WHERE user_id=?", (_uid["id"],))
            c.execute("DELETE FROM skill_likes WHERE user_id=?", (_uid["id"],))
            db.commit()


# ===================== AT-VAL-06 分页参数校验 =====================

class TestSceneMarketPaginationValidation:
    """GET /api/scenes/market 分页参数越界 → 422（不再 500 回显 SQL）"""

    def _get(self, params: dict):
        return client.get("/api/scenes/market", params=params, headers=_headers)

    def _assert_rejected(self, params: dict):
        r = self._get(params)
        assert r.status_code == 422, (
            f"params={params} got {r.status_code}: {r.text[:300]} (期望 422)")
        # 响应体不得含 SQL / 表结构 / 错误原文
        assert "SQL" not in r.text, f"SQL detail leaked: {r.text[:300]}"
        assert "OFFSET" not in r.text.upper() or "LIMIT" not in r.text.upper()

    def test_page_size_negative_rejected_422(self):
        """AT-VAL-06: page_size=-1 → 422（修复前 500 回显 'LIMIT -1' 语法错误）"""
        self._assert_rejected({"page": 1, "page_size": -1})

    def test_page_size_zero_rejected_422(self):
        """AT-VAL-06: page_size=0 → 422"""
        self._assert_rejected({"page": 1, "page_size": 0})

    def test_page_zero_rejected_422(self):
        """AT-VAL-06: page=0 → 422（修复前 500 回显 'OFFSET must not be negative'）"""
        self._assert_rejected({"page": 0, "page_size": 20})

    def test_page_negative_rejected_422(self):
        """AT-VAL-06: page=-1 → 422"""
        self._assert_rejected({"page": -1, "page_size": 20})

    def test_non_integer_page_rejected_422(self):
        """AT-VAL-06: page=abc（非整数）→ 422"""
        self._assert_rejected({"page": "abc", "page_size": 20})

    def test_non_integer_page_size_rejected_422(self):
        """AT-VAL-06: page_size=abc（非整数）→ 422"""
        self._assert_rejected({"page": 1, "page_size": "abc"})

    def test_valid_pagination_still_works(self):
        """合法分页回归：page=1&page_size=20 → 200，响应结构不变"""
        r = self._get({"page": 1, "page_size": 20})
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:300]}"
        data = r.json()
        assert "scenes" in data and "total" in data
        assert data["page"] == 1 and data["page_size"] == 20
        # 响应体同样要求不泄露任何 DB 原文
        assert "OFFSET" not in r.text.upper() or "LIMIT" not in r.text.upper()

    def test_featured_limit_skips_paging_unaffected(self):
        """limit>0 跳过分页的精选场景逻辑不受影响（featured_only=1&limit=5 → 200）"""
        r = self._get({"featured_only": 1, "limit": 5})
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:300]}"
        data = r.json()
        assert "scenes" in data
        assert len(data["scenes"]) <= 5

    def test_paging_with_page_size_1_returns_at_most_one(self):
        """分页语义回归：page_size=1 最多返回 1 条"""
        r = self._get({"page": 1, "page_size": 1})
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:300]}"
        data = r.json()
        assert len(data["scenes"]) <= 1


class TestSceneMarketGlobalExceptionSanitized:
    """#411 第三层：全局异常处理器不回显异常原文（DB 错误原文不进 message）"""

    def test_global_handler_returns_generic_message(self):
        import asyncio
        from src.api.main import global_exception_handler
        from starlette.requests import Request

        scope = {
            "type": "http", "headers": [], "request_id": "req-test-411",
            "method": "GET", "path": "/api/scenes/market", "query_string": b"",
            "scheme": "http", "server": ("testserver", 80), "root_path": "",
        }
        req = Request(scope=scope)
        resp = asyncio.run(global_exception_handler(
            req, RuntimeError('near "LIMIT": syntax error ... OFFSET must not be negative ... scene_market')))
        assert resp.status_code == 500
        body = resp.body.decode() if isinstance(resp.body, bytes) else str(resp.body)
        assert "LIMIT" not in body, f"SQL detail leaked: {body}"
        assert "OFFSET" not in body, f"SQL detail leaked: {body}"
        assert "scene_market" not in body, f"table name leaked: {body}"
        assert "服务器内部错误" in body


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)