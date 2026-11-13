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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient

from fastapi import HTTPException

from src.api.main import app

from src.kernel.session_manager import db, db_lock

client = TestClient(app)

_uid = None

_headers = {}

def setup_module():
    global _uid, _headers
    from src.domains.mgmt.auth.password import hash_password
    from src.kernel.auth_sdk.jwt_utils import create_access_token
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

class TestKnowledgePagination422:
    """GET /api/knowledge 越界分页参数 → 422（不再 500 回显 SQL）"""

    def _call(self, params: dict):
        return client.get("/api/knowledge", params=params, headers=_headers)

    def _ensure_repo_member(self):
        """预置测试用户对 gitcode:org/test 的成员关系（越界校验 422 应先于业务 403）。"""
        from src.domains.mgmt.user import db as user_db
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
                from src.domains.mgmt.auth.password import hash_password
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
            from src.domains.mgmt.auth.password import hash_password
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
            from src.domains.mgmt.auth.password import hash_password
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

def _run(coro):
    import inspect
    if inspect.iscoroutine(coro):
        import asyncio
        return asyncio.run(coro)
    return coro

class TestHostRepoUpstreamErrorMapping:
    """host_repo 上游异常分流（D97 缝迁移重写：mock 缝从已删符号
    get_user_token_with_refresh 迁至卡驱动端口 create_provider().get_user_login——
    对齐 ed04df860 簇1 _mock_provider 手法与 UC18 网络缝口径）。

    现行语义（S62/D42 适配器端口后，完整生产链实测锚定）：
    (404)→404 REPO_NOT_FOUND；401→with_oauth_token 刷新域 401 reauth_required；
    403 业务权限（非 token 关键词）→403 TOKEN_PERMISSION_INSUFFICIENT；
    403+token 关键词→同 401 刷新域；5xx/网络错→身份前置放行→下游
    _host_502_detail 兜底 502（retryable=true，上游报文不透出 #383/#395）。
    五用例按现行语义重写——旧预期（401/403 直连分流、5xx 固定文案）为
    重构前行为。"""

    def _host(self, upstream_exc):
        import src.domains.mgmt.routes.repos as repos_mod

        class FakeBody:
            full_name = "at_none/at_none"

        fake_user = {"id": 1, "username": "tester", "auth_type": "session_token"}

        class _FakeProv:
            """卡驱动端口行为注入：get_user_login 抛指定上游异常（身份前置缝）。"""

            def get_user_login(self, token):
                raise upstream_exc

            def webhook_token_field(self):
                return "bot_gitcode_token"

            def token_storage_field(self):
                return "bot_gitcode_token"

        # TestClient 形态（对齐 test_repos_host_provider）：HTTPException 被转成
        # 响应，返回 status_code；None=请求成功（放行分支）
        # 注意：webhook_url 检查在身份前置之前（L1111 get_webhook_url）——
        # 必须给非空值，否则 400 WEBHOOK_URL_NOT_CONFIGURED 先于被测分支
        fake_user = dict(fake_user)
        with patch.object(repos_mod, "get_user_token", return_value="user_tok"), \
             patch.object(repos_mod, "get_bot_token", return_value="bot_tok"), \
             patch("src.domains.mgmt.user.db.get_webhook_url", return_value="http://wk.example/webhook/gitcode"), \
             patch("src.domains.mgmt.user.db.get_global_gitcode_bot_token", return_value=""), \
             patch("src.domains.mgmt.user.db.get_global_bot_usernames", return_value=[]), \
             patch("src.domains.mgmt.user.db.get_meta_value", return_value=""), \
             patch("src.kernel.token_resolver.create_provider", return_value=_FakeProv()), \
             patch.object(repos_mod, "get_user_token_for", return_value="user_tok"), \
             patch("src.api.deps.get_user_token_for", return_value="user_tok"), \
             patch.object(repos_mod, "_get_user_provider_and_token",
                          return_value=("gitcode", "user_tok")), \
             patch("builtins.open", MagicMock()):
            r = client.post("/api/repos/host", json={"full_name": "at_none/at_none"},
                            headers=_headers)
            return r.status_code, r.json().get("detail", r.text)

    def test_upstream_404_returns_404_fixed_message(self):
        code, detail = self._host(RuntimeError(
            "GitCode API GET https://gitcode.com/api/v5/repos/at_none/at_none failed (404): "
            "{\"message\":\"Not Found\"}"))
        assert code == 404, f"404 应显式拒绝（REPO_NOT_FOUND），Got {code}: {str(detail)[:120]}"
        assert detail.get("error_code") == "REPO_NOT_FOUND"
        msg = str(detail.get("message", ""))
        assert "gitcode.com/api" not in msg, "内部 URL 泄露"
        assert "failed (404)" not in msg, "上游原始错误泄露"

    def test_upstream_403_maps_403_token_insufficient(self):
        # 现行语义（完整生产链）：403 业务权限（消息不含 token/forbidden 关键词）
        # → with_oauth_token._is_token_error=False → 透传 → host_repo 分流
        # (403) → 403 TOKEN_PERMISSION_INSUFFICIENT。
        # 403+token 关键词走 with_oauth_token 刷新域（→401 reauth，非本分支）。
        code, detail = self._host(RuntimeError(
            "GitCode API GET https://gitcode.com/api/v5/user failed (403): permission denied for this operation"))
        assert code == 403, f"Got {code}: {str(detail)[:120]}"
        assert detail.get("error_code") == "TOKEN_PERMISSION_INSUFFICIENT"
        msg = str(detail.get("message", ""))
        assert "gitcode.com/api" not in msg, f"internal URL leaked: {msg}"
        assert "failed (403)" not in msg, f"upstream raw error leaked: {msg}"

    def test_upstream_401_maps_reauth_required(self):
        # 现行语义（完整生产链）：401 → with_oauth_token._is_token_error=True →
        # refresh_oauth_token（真库无 refresh_token → None）→ 401 reauth_required。
        # host_repo 的 (401)→403 直连分支被刷新域前置拦截，生产不可达。
        code, detail = self._host(RuntimeError(
            "GitCode API GET https://gitcode.com/api/v5/user failed (401): unauthorized"))
        assert code == 401, f"Got {code}: {str(detail)[:120]}"
        assert detail.get("action") == "reauth_required"
        msg = str(detail.get("message", ""))
        assert "gitcode.com/api" not in msg
        assert "failed (401)" not in msg

    def test_upstream_500_maps_502_retryable(self):
        # 现行语义（完整生产链）：5xx → 身份前置放行（issue455 语义）→ 下游
        # webhook 创建段失败 → _host_502_detail 兜底：502 结构化 detail
        # （retryable=true + 自愈提示），上游原始报文不透出（#383/#395 防泄露）
        code, detail = self._host(RuntimeError(
            "GitCode API GET https://gitcode.com/api/v5/repos/at_none/at_none failed (500): "
            "internal server error"))
        assert code == 502, f"Got {code}: {str(detail)[:160]}"
        assert detail.get("retryable") is True
        msg = str(detail.get("message", ""))
        assert "gitcode.com/api" not in msg, f"internal URL leaked: {msg}"
        assert "failed (500)" not in msg, f"upstream raw error leaked: {msg}"
        assert "internal server error" not in msg, "上游原始报文泄露"

    def test_network_error_maps_502_retryable(self):
        # 现行语义：网络错（无状态码）→ 前置放行 → 下游兜底 502 retryable
        code, detail = self._host(RuntimeError("Cannot reach GitCode API: connection refused"))
        assert code == 502, f"Got {code}: {str(detail)[:160]}"
        assert detail.get("retryable") is True
        msg = str(detail.get("message", ""))
        assert "Cannot reach" not in msg, "上游原始错误泄露"
        assert "gitcode.com" not in msg, "内部域名泄露"

if __name__ == "__main__":
    unittest.main(verbosity=2)

# 强制使用独立测试库，防止 TestClient 误连生产库（与 test_api_routes.py 同款双保险）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")
