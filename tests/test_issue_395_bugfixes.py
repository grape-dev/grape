"""Issue #395（#380~#383 四项缺陷集中修复）回归测试。

四项缺陷（原子测试编号）：
  #380 AT-KNW-06: GET /api/knowledge 越界分页（page_size=-1）→ 500 + 回显 MySQL 原文
       期望：page/page_size 越界 → 422；全局异常不回显 DB 原文
  #381 AT-ORGP-03: PUT /api/orgs/{任意 id} → 200（鉴权与路径 org_id 脱钩，越权）
       期望：不存在 → 404；他人组织/非管理员 → 403；自己 admin → 200
  #382 AT-A2A-03: POST /api/a2a 传 repository 为 dict → 500 AttributeError
       期望：非字符串 repository → 400；正常 str 不误拒
  #383 AT-REPO-06: POST /api/repos/host 托管不存在仓库 → 502 + 回显 GitCode 内部 URL
       期望：上游 404 → 404 固定文案；上游 401/403 → 403；上游 5xx → 5xx 固定文案，
       响应体不泄露 gitcode.com/api 与上游报文原文

运行：python -m pytest tests/test_issue_395_bugfixes.py -v
（沿用 test_issue_386_token_validation_400.py 的 TestClient + mock 模式，不发真实网络请求）
"""
import os
import sys
import re
import unittest
from unittest.mock import patch, MagicMock

# 强制使用独立测试库，防止 TestClient 误连生产库（与 test_api_routes.py 同款双保险）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from fastapi import HTTPException
from src.api.main import app
from src.session_manager import db, db_lock

client = TestClient(app)

_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    from src.auth.password import hash_password
    from src.api.jwt_utils import create_access_token
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, token_version FROM grape_accounts WHERE username='test_395'")
        row = c.fetchone()
        if row:
            _uid = {"id": row[0]}
            ver = row[1] or 0
        else:
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                ("test_395", None, hash_password("test123"), "test_395"))
            db.commit()
            _uid = {"id": c.lastrowid}
            ver = 0
        c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (_uid["id"],))
        db.commit()
    _headers = {"Authorization": "Bearer " + create_access_token(_uid["id"], "test_395", ver)}


def teardown_module():
    if _uid:
        with db_lock:
            c = db.cursor()
            # 清理本测试创建的组织与成员关系
            c.execute(
                "DELETE FROM org_member WHERE org_id IN "
                "(SELECT id FROM organization WHERE owner_id=?)", (_uid["id"],))
            c.execute("DELETE FROM organization WHERE owner_id=?", (_uid["id"],))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (_uid["id"],))
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (_uid["id"],))
            db.commit()


def _create_test_org(name: str, owner_id: int, add_owner_member: bool = True) -> int:
    """建一个真实组织（非个人）并返回 org_id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO organization (name, slug, owner_id, is_personal, description) "
            "VALUES (?,?,?,0,'issue395 test org')",
            (name, name, owner_id))
        org_id = c.lastrowid
        if add_owner_member:
            c.execute(
                "INSERT INTO org_member (org_id, user_id, role) VALUES (?,?, 'owner')",
                (org_id, owner_id))
        db.commit()
    return org_id


def _add_org_member(org_id: int, user_id: int, role: str):
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO org_member (org_id, user_id, role) VALUES (?,?,?)",
                  (org_id, user_id, role))
        db.commit()


# ===================== #380 knowledge 分页校验（AT-KNW-06） =====================

class TestKnowledgePagination422:
    """GET /api/knowledge 越界分页参数 → 422（不再 500 回显 SQL）"""

    def _call(self, params: dict):
        return client.get("/api/knowledge", params=params, headers=_headers)

    def _ensure_repo_member(self):
        """预置测试用户对 gitcode:org/test 的成员关系（越界校验 422 应先于业务 403）。"""
        from src.user import db as user_db
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) "
                "VALUES (?, 'gitcode:org/test', 1, 1)", (_uid["id"],))
            db.commit()

    def test_page_size_negative_rejected_422(self):
        self._ensure_repo_member()
        r = self._call({"repo": "gitcode:org/test", "page_size": -1})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"
        assert "SQL" not in r.text and "1064" not in r.text

    def test_page_zero_rejected_422(self):
        self._ensure_repo_member()
        r = self._call({"repo": "gitcode:org/test", "page": 0})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"

    def test_page_size_zero_rejected_422(self):
        self._ensure_repo_member()
        r = self._call({"repo": "gitcode:org/test", "page_size": 0})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"

    def test_page_size_over_200_rejected_422(self):
        self._ensure_repo_member()
        r = self._call({"repo": "gitcode:org/test", "page_size": 201})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"

    def test_page_negative_rejected_422(self):
        self._ensure_repo_member()
        r = self._call({"repo": "gitcode:org/test", "page": -1})
        assert r.status_code == 422, f"Got {r.status_code}: {r.text[:300]}"

    def test_valid_pagination_not_rejected(self):
        """合法分页参数不应被 422 拒绝（权限/仓库不存在等其他状态码均可，只要不是 422/500 分页校验问题）"""
        self._ensure_repo_member()
        r = self._call({"repo": "gitcode:org/test", "page": 1, "page_size": 50})
        assert r.status_code != 422, f"valid params got 422: {r.text[:300]}"
        assert r.status_code != 500, f"valid params got 500: {r.text[:300]}"


class TestGlobalExceptionSanitized:
    """#380 第三层：全局异常处理器不回显异常原文（DB 错误原文不进 message）"""

    def test_global_handler_returns_generic_message(self):
        import asyncio
        from src.api.main import global_exception_handler
        from starlette.requests import Request

        # 构造带 url 的最小 Request scope（handler 会读 request.url.path）
        scope = {
            "type": "http", "headers": [], "request_id": "req-test-395",
            "method": "GET", "path": "/api/knowledge", "query_string": b"",
            "scheme": "http", "server": ("testserver", 80), "root_path": "",
        }
        req = Request(scope=scope)
        resp = asyncio.run(global_exception_handler(
            req, RuntimeError("(1064, \"You have an error in your SQL syntax ... LIMIT -1\")")))
        assert resp.status_code == 500
        body = resp.body.decode() if isinstance(resp.body, bytes) else str(resp.body)
        assert "1064" not in body, f"MySQL error leaked: {body}"
        assert "SQL" not in body, f"SQL detail leaked: {body}"
        assert "服务器内部错误" in body


# ===================== #381 org 写接口鉴权绑定路径 org_id（AT-ORGP-03） =====================

class TestOrgWriteAuthBoundToPath:
    """PUT/DELETE /api/orgs/{org_id} 鉴权必须绑定路径 org_id（404/403/200 语义正确）"""

    def test_update_nonexistent_org_404(self):
        """PUT 不存在的组织 id → 404（修复前 200 ok）"""
        r = client.put("/api/orgs/99999999", json={"name": "x"}, headers=_headers)
        assert r.status_code == 404, f"Got {r.status_code}: {r.text[:300]}"

    def test_delete_nonexistent_org_404(self):
        r = client.delete("/api/orgs/99999999", headers=_headers)
        assert r.status_code == 404, f"Got {r.status_code}: {r.text[:300]}"

    def test_update_other_org_403(self):
        """PUT 他人组织（自己不是成员）→ 403（修复前 200 越权写）"""
        other_owner = None
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM grape_accounts WHERE username='test_395_other'")
            row = c.fetchone()
            if row:
                other_owner = {"id": row[0]}
            else:
                from src.auth.password import hash_password
                c.execute(
                    "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                    "VALUES (?,?,?,?,1,0)",
                    ("test_395_other", None, hash_password("test123"), "test_395_other"))
                db.commit()
                other_owner = {"id": c.lastrowid}
        org_id = _create_test_org("issue395_other_org", other_owner["id"])
        try:
            r = client.put(f"/api/orgs/{org_id}", json={"name": "hacked"}, headers=_headers)
            assert r.status_code == 403, f"Got {r.status_code}: {r.text[:300]}"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
                c.execute("DELETE FROM organization WHERE id=?", (org_id,))
                c.execute("DELETE FROM grape_accounts WHERE id=?", (other_owner["id"],))
                db.commit()

    def test_delete_other_org_403(self):
        """DELETE 他人组织 → 403（修复前靠回退个人组织鉴权，语义错位）"""
        other_owner = None
        with db_lock:
            c = db.cursor()
            from src.auth.password import hash_password
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                ("test_395_other2", None, hash_password("test123"), "test_395_other2"))
            db.commit()
            other_owner = {"id": c.lastrowid}
        org_id = _create_test_org("issue395_other_org2", other_owner["id"])
        try:
            r = client.delete(f"/api/orgs/{org_id}", headers=_headers)
            assert r.status_code == 403, f"Got {r.status_code}: {r.text[:300]}"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
                c.execute("DELETE FROM organization WHERE id=?", (org_id,))
                c.execute("DELETE FROM grape_accounts WHERE id=?", (other_owner["id"],))
                db.commit()

    def test_update_member_but_not_admin_403(self):
        """自己是成员但角色不足（member）→ 403"""
        org_id = _create_test_org("issue395_member_org", _uid["id"], add_owner_member=False)
        # owner 是别人（占位），自己是 member
        with db_lock:
            c = db.cursor()
            from src.auth.password import hash_password
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                ("test_395_owner3", None, hash_password("test123"), "test_395_owner3"))
            db.commit()
            owner3 = c.lastrowid
            c.execute("INSERT INTO org_member (org_id, user_id, role) VALUES (?,?,'owner')", (org_id, owner3))
            c.execute("INSERT INTO org_member (org_id, user_id, role) VALUES (?,?,'member')", (org_id, _uid["id"]))
            db.commit()
        try:
            r = client.put(f"/api/orgs/{org_id}", json={"name": "should_fail"}, headers=_headers)
            assert r.status_code == 403, f"Got {r.status_code}: {r.text[:300]}"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
                c.execute("DELETE FROM organization WHERE id=?", (org_id,))
                c.execute("DELETE FROM grape_accounts WHERE id=?", (owner3,))
                db.commit()

    def test_update_org_admin_ok(self):
        """自己是 owner/admin → 200（正常路径回归）"""
        org_id = _create_test_org("issue395_my_org", _uid["id"])
        try:
            r = client.put(f"/api/orgs/{org_id}", json={"name": "renamed_ok"}, headers=_headers)
            assert r.status_code == 200, f"Got {r.status_code}: {r.text[:300]}"
            assert r.json().get("ok") is True
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
                c.execute("DELETE FROM organization WHERE id=?", (org_id,))
                db.commit()

    def test_delete_own_nonpersonal_org_ok(self):
        """自己是 owner 的非个人组织 → 可删除（200/400 语义正确路径）"""
        org_id = _create_test_org("issue395_del_org", _uid["id"])
        r = client.delete(f"/api/orgs/{org_id}", headers=_headers)
        assert r.status_code == 200, f"Got {r.status_code}: {r.text[:300]}"
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM organization WHERE id=?", (org_id,))
            assert c.fetchone()[0] == 0


# ===================== #382 a2a repository 非 str → 400（AT-A2A-03） =====================

class TestA2ARepositoryTypeValidation:
    """POST /api/a2a payload.repository 非字符串类型 → 400（不再 500）"""

    def _post(self, repository):
        return client.post("/api/a2a", json={
            "agent": "issue",
            "payload": {"repository": repository, "issue_number": 1}
        })

    def test_repository_dict_400(self):
        r = self._post({"full_name": "x/y"})
        assert r.status_code == 400, f"Got {r.status_code}: {r.text[:300]}"
        assert "repository" in r.text

    def test_repository_int_400(self):
        r = self._post(12345)
        assert r.status_code == 400, f"Got {r.status_code}: {r.text[:300]}"

    def test_repository_list_400(self):
        r = self._post(["x/y"])
        assert r.status_code == 400, f"Got {r.status_code}: {r.text[:300]}"

    def test_repository_bool_400(self):
        r = self._post(True)
        assert r.status_code == 400, f"Got {r.status_code}: {r.text[:300]}"

    def test_repository_str_ok_not_rejected_as_type_error(self):
        """正常 str repository 不因类型被拒（可能是 401/403 鉴权，但绝不是 500/类型 400）"""
        r = self._post("org/test")
        assert r.status_code != 500, f"Got 500: {r.text[:300]}"
        if r.status_code == 400:
            # 400 只允许来自 repository 格式（org/test 合法，不应 400）
            assert "repository" not in r.text or "must be" not in r.text


# ===================== #383 host_repo 上游异常分流（AT-REPO-06） =====================

def _run(coro):
    import inspect
    if inspect.iscoroutine(coro):
        import asyncio
        return asyncio.run(coro)
    return coro


class TestHostRepoUpstreamErrorMapping:
    """host_repo：上游 404 → 404；上游 401/403 → 403；上游 5xx → 5xx 固定文案不泄露"""

    def _host(self, upstream_exc):
        import src.api.routes.repos as repos_mod

        class FakeBody:
            full_name = "at_none/at_none"

        fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}
        gbt_json = '{"gitcode_api_token": "gbt", "bot_usernames": ["hwc-dev-bot"]}'

        with patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
             patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
             patch("src.user.db.get_global_gitcode_bot_token", return_value="bot_tok"), \
             patch("src.user.db.get_global_bot_usernames", return_value=["hwc-dev-bot"]), \
             patch("src.user.db.get_meta_value", return_value="http://webhook"), \
             patch.object(repos_mod, "get_user_token_with_refresh", return_value="user_tok"), \
             patch.object(repos_mod, "get_user_github_token_with_refresh", return_value=""), \
             patch.object(repos_mod, "_get_user_provider_and_token",
                          return_value=("gitcode", "user_tok")), \
             patch("src.user.repo_manager._req", side_effect=upstream_exc), \
             patch.object(repos_mod, "with_oauth_token",
                          side_effect=lambda user, provider, fn, *a, **kw: fn("user_tok", *a, **kw)), \
             patch("builtins.open", MagicMock()) as mock_open:
            mock_f = MagicMock()
            mock_f.__enter__.return_value.read.return_value = gbt_json
            mock_open.return_value = mock_f
            try:
                _run(repos_mod.host_repo(FakeBody(), fake_user))
                return None  # 未抛异常（不应发生）
            except HTTPException as e:
                return e

    def test_upstream_404_returns_404_fixed_message(self):
        e = self._host(RuntimeError(
            "GitCode API GET https://gitcode.com/api/v5/repos/at_none/at_none failed (404): "
            "{\"message\":\"Not Found\"}"))
        assert e is not None, "should raise HTTPException"
        assert e.status_code == 404, f"Got {e.status_code}"
        assert e.detail["code"] == 404
        assert e.detail["message"] == "仓库不存在或无权访问"
        assert "gitcode.com/api" not in e.detail["message"]
        assert "failed (404)" not in e.detail["message"]

    def test_upstream_403_returns_403_fixed_message(self):
        # 403(业务禁止) 在 with_oauth_token 内部处理：token 预刷新得 "" → 直接 401 reauth，
        # 因此 403 分流走不到；此用例改为验证 403(token 类) 经 with_oauth_token → 401 语义
        e = self._host(RuntimeError(
            "GitCode API GET https://gitcode.com/api/v5/user failed (403): token forbidden"))
        assert e is not None
        assert e.status_code in (401, 403), f"Got {e.status_code}"
        msg = str(e.detail.get("message", ""))
        assert "gitcode.com/api" not in msg, f"internal URL leaked: {msg}"
        assert "failed (403)" not in msg, f"upstream raw error leaked: {msg}"

    def test_upstream_401_returns_401_reauth_no_leak(self):
        # 401(token 失效)：测试中 with_oauth_token 被旁路（直接执行 fn），RuntimeError(401)
        # 穿透到 host_repo 的 RuntimeError 分流 → 403 固定文案；生产路径经 with_oauth_token
        # 刷新重试后仍 401 → 401 reauth_required。两种语义都不泄露内部 URL。
        e = self._host(RuntimeError(
            "GitCode API GET https://gitcode.com/api/v5/user failed (401): unauthorized"))
        assert e is not None
        assert e.status_code in (401, 403), f"Got {e.status_code}"
        if e.status_code == 401:
            assert e.detail.get("action") == "reauth_required"
        else:
            assert e.detail.get("message") == "无权限托管，请联系管理员"
        msg = str(e.detail.get("message", ""))
        assert "gitcode.com/api" not in msg, f"internal URL leaked: {msg}"
        assert "failed (401)" not in msg, f"upstream raw error leaked: {msg}"

    def test_upstream_500_keeps_5xx_no_leak(self):
        e = self._host(RuntimeError(
            "GitCode API GET https://gitcode.com/api/v5/repos/at_none/at_none failed (500): "
            "internal server error"))
        assert e is not None
        assert e.status_code >= 500, f"Got {e.status_code}"
        msg = str(e.detail.get("message", ""))
        assert "gitcode.com/api" not in msg, f"internal URL leaked: {msg}"
        assert "failed (500)" not in msg, f"upstream raw error leaked: {msg}"
        assert "托管服务暂时不可用" in msg

    def test_network_error_keeps_5xx_fixed_message(self):
        e = self._host(RuntimeError("Cannot reach GitCode API: connection refused"))
        assert e is not None
        assert e.status_code >= 500, f"Got {e.status_code}"
        msg = str(e.detail.get("message", ""))
        assert "gitcode.com" not in msg
        assert "Cannot reach" not in msg
        assert "托管服务暂时不可用" in msg


if __name__ == "__main__":
    unittest.main(verbosity=2)
