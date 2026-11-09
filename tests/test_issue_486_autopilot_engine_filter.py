"""Issue #486: 自动化任务引擎选择按「仓库作用域 + 用户归属」过滤。

验证：
- _validate_engine_runtime_ids 复用 #277 可见性口径：
  全局 / 本人个人 / 当前仓库 repo 引擎放行；其他仓库 repo、他人个人、
  不存在 ID、已吊销凭据 → 400。
- POST /api/autopilot/tasks 与 PUT /api/autopilot/tasks/{id} 的
  engine_runtime_ids 服务端归属校验（API 层越权提交被拒）。
- 空列表（广播模式）不受影响。

参考 tests/test_issue_277_repo_isolation.py 模式。
"""
import pytest
from fastapi.testclient import TestClient

from src.session_manager import db, db_lock
from src.api.routes.autopilot import _validate_engine_runtime_ids


def _mk_cred(name, scope="global", owner_id="", owner_name="alice", repo_key="", status="active"):
    """创建 runtime_credentials 测试行，返回 credential_id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status, repo_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("h_" + name, name, "dev-" + name, scope, owner_id, owner_name, status, repo_key))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, cid, scope="global", owner_id=""):
    """创建 agent_runtimes 测试行，返回 runtime_id。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
            "last_seen_at, scope, owner_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, cid, "online", 1, 1,
             "2026-01-01T00:00:00+00:00", scope, owner_id))
        rid = c.lastrowid
        db.commit()
        return rid


def _seed_engines(prefix="i486"):
    """创建典型引擎矩阵，返回 dict。"""
    # 全局引擎
    cid_global = _mk_cred(f"{prefix}_glob", scope="global")
    rid_global = _mk_engine(f"{prefix}_glob_rt", cid_global, scope="global")
    # alice 的个人引擎
    cid_own = _mk_cred(f"{prefix}_own", scope="user", owner_name="alice")
    rid_own = _mk_engine(f"{prefix}_own_rt", cid_own, scope="user")
    # 仓库 A 的 repo 引擎（当前仓库）
    cid_repoa = _mk_cred(f"{prefix}_repoa", scope="repo",
                         owner_id="gitcode:alice/repo-a", owner_name="alice",
                         repo_key="gitcode:alice/repo-a")
    rid_repoa = _mk_engine(f"{prefix}_repoa_rt", cid_repoa, scope="repo",
                           owner_id="gitcode:alice/repo-a")
    # 仓库 B 的 repo 引擎（其他仓库，越权）
    cid_repob = _mk_cred(f"{prefix}_repob", scope="repo",
                         owner_id="gitcode:bob/repo-b", owner_name="bob",
                         repo_key="gitcode:bob/repo-b")
    rid_repob = _mk_engine(f"{prefix}_repob_rt", cid_repob, scope="repo",
                           owner_id="gitcode:bob/repo-b")
    # bob 的个人引擎（他人个人，越权）
    cid_bob = _mk_cred(f"{prefix}_bob", scope="user", owner_name="bob")
    rid_bob = _mk_engine(f"{prefix}_bob_rt", cid_bob, scope="user")
    # 已吊销凭据引擎
    cid_revoked = _mk_cred(f"{prefix}_rev", scope="repo",
                           owner_id="gitcode:alice/repo-a", owner_name="alice",
                           repo_key="gitcode:alice/repo-a", status="revoked")
    rid_revoked = _mk_engine(f"{prefix}_rev_rt", cid_revoked, scope="repo",
                             owner_id="gitcode:alice/repo-a")
    return {
        "global": rid_global, "own": rid_own, "repo_a": rid_repoa,
        "repo_b": rid_repob, "bob_user": rid_bob, "revoked": rid_revoked,
    }


@pytest.fixture(autouse=True)
def _clean_tables():
    _p = "i486_%"
    _clean_sql = [
        "DELETE FROM agent_runtimes WHERE name LIKE ? OR credential_id IN "
        "(SELECT id FROM runtime_credentials WHERE name LIKE ?)",
        "DELETE FROM runtime_credentials WHERE name LIKE ?",
        "DELETE FROM autopilot_tasks WHERE name LIKE 'i486_task_%'",
        "DELETE FROM autopilot_executions WHERE task_id NOT IN (SELECT id FROM autopilot_tasks)",
    ]
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            c.execute(sql, (_p,) * sql.count("?"))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            c.execute(sql, (_p,) * sql.count("?"))
        db.commit()


@pytest.fixture
def api_client():
    """注入当前用户（alice，session_token 作用域跳过 repo 访问校验）的 TestClient。"""
    from src.api.deps import get_current_user
    from src.api.main import app
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 0, "username": "alice", "auth_type": "session_token"}
    try:
        with TestClient(app) as cli:
            yield cli
    finally:
        app.dependency_overrides.pop(get_current_user, None)


class TestValidateEngineRuntimeIds:
    """_validate_engine_runtime_ids 单元测试（Issue #486 核心口径）。"""

    def test_global_user_and_current_repo_engines_allowed(self):
        """全局 / 本人个人 / 当前仓库 repo 引擎 → 放行。"""
        rids = _seed_engines()
        user = {"id": 0, "username": "alice"}
        # 不抛异常即通过
        _validate_engine_runtime_ids(user, "gitcode:alice/repo-a",
                                     [rids["global"], rids["own"], rids["repo_a"]])

    def test_other_repo_engine_rejected(self):
        """仓库 B 的 repo 引擎 → 400（跨仓库越权，Issue #486 核心场景）。"""
        from fastapi import HTTPException
        rids = _seed_engines()
        user = {"id": 0, "username": "alice"}
        with pytest.raises(HTTPException) as ei:
            _validate_engine_runtime_ids(user, "gitcode:alice/repo-a", [rids["repo_b"]])
        assert ei.value.status_code == 400
        assert str(rids["repo_b"]) in ei.value.detail["message"]

    def test_other_user_personal_engine_rejected(self):
        """他人个人（user scope）引擎 → 400。"""
        from fastapi import HTTPException
        rids = _seed_engines()
        user = {"id": 0, "username": "alice"}
        with pytest.raises(HTTPException) as ei:
            _validate_engine_runtime_ids(user, "gitcode:alice/repo-a", [rids["bob_user"]])
        assert ei.value.status_code == 400

    def test_missing_runtime_id_rejected(self):
        """不存在的 runtime id → 400。"""
        from fastapi import HTTPException
        _seed_engines()
        user = {"id": 0, "username": "alice"}
        with pytest.raises(HTTPException) as ei:
            _validate_engine_runtime_ids(user, "gitcode:alice/repo-a", [99999999])
        assert ei.value.status_code == 400

    def test_revoked_credential_rejected(self):
        """凭据已吊销的引擎 → 400（与引擎列表可见口径一致）。"""
        from fastapi import HTTPException
        rids = _seed_engines()
        user = {"id": 0, "username": "alice"}
        with pytest.raises(HTTPException) as ei:
            _validate_engine_runtime_ids(user, "gitcode:alice/repo-a", [rids["revoked"]])
        assert ei.value.status_code == 400

    def test_empty_list_broadcast_not_validated(self):
        """空列表 = 广播模式 → 不校验（行为保持不变）。"""
        _seed_engines()
        user = {"id": 0, "username": "alice"}
        _validate_engine_runtime_ids(user, "gitcode:alice/repo-a", [])

    def test_no_repo_key_keeps_user_scope_visibility(self):
        """repo_key 为空（无仓库任务）→ repo_filter 为空，走用户仓库集合口径（旧行为）。"""
        rids = _seed_engines()
        user = {"id": 0, "username": "alice"}
        # 全局 + 本人引擎仍然放行
        _validate_engine_runtime_ids(user, "", [rids["global"], rids["own"]])


class TestAutopilotTaskApiEngineFilter:
    """POST/PUT /api/autopilot/tasks 的 engine_runtime_ids 服务端校验。"""

    def _body(self, rids, name="i486_task_1"):
        return {
            "name": name,
            "repo_key": "gitcode:alice/repo-a",
            "instruction": "run daily check",
            "cron": "0 9 * * *",
            "max_retry": 3,
            "engine_runtime_ids": rids,
        }

    def test_create_with_authorized_engines_succeeds(self, api_client):
        """创建任务绑定当前仓库可见引擎 → 200。"""
        rids = _seed_engines()
        r = api_client.post("/api/autopilot/tasks", json=self._body(
            [rids["global"], rids["own"], rids["repo_a"]]))
        assert r.status_code == 200, r.text

    def test_create_with_cross_repo_engine_rejected(self, api_client):
        """创建任务绑定仓库 B 引擎 → 400（Issue #486 验收标准）。"""
        rids = _seed_engines()
        r = api_client.post("/api/autopilot/tasks", json=self._body([rids["repo_b"]]))
        assert r.status_code == 400, r.text
        assert str(rids["repo_b"]) in r.json()["detail"]["message"]

    def test_create_with_other_user_engine_rejected(self, api_client):
        """创建任务绑定他人个人引擎 → 400。"""
        rids = _seed_engines()
        r = api_client.post("/api/autopilot/tasks", json=self._body([rids["bob_user"]]))
        assert r.status_code == 400, r.text

    def test_create_broadcast_empty_list_unaffected(self, api_client):
        """创建任务空列表（广播）→ 200，行为不变。"""
        _seed_engines()
        r = api_client.post("/api/autopilot/tasks", json=self._body([]))
        assert r.status_code == 200, r.text

    def test_update_with_cross_repo_engine_rejected(self, api_client):
        """更新任务绑定仓库 B 引擎 → 400。"""
        rids = _seed_engines()
        r = api_client.post("/api/autopilot/tasks", json=self._body(
            [rids["repo_a"]], name="i486_task_upd"))
        assert r.status_code == 200, r.text
        task_id = r.json()["id"]
        r2 = api_client.put(f"/api/autopilot/tasks/{task_id}", json={
            "engine_runtime_ids": [rids["repo_b"]]})
        assert r2.status_code == 400, r2.text

    def test_update_with_authorized_engine_succeeds(self, api_client):
        """更新任务绑定当前仓库引擎 → 200。"""
        rids = _seed_engines()
        r = api_client.post("/api/autopilot/tasks", json=self._body(
            [rids["repo_a"]], name="i486_task_upd2"))
        assert r.status_code == 200, r.text
        task_id = r.json()["id"]
        r2 = api_client.put(f"/api/autopilot/tasks/{task_id}", json={
            "engine_runtime_ids": [rids["global"], rids["own"]]})
        assert r2.status_code == 200, r2.text

    def test_update_without_engine_field_unaffected(self, api_client):
        """更新不提交 engine_runtime_ids（None）→ 不校验，正常更新。"""
        rids = _seed_engines()
        r = api_client.post("/api/autopilot/tasks", json=self._body(
            [rids["repo_a"]], name="i486_task_upd3"))
        assert r.status_code == 200, r.text
        task_id = r.json()["id"]
        r2 = api_client.put(f"/api/autopilot/tasks/{task_id}", json={
            "name": "i486_task_upd3_renamed"})
        assert r2.status_code == 200, r2.text
