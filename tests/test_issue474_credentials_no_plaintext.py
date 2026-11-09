"""Issue #474: GET /api/settings/credentials 不应回显明文 AK/SK。

背景：凭据视图 huawei_cloud 块原先同时返回 ak_exists/sk_exists 以及明文 ak/sk
（取库直出、未遮蔽）。SK 等同云账号操作密钥，任意已登录用户可无审计读取原值，
泄露面覆盖浏览器缓存/截图/代理与网关日志/第三方插件。

本测试验证（与平台「机密只回存在性」口径对齐）：
  1. 已有 huawei_cloud 凭据时：huawei_cloud 块仅含 ak_exists/sk_exists，无 ak/sk 明文键
  2. 未配置 huawei_cloud 凭据时：存在性为 False 且仍无明文键
  3. 经 PUT 空串显式清除后：存在性为 False 且无明文键
  4. 回归：codearts 块不回退（仅存在性、有值可正确反映、无明文键）

运行：python -m pytest tests/test_issue474_credentials_no_plaintext.py -v
"""
import os
import sys
import unittest

# 强制使用独立测试库，防止 TestClient 误连生产库（与 test_api_routes.py 同款双保险）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.user import db as user_db
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
        c.execute("SELECT id, token_version FROM grape_accounts WHERE username='test_474'")
        row = c.fetchone()
        if row:
            _uid = {"id": row[0]}
            ver = row[1] or 0
        else:
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                ("test_474", None, hash_password("test123"), "test_474"))
            db.commit()
            _uid = {"id": c.lastrowid}
            ver = 0
        # 清理本用户可能残留的凭据，保证用例间状态干净
        c.execute("DELETE FROM user_credentials WHERE user_id=?", (_uid["id"],))
        db.commit()
    _headers = {"Authorization": "Bearer " + create_access_token(_uid["id"], "test_474", ver)}


def teardown_module():
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM user_credentials WHERE user_id=?", (_uid["id"],))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (_uid["id"],))
            db.commit()


class TestCredentialsNoPlaintext:
    """凭据响应不应包含明文 AK/SK 键（Issue #474）"""

    def test_huawei_cloud_has_no_plaintext_when_configured(self):
        # 有值：huawei_cloud 块只应存在 ak_exists/sk_exists，绝不能出现 ak/sk 明文键
        user_db.set_credential(_uid["id"], "huawei_cloud", {"ak": "real_ak_474", "sk": "real_sk_474"})
        try:
            r = client.get("/api/settings/credentials", headers=_headers)
            assert r.status_code == 200
            hw = r.json()["huawei_cloud"]
            assert hw["ak_exists"] is True
            assert hw["sk_exists"] is True
            assert "ak" not in hw, "huawei_cloud 块不应包含明文 ak 键"
            assert "sk" not in hw, "huawei_cloud 块不应包含明文 sk 键"
        finally:
            user_db.set_credential(_uid["id"], "huawei_cloud", {})

    def test_huawei_cloud_has_no_plaintext_when_not_configured(self):
        # 无值：存在性为 False 且仍无明文键
        user_db.set_credential(_uid["id"], "huawei_cloud", {})
        r = client.get("/api/settings/credentials", headers=_headers)
        assert r.status_code == 200
        hw = r.json()["huawei_cloud"]
        assert hw["ak_exists"] is False
        assert hw["sk_exists"] is False
        assert "ak" not in hw, "huawei_cloud 块不应包含明文 ak 键"
        assert "sk" not in hw, "huawei_cloud 块不应包含明文 sk 键"

    def test_clear_after_put_empty_string_no_plaintext(self):
        # 显式清除：PUT 空串后 GET 应反映清除且无明文键
        client.put("/api/settings/credentials", json={
            "huawei_cloud_ak": "clear_ak_474", "huawei_cloud_sk": "clear_sk_474"
        }, headers=_headers)
        client.put("/api/settings/credentials", json={
            "huawei_cloud_ak": "", "huawei_cloud_sk": ""
        }, headers=_headers)
        r = client.get("/api/settings/credentials", headers=_headers)
        assert r.status_code == 200
        hw = r.json()["huawei_cloud"]
        assert hw["ak_exists"] is False
        assert hw["sk_exists"] is False
        assert "ak" not in hw
        assert "sk" not in hw

    def test_codearts_block_not_regressed(self):
        # 回归：codearts 块同样仅存在性、无明文键，有值可正确反映
        user_db.set_credential(_uid["id"], "codearts", {"ak": "cd_ak_474", "sk": "cd_sk_474"})
        try:
            r = client.get("/api/settings/credentials", headers=_headers)
            assert r.status_code == 200
            ca = r.json()["codearts"]
            assert ca["ak_exists"] is True
            assert ca["sk_exists"] is True
            assert "ak" not in ca, "codearts 块不应回退出现明文 ak 键"
            assert "sk" not in ca, "codearts 块不应回退出现明文 sk 键"
        finally:
            user_db.set_credential(_uid["id"], "codearts", {})


if __name__ == "__main__":
    unittest.main()