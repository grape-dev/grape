"""飞书扫码一键建应用（feishu_onboard）测试

覆盖 docs/new-version2/feishu-qr-app-onboarding-plan.md §10 的用例：
- start 参数校验 / 并发限制
- on_qr_code 回调 → waiting_confirm + qr_url 落库
- 扫码成功 → connected + chat_agent_session / devops_agent_session 落库
- 异常（用户拒绝 / 过期 / 通用错误）→ failed
- status 端点权限（不存在 / 越权）
- cancel 幂等
"""
import asyncio
import os
import sys
import uuid
from unittest.mock import patch, AsyncMock

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

from fastapi.testclient import TestClient
from src.api.main import app
from src.user import db as user_db
from src.session_manager import db, db_lock

client = TestClient(app)

_uid = None
_username = None
_token = ""
_headers = {}

QR_URL = "https://accounts.feishu.cn/authorize?device_code=abc"


def _result():
    """动态生成唯一 app_id/app_secret，避免测试库残留绑定污染（app_id 全局唯一强约束）。"""
    return {
        "client_id": "cli_onboard_" + uuid.uuid4().hex[:12],
        "client_secret": "sec_onboard_" + uuid.uuid4().hex[:12],
        "user_info": {"open_id": "ou_xxx", "tenant_brand": "feishu"},
    }


def setup_module():
    global _uid, _username, _token, _headers
    _username = "onboard_" + uuid.uuid4().hex[:8]
    _uid = user_db.create_user(_username, "pw123")
    if not _uid:
        _uid = user_db.get_user_by_username(_username)
    _token = user_db.create_session_token(_uid["id"])
    _headers = {"Authorization": f"Bearer {_token}"}


def teardown_module():
    global _uid
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM feishu_onboard_task WHERE user_id=?", (_uid["id"],))
            c.execute("DELETE FROM chat_agent_session WHERE user_id=?", (_uid["id"],))
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (f"repo_{_uid['id']}",))
            c.execute("DELETE FROM user_accounts WHERE id=?", (_uid["id"],))
            c.execute("DELETE FROM global_im_bot_config WHERE bot_app_id LIKE 'cli_onboard_%' OR bot_app_id LIKE 'cli_vod_%'")
            db.commit()
        user_db.delete_user(_uid["id"])


def _clean_tasks():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM feishu_onboard_task WHERE user_id=?", (_uid["id"],))
        db.commit()


def _patch_register(side_effect):
    """patch lark_oapi.aregister_app（SDK 顶层函数），side_effect 为 async 协程工厂。"""
    return patch("lark_oapi.aregister_app", side_effect=side_effect)


async def _fake_success(**kwargs):
    """正常流程：先触发 on_qr_code，再返回 RESULT"""
    kwargs.get("on_qr_code")({"url": QR_URL, "expire_in": 300})
    return _result()


def _await_run(bot_type, repo_key="", patched=None):
    """直接 await 后台协程（同步测试辅助），确保状态已落库。"""
    from src.api.routes import feishu_onboard as f
    task_id = uuid.uuid4().hex
    f._create_task(task_id, bot_type, repo_key, _uid["id"])
    with patched:
        asyncio.run(f._run_registration(task_id, bot_type, repo_key, _uid["id"]))
    return task_id


# ===================== A. start 端点 =====================
class TestStart:
    def test_start_returns_task_id(self):
        _clean_tasks()
        async def _stub(**kwargs):
            kwargs.get("on_qr_code")({"url": QR_URL, "expire_in": 300})
            return _result()

        with _patch_register(_stub) as m:
            r = client.post("/api/feishu/onboard/start", json={"bot_type": "chat_bot"}, headers=_headers)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["status"] == "pending"
            assert data["task_id"]
            m.assert_awaited()
        _clean_tasks()

    def test_start_invalid_bot_type(self):
        r = client.post("/api/feishu/onboard/start", json={"bot_type": "xxx"}, headers=_headers)
        assert r.status_code == 400
        assert "bot_type" in r.json()["detail"]["message"]

    def test_start_vod_requires_repo_key(self):
        r = client.post("/api/feishu/onboard/start", json={"bot_type": "vod_bot"}, headers=_headers)
        assert r.status_code == 400
        assert "repo_key" in r.json()["detail"]["message"]

    def test_start_concurrency_409(self):
        _clean_tasks()
        from src.api.routes import feishu_onboard as f
        # 先插入一条进行中的任务
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO feishu_onboard_task (task_id, bot_type, user_id, status) VALUES (?,?,?,?)",
                ("concurrent_task", "chat_bot", _uid["id"], "waiting_confirm"),
            )
            db.commit()
        try:
            with _patch_register(AsyncMock()) as m:
                r = client.post("/api/feishu/onboard/start", json={"bot_type": "chat_bot"}, headers=_headers)
                assert r.status_code == 409, r.text
                m.assert_not_awaited()
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM feishu_onboard_task WHERE task_id='concurrent_task'")
                db.commit()


# ===================== B. 扫码回调 / 状态流转 =====================
class TestStatusFlow:
    def test_qr_callback_sets_waiting_confirm(self):
        """on_qr_code 触发后任务置 waiting_confirm 且 qr_url 落库。"""
        _clean_tasks()
        task_id = _await_run("chat_bot", patched=_patch_register(_fake_success))
        from src.api.routes import feishu_onboard as f
        t = f._get_task(task_id)
        assert t["status"] == "connected"
        assert t["qr_url"] == QR_URL
        _clean_tasks()

    def test_chat_connected_persists_chat_session(self):
        """扫码成功 chat_bot → chat_agent_session + global_im_bot_config 落库。"""
        _clean_tasks()
        result = _result()
        task_id = _await_run("chat_bot", patched=_patch_register(_fake_success))
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT im_app_id, im_app_secret FROM chat_agent_session WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) LIMIT 1",
                (_uid["id"],),
            ).fetchone()
            reg = c.execute(
                "SELECT bot_type FROM global_im_bot_config WHERE bot_app_id=? AND is_active=1",
                (row[0],) if row else ("",),
            ).fetchone()
        assert row and row[0].startswith("cli_onboard_")
        assert row[1].startswith("sec_onboard_")
        assert reg and reg[0] == "chat_bot"
        _clean_tasks()

    def test_vod_connected_persists_devops_session(self):
        """扫码成功 vod_bot → devops_agent_session + global_im_bot_config 落库。"""
        _clean_tasks()
        repo_key = f"repo_{_uid['id']}"
        from src.api.routes import feishu_onboard as f
        # 预建 devops_agent_session 行（vod）
        import datetime
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, status, last_active_at, im_provider) VALUES (?,?,?,?,datetime('now'),'feishu')",
                (f"vod_{_uid['id']}", repo_key, "vod", "idle"),
            )
            db.commit()
        with _patch_register(_fake_success) as patched:
            from src.api.routes import feishu_onboard as f
            # 改用 vod 专用返回
            async def _fake_vod(**kwargs):
                kwargs.get("on_qr_code")({"url": QR_URL, "expire_in": 300})
                return _result()
            patched.side_effect = _fake_vod
            task_id = _await_run("vod_bot", repo_key, patched=patched)
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT im_app_id, im_app_secret FROM devops_agent_session WHERE repo_key=? AND agent_type='vod'",
                (repo_key,),
            ).fetchone()
            reg = c.execute(
                "SELECT bot_type FROM global_im_bot_config WHERE bot_app_id=? AND is_active=1",
                (row[0],) if row else ("",),
            ).fetchone()
        assert row and row[0].startswith("cli_onboard_")
        assert row[1].startswith("sec_onboard_")
        assert reg and reg[0] == "vod_bot"
        _clean_tasks()

    def test_access_denied_fails(self):
        """用户拒绝授权 → AppAccessDeniedError → failed"""
        _clean_tasks()
        from lark_oapi.scene.registration import AppAccessDeniedError

        async def _denied(**kwargs):
            raise AppAccessDeniedError("access_denied", "user denied")

        task_id = _await_run("chat_bot", patched=_patch_register(_denied))
        from src.api.routes import feishu_onboard as f
        t = f._get_task(task_id)
        assert t["status"] == "failed"
        _clean_tasks()

    def test_expired_fails(self):
        """二维码过期 → AppExpiredError → failed"""
        _clean_tasks()
        from lark_oapi.scene.registration import AppExpiredError

        async def _expired(**kwargs):
            raise AppExpiredError("expired_token", "polling timed out")

        task_id = _await_run("chat_bot", patched=_patch_register(_expired))
        from src.api.routes import feishu_onboard as f
        t = f._get_task(task_id)
        assert t["status"] == "failed"
        assert "expired" in t["error"].lower()
        _clean_tasks()

    def test_generic_error_fails(self):
        """通用异常 → failed"""
        _clean_tasks()

        async def _boom(**kwargs):
            raise RuntimeError("network down")

        task_id = _await_run("chat_bot", patched=_patch_register(_boom))
        from src.api.routes import feishu_onboard as f
        t = f._get_task(task_id)
        assert t["status"] == "failed"
        assert "network down" in t["error"]
        _clean_tasks()


# ===================== C. status 端点 =====================
class TestStatusEndpoint:
    def test_status_connected(self):
        _clean_tasks()
        task_id = _await_run("chat_bot", patched=_patch_register(_fake_success))
        r = client.get(f"/api/feishu/onboard/status?task_id={task_id}", headers=_headers)
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "connected"
        assert d["app_id"].startswith("cli_onboard_")
        assert d["app_secret_exists"] is True
        _clean_tasks()

    def test_status_not_found(self):
        r = client.get("/api/feishu/onboard/status?task_id=no_such_task", headers=_headers)
        assert r.status_code == 404

    def test_status_cross_user_403(self):
        """他人 task_id → 403 防越权"""
        _clean_tasks()
        user_b = user_db.create_user("onboard_b_" + uuid.uuid4().hex[:6], "pw123")
        task_id = _await_run("chat_bot", patched=_patch_register(_fake_success))
        # 换用 user_b 查询
        token_b = user_db.create_session_token(user_b["id"])
        r = client.get(
            f"/api/feishu/onboard/status?task_id={task_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert r.status_code == 403
        user_db.delete_user(user_b["id"])
        _clean_tasks()


# ===================== D. cancel 端点 =====================
class TestCancel:
    def test_cancel_pending_task(self):
        _clean_tasks()
        # 直接建一条 waiting_confirm 任务（无后台协程，cancel 幂等即可）
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO feishu_onboard_task (task_id, bot_type, user_id, status, qr_url) VALUES (?,?,?,?,?)",
                ("cancel_task", "chat_bot", _uid["id"], "waiting_confirm", QR_URL),
            )
            db.commit()
        try:
            r = client.post("/api/feishu/onboard/cancel", json={"task_id": "cancel_task"}, headers=_headers)
            assert r.status_code == 200, r.text
            from src.api.routes import feishu_onboard as f
            t = f._get_task("cancel_task")
            assert t["status"] == "failed"
            assert "取消" in t["error"]
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM feishu_onboard_task WHERE task_id='cancel_task'")
                db.commit()

    def test_cancel_already_connected_idempotent(self):
        """已 connected 的任务 cancel 幂等，不改状态。"""
        _clean_tasks()
        task_id = _await_run("chat_bot", patched=_patch_register(_fake_success))
        r = client.post("/api/feishu/onboard/cancel", json={"task_id": task_id}, headers=_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "connected"
        from src.api.routes import feishu_onboard as f
        t = f._get_task(task_id)
        assert t["status"] == "connected"
        _clean_tasks()

    def test_cancel_not_found(self):
        r = client.post("/api/feishu/onboard/cancel", json={"task_id": "no_such"}, headers=_headers)
        assert r.status_code == 404


# ===================== E. addons schema 看护 =====================
class TestAddonsSchema:
    """看护 _ADDONS 结构，防止引入 SDK 不支持的键（如 preset）导致扫码必失败。

    背景：lark-oapi 1.7.0 的 _normalize_addons 仅允许顶层 scopes/events/callbacks，
    文档中的 addons.preset 在实测中抛 ValueError('addons.preset is not allowed')，
    曾导致所有扫码任务直接 failed（见 feishu-qr-app-onboarding-plan.md 修复记录）。
    """

    def test_addons_top_level_keys_allowed(self):
        """_ADDONS 顶层键必须 ⊆ SDK 允许集 {scopes, events, callbacks}。"""
        from src.api.routes.feishu_onboard import _ADDONS
        allowed = {"scopes", "events", "callbacks"}
        unknown = set(_ADDONS) - allowed
        assert not unknown, f"addons 含 SDK 不支持的键: {unknown}"

    def test_addons_sdk_normalize_accepts(self):
        """_ADDONS 必须能被 SDK 的 _normalize_addons 校验通过（不抛 ValueError）。

        直接调用 SDK 校验函数，等于模拟 aregister_app 内部首个校验步骤；
        若校验抛错则说明 schema 与 SDK 版本不兼容，扫码必失败。
        """
        from src.api.routes.feishu_onboard import _ADDONS
        from lark_oapi.scene.registration import _normalize_addons
        normalized = _normalize_addons(_ADDONS)
        assert "scopes" in normalized
        assert "tenant" in normalized["scopes"]
        assert "im:message:send_as_bot" in normalized["scopes"]["tenant"]
        assert "events" in normalized
        assert "im.message.receive_v1" in normalized["events"]["items"]["tenant"]

    def test_addons_encode_addons_no_error(self):
        """_ADDONS 必须能通过 SDK 完整编码（gzip+base64），模拟真实调用路径。"""
        from src.api.routes.feishu_onboard import _ADDONS
        from lark_oapi.scene.registration import _encode_addons
        encoded = _encode_addons(_ADDONS)
        assert encoded, "addons 编码结果不应为空"

    def test_addons_has_required_scopes_and_event(self):
        """关键依赖：发送消息 scope + im.message.receive_v1 事件不可缺失。"""
        from src.api.routes.feishu_onboard import _ADDONS
        tenant_scopes = _ADDONS["scopes"]["tenant"]
        assert "im:message:send_as_bot" in tenant_scopes
        assert "im:chat:readonly" in tenant_scopes
        events = _ADDONS["events"]["items"]["tenant"]
        assert "im.message.receive_v1" in events
