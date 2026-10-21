"""Issue #400：chat agent 引擎选择单选语义 — 后端候选去重/selectedEngine/指针一致性测试。

覆盖（对应 Acceptance Criteria）：
- 候选按唯一键去重（user/repo 双档、历史同实例重复行合并为单条）
- GET /api/chat/config 返回唯一 selectedEngine（对象，绝不数组），
  selectedEngine.runtime_id == 会话引擎指针（chat_agent_session.runtime_id）
- 本机直连 + 个人实例并存时：选中唯一（以 runtime_id 精确匹配，不做前缀匹配）
- 指针指向个人实例时 selectedEngine.name == 实例名（实际引擎可感知）
"""
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import db, db_lock

UID = 40001
USERNAME = "issue400_tester"


@pytest.fixture(autouse=True)
def _client():
    """隔离：快照并清空 agent_runtimes / runtime_credentials（含 conftest 种子），
    用例内确定性构造引擎；teardown 恢复快照，避免影响同批其他测试。"""
    from src.runtime.engine_select import _resolve_local_cache
    _resolve_local_cache.clear()
    with db_lock:
        cur = db.cursor()
        _rt_cols = [r[1] for r in cur.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        _rc_cols = [r[1] for r in cur.execute("PRAGMA table_info(runtime_credentials)").fetchall()]
        _rt_rows = cur.execute("SELECT * FROM agent_runtimes").fetchall()
        _rc_rows = cur.execute("SELECT * FROM runtime_credentials").fetchall()
        cur.execute("DELETE FROM agent_runtimes")
        cur.execute("DELETE FROM runtime_credentials")
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
        cur.execute("DELETE FROM chat_conversations WHERE user_id=?", (UID,))
        cur.execute("DELETE FROM chat_agent_session WHERE user_id=?", (UID,))
        cur.execute("DELETE FROM agent_runtimes")
        cur.execute("DELETE FROM runtime_credentials")
        # 先恢复凭据（agent_runtimes.credential_id 引用），再恢复引擎行
        for row in _rc_rows:
            cur.execute(
                f"INSERT INTO runtime_credentials ({','.join(_rc_cols)}) VALUES ({','.join('?' * len(_rc_cols))})", row)
        for row in _rt_rows:
            cur.execute(
                f"INSERT INTO agent_runtimes ({','.join(_rt_cols)}) VALUES ({','.join('?' * len(_rt_cols))})", row)
        db.commit()
    _resolve_local_cache.clear()


def _mk_local_engine(name="opencode") -> int:
    """本机直连引擎（credential_id=0，健康）"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope) "
            "VALUES (?, 'issue400_local', 0, 1, 1, 'online', 'global')", (name,))
        rid = c.lastrowid
        db.commit()
        return rid


def _mk_user_engine(name="opencode", device="my-mac", owner=USERNAME) -> int:
    """个人引擎（scope=user，owner_name=USERNAME，active）；每次创建唯一凭据名。"""
    import uuid as _uuid
    cred_name = f"issue400_cred_{_uuid.uuid4().hex[:10]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?, ?, ?, 'user', ?, 'active')",
            ("h" + _uuid.uuid4().hex, cred_name, device, owner))
        cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope) "
            "VALUES (?, 'issue400_user', ?, 1, 1, 'online', 'user')", (name, cid))
        rid = c.lastrowid
        db.commit()
        return rid


def _set_cfg_runtime_id(rid):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, runtime_id, created_at, last_active_at) "
            "VALUES (?,?,?,?,datetime('now'),datetime('now'))",
            (f"sess-cfg-{UID}", UID, "", rid))
        db.commit()


def _get_cfg_repr(cfg):
    """与 route 内 _chat_selected_engine 相同入参：cfg dict + backends 列表"""
    from src.api.routes.chat import _chat_selected_engine
    return _chat_selected_engine(cfg, [])


class TestSelectedEngineUnique:
    def test_single_selected_when_local_and_personal_coexist(self, _client):
        """本机直连 opencode + 个人实例 opencode-my-mac 并存：仅一个 selectedEngine，且按 runtime_id 唯一命中。"""
        local_rid = _mk_local_engine("opencode")
        user_rid = _mk_user_engine("opencode", device="my-mac")
        # 会话指针 = 本机直连（未显式配置时的兜底物化）
        _set_cfg_runtime_id(local_rid)

        r = _client.get("/api/chat/config")
        assert r.status_code == 200
        data = r.json()
        backends = data["_backends"]
        assert len(backends) == 2, f"应包含本机直连+个人实例两条，实际 {backends}"
        sel = data["selectedEngine"]
        assert isinstance(sel, dict), "selectedEngine 必须为单一对象"
        assert sel["runtime_id"] == local_rid, "指针命中本机直连，selectedEngine 应指向它"
        # 唯一性：选中项在列表中有且仅有一个匹配
        hits = [b for b in backends if b["runtime_id"] == sel["runtime_id"]]
        assert len(hits) == 1

    def test_selected_engine_is_personal_instance_when_pointer_points_user(self, _client):
        """会话指针指向个人实例：selectedEngine.name 必须为实例名（实际引擎可感知），而非类型名。"""
        _mk_local_engine("opencode")
        user_rid = _mk_user_engine("opencode", device="my-mac")
        _set_cfg_runtime_id(user_rid)  # 会话指针 = 个人实例

        r = _client.get("/api/chat/config")
        data = r.json()
        sel = data["selectedEngine"]
        assert sel is not None and sel["runtime_id"] == user_rid
        assert sel["name"] == "opencode-my-mac", f"应回显实例名，实际 {sel.get('name')}"
        assert sel["scope"] == "user"
        # 精确命中：列表内只有一条（个人实例）被选中
        active = [b for b in data["_backends"] if b["runtime_id"] == user_rid]
        assert len(active) == 1

    def test_backend_type_name_never_marks_multiple(self, _client):
        """cfg.backend 为类型名 'opencode' 时：selectedEngine 仍由 runtime_id 权威解析（不会两个都选）。"""
        local_rid = _mk_local_engine("opencode")
        _mk_user_engine("opencode", device="my-mac")
        # chat_agent_session 无 backend 列：cfg.backend 由 _chat_config_row_engine 从 runtime_id 派生为类型名 'opencode'
        _set_cfg_runtime_id(local_rid)
        r = _client.get("/api/chat/config")
        data = r.json()
        sel = data["selectedEngine"]
        assert sel is not None and sel["runtime_id"] == local_rid
        # 模拟前端单选判定：selectedEngine 精确匹配，不依赖 backend.type 前缀
        active = [b for b in data["_backends"] if b["runtime_id"] == sel["runtime_id"]]
        assert len(active) == 1

    def test_no_engine_returns_none(self, _client):
        """无任何引擎 → selectedEngine=None（前端显示不可用）"""
        r = _client.get("/api/chat/config")
        data = r.json()
        assert data["selectedEngine"] is None
        assert "_backends" in data

    def test_dedupe_same_instance_merged(self, _client):
        """同一实例（同 device）user 双行重复：_backends 合并为单条（候选唯一键归一）。"""
        rid1 = _mk_user_engine("opencode", device="dup-01")
        rid2 = _mk_user_engine("opencode", device="dup-01")  # 同 device → 同 instance_name
        _set_cfg_runtime_id(rid1)

        r = _client.get("/api/chat/config")
        data = r.json()
        user_backends = [b for b in data["_backends"] if b.get("scope") == "user"]
        names = [b.get("name") for b in user_backends]
        assert len(user_backends) == 1, f"同实例应合并为一条，实际 {names}"
        assert names == ["opencode-dup-01"]
        # 选中落在保留的那条上
        assert data["selectedEngine"]["runtime_id"] == rid1


class TestChatSelectedEngineHelper:
    def test_runtime_id_hit_in_backends(self):
        cfg = {"runtime_id": 7, "backend": "opencode"}
        backends = [{"runtime_id": 7, "name": "opencode-my-mac", "scope": "user"},
                    {"runtime_id": 3, "name": "opencode", "scope": "local"}]
        from src.api.routes.chat import _chat_selected_engine
        sel = _chat_selected_engine(cfg, backends)
        assert sel["runtime_id"] == 7 and sel["name"] == "opencode-my-mac"

    def test_no_runtime_id_exact_name_match(self):
        cfg = {"runtime_id": 0, "backend": "opencode"}
        backends = [{"runtime_id": 3, "name": "opencode", "scope": "local"}]
        from src.api.routes.chat import _chat_selected_engine
        sel = _chat_selected_engine(cfg, backends)
        assert sel is not None and sel["runtime_id"] == 3

    def test_no_prefix_match_avoids_multi_hit(self):
        """个人实例 'opencode-fff' 不应命中 backend='opencode'（前缀宽松匹配被移除）"""
        cfg = {"runtime_id": 0, "backend": "opencode"}
        backends = [{"runtime_id": 5, "name": "opencode-fff", "scope": "user"},
                    {"runtime_id": 6, "name": "opencode", "scope": "local"}]
        from src.api.routes.chat import _chat_selected_engine
        sel = _chat_selected_engine(cfg, backends)
        assert sel is not None and sel["runtime_id"] == 6, "精确匹配应命中本机直连，而非前缀同名的个人实例"


class TestChatDedupeBackends:
    def test_merge_duplicate_entries(self):
        from src.api.routes.chat import _chat_dedupe_backends
        backends = [
            {"name": "opencode", "scope": "local", "runtime_id": 1},
            {"name": "opencode", "scope": "local", "runtime_id": 2},  # 同键 → 合并
            {"name": "opencode-my-mac", "scope": "user", "runtime_id": 3},
        ]
        out = _chat_dedupe_backends(backends)
        assert len(out) == 2
        rids = [b["runtime_id"] for b in out]
        assert rids == [1, 3]

    def test_local_vs_user_instance_kept_separate(self):
        from src.api.routes.chat import _chat_dedupe_backends
        backends = [
            {"name": "opencode", "scope": "local", "runtime_id": 1},
            {"name": "opencode-my-mac", "scope": "user", "runtime_id": 2},
        ]
        out = _chat_dedupe_backends(backends)
        assert len(out) == 2, "本机直连类型名 与 个人实例名 是不同引擎，必须保留两条"