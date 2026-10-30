"""Issue #474（方案A 追加）：POST /api/settings/credentials/reveal 按需取回明文。

背景：PR #673 已把 GET /api/settings/credentials 收敛为「凭据只回存在性」
（huawei_cloud/codearts 块仅 *_exists）。用户确认方案A——在既有收敛之上追加
独立的「按需取回明文」接口，满足「能看能复制」但不再从列表接口泄露明文：

  1. 二次确认：必须重新输入当前 Grape 密码（错误 → 400，未设置密码 → 400 + need_set_password）
  2. 明文仅当次返回：响应一次返回指定字段明文，并带 Cache-Control: no-store；
     列表接口 GET /credentials 仍只回存在性（回归断言）
  3. 审计日志：每次取回写入 audit_logs（event_type=credential_reveal，detail 含 provider/field）
  4. 高频取回告警：窗口内取回次数超阈值 → 429 + 服务端告警日志

运行：python -m pytest tests/test_issue474_credentials_reveal.py -v
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
        c.execute("SELECT id, token_version FROM grape_accounts WHERE username='test_474_reveal'")
        row = c.fetchone()
        if row:
            _uid = {"id": row[0]}
            ver = row[1] or 0
        else:
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                ("test_474_reveal", None, hash_password("test123"), "test_474_reveal"))
            db.commit()
            _uid = {"id": c.lastrowid}
            ver = 0
        # 清理本用户可能残留的凭据 / 取回日志，保证用例间状态干净
        c.execute("DELETE FROM user_credentials WHERE user_id=?", (_uid["id"],))
        c.execute("DELETE FROM grape_t_credential_reveal_log WHERE user_id=?", (_uid["id"],))
        db.commit()
    _headers = {"Authorization": "Bearer " + create_access_token(_uid["id"], "test_474_reveal", ver)}


def teardown_module():
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_t_credential_reveal_log WHERE user_id=?", (_uid["id"],))
            c.execute("DELETE FROM user_credentials WHERE user_id=?", (_uid["id"],))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (_uid["id"],))
            db.commit()


class TestCredentialsReveal:
    """按需取回明文接口（Issue #474 方案A）"""

    def test_reveal_requires_password_confirm(self):
        # 二次确认：错误密码 / 缺密码 → 400，不返回明文
        user_db.set_credential(_uid["id"], "huawei_cloud", {"ak": "real_ak_474", "sk": "real_sk_474"})
        try:
            r = client.post("/api/settings/credentials/reveal", json={
                "provider": "huawei_cloud", "field": "ak", "password": "wrong-pass"
            }, headers=_headers)
            assert r.status_code == 400, r.text
            assert "密码" in r.json()["detail"]["message"]

            r2 = client.post("/api/settings/credentials/reveal", json={
                "provider": "huawei_cloud", "field": "ak", "password": ""
            }, headers=_headers)
            assert r2.status_code == 400, r2.text
        finally:
            user_db.set_credential(_uid["id"], "huawei_cloud", {})

    def test_reveal_returns_plaintext_once_with_no_store(self):
        # 正确密码 → 当次返回明文一次；响应带 Cache-Control: no-store；列表接口仍无明文
        user_db.set_credential(_uid["id"], "huawei_cloud", {"ak": "real_ak_474", "sk": "real_sk_474"})
        try:
            r = client.post("/api/settings/credentials/reveal", json={
                "provider": "huawei_cloud", "field": "ak", "password": "test123"
            }, headers=_headers)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["provider"] == "huawei_cloud"
            assert data["field"] == "ak"
            assert data["value"] == "real_ak_474"
            assert "no-store" in (r.headers.get("cache-control", "").lower())

            # sk 同样当次返回
            r2 = client.post("/api/settings/credentials/reveal", json={
                "provider": "huawei_cloud", "field": "sk", "password": "test123"
            }, headers=_headers)
            assert r2.status_code == 200, r2.text
            assert r2.json()["value"] == "real_sk_474"

            # 回归：列表接口仍只回存在性，绝无明文
            rg = client.get("/api/settings/credentials", headers=_headers)
            hw = rg.json()["huawei_cloud"]
            assert hw["ak_exists"] is True
            assert hw["sk_exists"] is True
            assert "ak" not in hw
            assert "sk" not in hw
        finally:
            user_db.set_credential(_uid["id"], "huawei_cloud", {})

    def test_reveal_unknown_provider_or_field(self):
        user_db.set_credential(_uid["id"], "huawei_cloud", {"ak": "real_ak_474", "sk": "real_sk_474"})
        try:
            r = client.post("/api/settings/credentials/reveal", json={
                "provider": "unknown_provider", "field": "ak", "password": "test123"
            }, headers=_headers)
            assert r.status_code in (400, 404), r.text

            r2 = client.post("/api/settings/credentials/reveal", json={
                "provider": "huawei_cloud", "field": "token", "password": "test123"
            }, headers=_headers)
            assert r2.status_code in (400, 404), r2.text
        finally:
            user_db.set_credential(_uid["id"], "huawei_cloud", {})

    def test_reveal_missing_credential(self):
        # 未配置凭据 → 不返回明文
        user_db.set_credential(_uid["id"], "huawei_cloud", {})
        r = client.post("/api/settings/credentials/reveal", json={
            "provider": "huawei_cloud", "field": "ak", "password": "test123"
        }, headers=_headers)
        assert r.status_code in (400, 404), r.text

    def test_reveal_writes_audit_log(self):
        # 审计：每次取回写入 audit_logs（event_type=credential_reveal，detail 含 provider/field，不含明文值）
        user_db.set_credential(_uid["id"], "huawei_cloud", {"ak": "real_ak_474", "sk": "real_sk_474"})
        try:
            before = 0
            with db_lock:
                c = db.cursor()
                c.execute("SELECT COUNT(*) FROM audit_logs WHERE event_type='credential_reveal' AND user_id=?", (_uid["id"],))
                before = c.fetchone()[0]
            r = client.post("/api/settings/credentials/reveal", json={
                "provider": "huawei_cloud", "field": "ak", "password": "test123"
            }, headers=_headers)
            assert r.status_code == 200, r.text
            with db_lock:
                c = db.cursor()
                c.execute("SELECT detail FROM audit_logs WHERE event_type='credential_reveal' AND user_id=? ORDER BY seq DESC LIMIT 1", (_uid["id"],))
                row = c.fetchone()
            assert row, "应写入 credential_reveal 审计日志"
            import json as _json
            detail = _json.loads(row[0])
            assert detail.get("provider") == "huawei_cloud"
            assert detail.get("field") == "ak"
            assert "real_ak_474" not in row[0], "审计日志不得记录明文值"
            assert detail.get("count", 1) >= 1
        finally:
            user_db.set_credential(_uid["id"], "huawei_cloud", {})

    def test_reveal_high_frequency_alert(self):
        # 高频取回告警：窗口内取回次数达到阈值 → 429 + 不再返回明文
        from src.api.routes.settings import _REVEAL_WINDOW_SECONDS, _REVEAL_MAX_PER_WINDOW
        user_db.set_credential(_uid["id"], "huawei_cloud", {"ak": "real_ak_474", "sk": "real_sk_474"})
        try:
            # 预填窗口内成功取回记录（模拟高频）
            with db_lock:
                c = db.cursor()
                for _ in range(_REVEAL_MAX_PER_WINDOW):
                    c.execute(
                        "INSERT INTO grape_t_credential_reveal_log (user_id, provider, field, ip) VALUES (?,?,?,?)",
                        (_uid["id"], "huawei_cloud", "ak", "127.0.0.1"))
                db.commit()
            r = client.post("/api/settings/credentials/reveal", json={
                "provider": "huawei_cloud", "field": "ak", "password": "test123"
            }, headers=_headers)
            assert r.status_code == 429, r.text
            assert r.json()["detail"]["code"] == 429
            assert "value" not in r.json()
        finally:
            user_db.set_credential(_uid["id"], "huawei_cloud", {})
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM grape_t_credential_reveal_log WHERE user_id=?", (_uid["id"],))
                db.commit()


class TestCredentialsRevealNoPassword:
    """未设置 Grape 密码的用户：二次确认不可用 → 400 + need_set_password"""

    @classmethod
    def setup_class(cls):
        from src.api.jwt_utils import create_access_token
        global _uid, _headers  # noqa: 复用同一用户（本测试需要无密码账号）
        # 无密码用户独立 id（避免污染主用例用户）
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, token_version FROM grape_accounts WHERE username='test_474_nopwd'")
            row = c.fetchone()
            if row:
                cls.uid = {"id": row[0]}
                ver = row[1] or 0
            else:
                c.execute(
                    "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                    "VALUES (?,?,?,?,1,0)",
                    ("test_474_nopwd", None, "", "test_474_nopwd"))
                db.commit()
                cls.uid = {"id": c.lastrowid}
                ver = 0
        cls.headers = {"Authorization": "Bearer " + create_access_token(cls.uid["id"], "test_474_nopwd", ver)}

    @classmethod
    def teardown_class(cls):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM user_credentials WHERE user_id=?", (cls.uid["id"],))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (cls.uid["id"],))
            db.commit()

    def test_reveal_no_password_set_rejected(self):
        user_db.set_credential(self.uid["id"], "huawei_cloud", {"ak": "real_ak_474", "sk": "real_sk_474"})
        try:
            r = client.post("/api/settings/credentials/reveal", json={
                "provider": "huawei_cloud", "field": "ak", "password": "test123"
            }, headers=self.headers)
            assert r.status_code == 400, r.text
            assert r.json()["detail"].get("need_set_password") is True or "密码" in r.json()["detail"].get("message", "")
        finally:
            user_db.set_credential(self.uid["id"], "huawei_cloud", {})


if __name__ == "__main__":
    unittest.main()