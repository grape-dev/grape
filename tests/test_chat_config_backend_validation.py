"""Issue #508 回归测试：PUT /api/chat/config 对 backend/skill 做类型校验。

根因：update_chat_config 把 body["backend"] 原样透传 kwargs["backend"]
（chat.py:1581-1582）→ set_chat_config → _resolve_chat_runtime_id（str 形参）
→ TypeError → 500。GET /config 返回 backend 为对象形态 {"type": ...}，
契约错配由 PUT 侧显式校验收口。

修复语义（与 #504 分歧点：本 Issue 口径为显式拒绝）：
- backend 为字符串 → 200 正常保存（旧前端路径，行为不变）；
- backend 非字符串（对象/数组/数字/bool 等，含 GET 对象形态 {"type": ...}）→ 400 明确提示；
- skill 为非字符串 → 400；
- 空串/None/缺键 → 跳过写入（空 body 普通保存仍 200，#431 不变量保持）。
"""
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import db, db_lock

UID = 50801
USERNAME = "issue508_tester"


@pytest.fixture(autouse=True)
def _client():
    """隔离：快照并清空 agent_runtimes（含 conftest 种子），用例内确定性构造引擎；
    teardown 恢复快照，避免影响同批其他测试（模式同 test_chat_config_engine_selection）。"""
    from src.runtime.engine_select import _resolve_local_cache
    _resolve_local_cache.clear()
    with db_lock:
        cur = db.cursor()
        _rt_cols = [r[1] for r in cur.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        _rt_rows = cur.execute("SELECT * FROM agent_runtimes").fetchall()
        cur.execute("DELETE FROM agent_runtimes")
        db.commit()

    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    with db_lock:
        cur = db.cursor()
        cur.execute("DELETE FROM chat_agent_session WHERE user_id=?", (UID,))
        cur.execute("DELETE FROM agent_runtimes")
        for row in _rt_rows:
            cur.execute(
                f"INSERT INTO agent_runtimes ({','.join(_rt_cols)}) VALUES ({','.join('?' * len(_rt_cols))})", row)
        db.commit()
    _resolve_local_cache.clear()


def _mk_local_engine(name="opencode") -> int:
    """本机直连引擎（credential_id=0，健康）——保证 backend 字符串可解析落库。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope) "
            "VALUES (?, 'issue508_local', 0, 1, 1, 'online', 'global')", (name,))
        rid = c.lastrowid or 0
        db.commit()
        return rid


def _seed_config(runtime_id=0):
    """给 UID 种一条配置行（写路径 sess-cfg-{UID}，供"不落库"对照）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, runtime_id, skill, skill_id, created_at, last_active_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (f"sess-cfg-{UID}", UID, "", runtime_id, "", 0))
        db.commit()


def _cfg_runtime_id():
    with db_lock:
        return db.cursor().execute(
            "SELECT runtime_id FROM chat_agent_session WHERE id=?", (f"sess-cfg-{UID}",)).fetchone()


class TestBackendTypeValidation:
    """PUT /api/chat/config backend 类型校验各分支。"""

    def test_backend_object_rejected_400(self, _client):
        """AC：GET 对象形态 {"backend":{"type":"opencode"}} → 400 明确提示（非 500）。"""
        r = _client.put("/api/chat/config", json={"backend": {"type": "opencode"}})
        assert r.status_code == 400, f"对象形态 backend 应 400，得到 {r.status_code}: {r.text}"
        assert "backend" in r.text and "字符串" in r.text, f"错误提示应明确指向 backend 字符串要求: {r.text}"

    def test_backend_string_ok_200(self, _client):
        """AC：字符串 backend → 200 不回归（旧前端路径）。"""
        r = _client.put("/api/chat/config", json={"backend": "opencode"})
        assert r.status_code == 200, r.text
        assert r.json()["message"] == "配置已更新"

    def test_backend_malformed_all_400(self, _client):
        """AC：数组/数字/空对象/type 非字符串/float/bool → 一律 4xx。"""
        for bad in ({"backend": ["opencode"]},   # 数组
                    {"backend": 123},            # 数字
                    {"backend": {}},             # 空对象
                    {"backend": {"type": 123}},  # type 非字符串
                    {"backend": 1.5},            # float
                    {"backend": True}):          # bool
            r = _client.put("/api/chat/config", json=bad)
            assert r.status_code == 400, f"畸形 backend {bad['backend']!r} 应 400，得到 {r.status_code}: {r.text}"

    def test_backend_malformed_not_saved(self, _client):
        """AC：畸形 backend 400 后不落库——runtime_id 指针保持原值。"""
        rid = _mk_local_engine("seedengine")
        _seed_config(runtime_id=rid)
        r = _client.put("/api/chat/config", json={"backend": {"type": "opencode"}})
        assert r.status_code == 400, r.text
        row = _cfg_runtime_id()
        assert row and row[0] == rid, f"畸形输入不得改写配置指针: {row}"

    def test_backend_object_type_str_still_400(self, _client):
        """AC（分歧点钉住）：{"type":"opencode"} 形态也拒绝——本 Issue 口径显式 400，不归一落库。"""
        r = _client.put("/api/chat/config", json={"backend": {"type": "opencode"}})
        assert r.status_code == 400, r.text

    def test_empty_body_still_200(self, _client):
        """AC：空 body / backend 空串 → 200（跳过写入，#431 普通保存不变量）。"""
        r = _client.put("/api/chat/config", json={})
        assert r.status_code == 200, r.text
        r2 = _client.put("/api/chat/config", json={"backend": ""})
        assert r2.status_code == 200, r2.text

    def test_string_backend_saved_and_get_returns_normal(self, _client):
        """AC：字符串落库后 GET 返回正常（backend.type 为该字符串）。"""
        _mk_local_engine("opencode")
        r = _client.put("/api/chat/config", json={"backend": "opencode"})
        assert r.status_code == 200, r.text
        data = _client.get("/api/chat/config").json()
        assert data["backend"]["type"] == "opencode", f"GET 应回显字符串引擎名: {data['backend']}"


class TestSkillTypeValidation:
    """同根因区域 skill 字段一致防御（对齐 #504 修法范围）。"""

    def test_skill_non_string_rejected_400(self, _client):
        """AC：skill 非字符串（数组/对象/数字）→ 400，非 500。"""
        for bad in ({"skill": ["codereview"]},
                    {"skill": {"name": "codereview"}},
                    {"skill": 123}):
            r = _client.put("/api/chat/config", json=bad)
            assert r.status_code == 400, f"畸形 skill {bad['skill']!r} 应 400，得到 {r.status_code}: {r.text}"
            assert "skill" in r.text, f"错误提示应指向 skill 字段: {r.text}"

    def test_skill_string_path_unchanged(self, _client):
        """AC：字符串 skill 旧路径回归 → 200。"""
        r = _client.put("/api/chat/config", json={"skill": "codereview", "skill_scope": "official"})
        assert r.status_code == 200, r.text

    def test_skill_id_non_int_no_500(self, _client):
        """核对：skill_id 畸形（字符串/对象）经既有路径静默容错，不 500。"""
        r = _client.put("/api/chat/config", json={"skill_id": "abc"})
        assert r.status_code == 200, f"skill_id 畸形不应 500: {r.status_code}: {r.text}"
