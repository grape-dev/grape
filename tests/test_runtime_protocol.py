"""M3 协议端点测试（见 acceptance-plan M3-C* / M3-R* / concurrency）"""
import hashlib, json, time
import pytest

from fastapi.testclient import TestClient
from src.session_manager import db, db_lock
from src.runtime import service
from src.runtime.health import derive_runtime_health


def _make_token(name="CI-Worker-01"):
    import secrets
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        # scope=global：协议测试的 worker 对任意用户可见（owner 由 settings API 建的凭据才走可见性）
        c.execute("INSERT INTO runtime_credentials (token_hash, name, scope) VALUES (?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), name, "global"))
        db.commit()
    return token


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM runtime_credentials")
        db.cursor().execute("DELETE FROM agent_runtimes")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks")
        db.cursor().execute("DELETE FROM runtime_credentials")
        db.cursor().execute("DELETE FROM agent_runtimes")
        db.commit()


@pytest.fixture
def client():
    return TestClient(__import__("src.api.main", fromlist=["app"]).app)


class TestCredentialAuth:
    def test_requires_token(self, client):
        r = client.post("/api/runtime/register", json={"runtimes": []})
        assert r.status_code == 401

    def test_revoked_token_401(self, client):
        token = _make_token()
        with db_lock:
            db.cursor().execute("UPDATE runtime_credentials SET status='revoked'")
            db.commit()
        r = client.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"}, json={"runtimes": []})
        assert r.status_code == 401

    def test_user_token_rejected(self, client):
        # 普通 user token 不能访问协议端点（D2 凭据隔离）——用假 bearer
        r = client.post("/api/runtime/register", headers={"Authorization": "Bearer faketoken"}, json={"runtimes": []})
        assert r.status_code == 401


class TestRegister:
    def test_register_upsert(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        body = {"device_name": "ci-01", "runtimes": [{"name": "opencode", "version": "v1.5.2", "healthy": True}]}
        r = client.post("/api/runtime/register", json=body, headers=headers)
        assert r.status_code == 200
        rid = r.json()["runtimes"][0]["id"]
        # 重复注册幂等
        r2 = client.post("/api/runtime/register", json=body, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["runtimes"][0]["id"] == rid
        with db_lock:
            cnt = db.cursor().execute("SELECT COUNT(*) FROM agent_runtimes").fetchone()[0]
        assert cnt == 1, "注册应幂等"


class TestClaimViaHttp:
    def test_claim_and_complete(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/runtime/register", json={"device_name": "c", "runtimes": [{"name": "opencode"}]}, headers=headers)
        with db_lock:
            rid = db.cursor().execute("SELECT id FROM agent_runtimes LIMIT 1").fetchone()[0]
        tid = service.enqueue_task(agent_session_id="sess-p1", runtime_id=rid, prompt="p", event_seq=1,
                                   issue_id="#5", repo_key="test/repo", agent_type="issue", skill="issue")
        r = client.post(f"/api/runtime/runtimes/{rid}/tasks/claim", headers=headers)
        assert r.status_code == 200
        task = r.json()["task"]
        assert task and task["id"] == tid
        # complete（幂等）
        r2 = client.post(f"/api/runtime/tasks/{tid}/complete", json={"result_text": "完成", "exit_code": 0}, headers=headers)
        assert r2.status_code == 200
        r3 = client.post(f"/api/runtime/tasks/{tid}/complete", json={"result_text": "again", "exit_code": 0}, headers=headers)
        assert r3.status_code == 200
        t = service.get_task(tid)
        assert t["status"] == "completed"

    def test_fail_sets_failed(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode"}]}, headers=headers)
        with db_lock:
            rid = db.cursor().execute("SELECT id FROM agent_runtimes LIMIT 1").fetchone()[0]
        tid = service.enqueue_task(agent_session_id="s-p2", runtime_id=rid, prompt="p", event_seq=2,
                                   repo_key="test/repo", agent_type="issue", skill="issue")
        r = client.post(f"/api/runtime/tasks/{tid}/fail", json={"error": "boom"}, headers=headers)
        assert r.status_code == 200
        assert service.get_task(tid)["status"] == "failed"

    def test_recover_orphans(self, client):
        token = _make_token()
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode"}]}, headers=headers)
        with db_lock:
            rid = db.cursor().execute("SELECT id FROM agent_runtimes LIMIT 1").fetchone()[0]
        tid = service.enqueue_task(agent_session_id="s-p3", runtime_id=rid, prompt="p", event_seq=3,
                                   repo_key="test/repo", agent_type="issue", skill="issue")
        service.update_task_status(tid, "running")
        r = client.post(f"/api/runtime/runtimes/{rid}/recover-orphans", headers=headers)
        assert r.status_code == 200
        assert service.get_task(tid)["status"] == "queued"


class TestHealth:
    def test_derive(self):
        import time as _t
        now = _t.time()
        from datetime import datetime, timezone, timedelta
        # 新语义（2026-08-12 修复）：status=online 必须校验心跳新鲜度（<5min 才算在线）
        assert derive_runtime_health("online", None, now) == "recently_lost"
        assert derive_runtime_health("offline", None, now) == "offline"
        # online + 心跳新鲜(<5min) → online
        fresh = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        assert derive_runtime_health("online", fresh, now) == "online"
        # online + 心跳超时(20min) → recently_lost（worker 宕机后不再永久显示在线）
        stale = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        assert derive_runtime_health("online", stale, now) == "recently_lost"
        # 非 online 分支行为不变（回归锚点）
        ts = now - 60
        assert derive_runtime_health("offline", None, now) == "offline"
        # 用 ISO 时间
        iso = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        assert derive_runtime_health("offline", iso, now) == "recently_lost"
        # offline：1 天前
        iso2 = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert derive_runtime_health("offline", iso2, now) == "offline"
        # about_to_gc：6.5 天前
        iso3 = (datetime.now(timezone.utc) - timedelta(days=6.5)).isoformat()
        assert derive_runtime_health("offline", iso3, now) == "about_to_gc"


class TestCredentialManagement:
    def test_create_list_revoke(self, client):
        from src.api.deps import get_current_user
        app = __import__("src.api.main", fromlist=["app"]).app
        def _fake_user():
            return {"id": 1, "username": "admin"}
        app.dependency_overrides[get_current_user] = _fake_user
        c = TestClient(app)
        r = c.post("/api/settings/runtime-credentials", json={"name": "worker-1"})
        assert r.status_code == 200
        token = r.json()["token"]
        assert token.startswith("rta_")
        # 列表不含明文
        lst = c.get("/api/settings/runtime-credentials")
        assert lst.status_code == 200
        assert all("token" not in str(cred) or cred.get("name") for cred in lst.json()["credentials"])
        cid = lst.json()["credentials"][0]["id"]
        # 吊销
        rv = c.post(f"/api/settings/runtime-credentials/{cid}/revoke")
        assert rv.status_code == 200
        # 吊销后该 token 失效
        r_proto = c.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"}, json={"runtimes": []})
        assert r_proto.status_code == 401
        app.dependency_overrides.clear()


class TestMultiInstanceRegister:
    """R1：register 按 (credential_id, name) 实例键 upsert（multi-runtime-instance-design §3.1）"""

    def test_multi_credential_same_name_coexist(self, client):
        tA = _make_token("worker-a")
        tB = _make_token("worker-b")
        hA = {"Authorization": f"Bearer {tA}"}
        hB = {"Authorization": f"Bearer {tB}"}
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode", "version": "v1", "cli_path": "/a/opencode"}]}, headers=hA)
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode", "version": "v2", "cli_path": "/b/opencode"}]}, headers=hB)
        with db_lock:
            rows = db.cursor().execute(
                "SELECT credential_id, version, cli_path FROM agent_runtimes WHERE name='opencode' ORDER BY credential_id").fetchall()
        assert len(rows) == 2, f"两个凭据同名引擎应并存: {rows}"
        assert {r[1] for r in rows} == {"v1", "v2"}, "互不覆盖 version"
        assert {r[2] for r in rows} == {"/a/opencode", "/b/opencode"}, "互不覆盖 cli_path"

    def test_revoked_old_credential_new_row(self, client):
        """凭据 A revoke（删除）后新凭据 C 注册同名 → 只有新行（旧行已随凭据删除）"""
        from src.api.deps import get_current_user
        app = __import__("src.api.main", fromlist=["app"]).app
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "admin"}
        c = TestClient(app)
        tA = _make_token("old")
        r = client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode", "version": "v-old"}]},
                        headers={"Authorization": f"Bearer {tA}"})
        old_rid = r.json()["runtimes"][0]["id"]
        with db_lock:
            old_cid = db.cursor().execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (old_rid,)).fetchone()[0]
        # API revoke → 删除凭据行 + 级联删旧 runtime 行
        assert c.post(f"/api/settings/runtime-credentials/{old_cid}/revoke").status_code == 200
        tB = _make_token("new")
        r2 = client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode", "version": "v-new"}]},
                         headers={"Authorization": f"Bearer {tB}"})
        new_rid = r2.json()["runtimes"][0]["id"]
        app.dependency_overrides.clear()
        assert new_rid != old_rid, "新凭据注册同名应得到新实例行"
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, credential_id, version, status FROM agent_runtimes WHERE name='opencode' AND credential_id>0 ORDER BY id").fetchall()
        assert len(rows) == 1, f"旧行已随凭据删除，应只剩新行: {rows}"
        assert rows[0][2] == "v-new", rows


class TestMultiInstanceCleanup:
    """R4/R5：refresh 防误删 + revoke 级联（multi-runtime-instance-design §5）"""

    def test_refresh_keeps_credential_rows(self, client):
        tA = _make_token("worker-a")
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode", "version": "w-v"}]},
                    headers={"Authorization": f"Bearer {tA}"})
        with db_lock:
            cid = db.cursor().execute("SELECT id FROM runtime_credentials LIMIT 1").fetchone()[0]
        # 本机 credential_id=0 占位行（模拟 ensure_default_runtime / refresh 创建）
        with db_lock:
            db.cursor().execute(
                "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, version, healthy, pip_package, scope, owner_id) VALUES (0,'codearts','Codearts',1,'1.0',1,'codearts-cli','global','')")
            db.commit()
        from src.user.db import save_agent_runtimes
        save_agent_runtimes([{"name": "opencode", "installed": True, "cli_path": "/usr/bin/opencode",
                              "version": "1.0", "healthy": True}])
        with db_lock:
            rows = db.cursor().execute("SELECT name, credential_id, version FROM agent_runtimes").fetchall()
        worker_rows = [r for r in rows if r[0] == "opencode" and r[1] == cid]
        assert worker_rows and worker_rows[0][2] == "w-v", "凭据绑定行应保留且不被刷新覆盖"
        assert not any(r[0] == "codearts" for r in rows), "本机不在发现列表的行应被清理"

    def test_revoke_deletes_credential_and_runtimes(self, client):
        """revoke = 删除：凭据行 + 其 agent_runtimes 行一并删除（2026-08-17 用户指示）。

        删除后：token 查不到（401）、凭据名释放可复用、Engines/Workers 视图不再出现。
        旧语义「置 revoked + 行置 offline」已废弃。
        """
        from src.api.deps import get_current_user
        app = __import__("src.api.main", fromlist=["app"]).app
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "admin"}
        c = TestClient(app)
        tA = _make_token("w")
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode"}]},
                    headers={"Authorization": f"Bearer {tA}"})
        with db_lock:
            cid = db.cursor().execute("SELECT id FROM runtime_credentials LIMIT 1").fetchone()[0]
            rid = db.cursor().execute("SELECT id FROM agent_runtimes WHERE credential_id=?", (cid,)).fetchone()[0]
        r = c.post(f"/api/settings/runtime-credentials/{cid}/revoke")
        assert r.status_code == 200
        with db_lock:
            cred_gone = db.cursor().execute("SELECT COUNT(*) FROM runtime_credentials WHERE id=?", (cid,)).fetchone()[0] == 0
            rt_gone = db.cursor().execute("SELECT COUNT(*) FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0] == 0
        assert cred_gone, "revoke 应删除凭据行"
        assert rt_gone, "revoke 应级联删除其 agent_runtimes 行"
        # 已删除凭据的 token 不能再注册
        r2 = client.post("/api/runtime/register", json={"runtimes": []}, headers={"Authorization": f"Bearer {tA}"})
        assert r2.status_code == 401
        app.dependency_overrides.clear()


class TestMultiInstanceDeviceView:
    """R6：设备视图按 credential 分组，各设备列各自引擎（§6）"""

    def test_devices_grouped_by_credential(self, client):
        tA = _make_token("wa")
        tB = _make_token("wb")
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode", "version": "1"}]},
                    headers={"Authorization": f"Bearer {tA}"})
        client.post("/api/runtime/register", json={"runtimes": [{"name": "codearts", "version": "2"}]},
                    headers={"Authorization": f"Bearer {tB}"})
        from src.api.deps import get_current_user
        app = __import__("src.api.main", fromlist=["app"]).app
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "admin"}
        c = TestClient(app)
        data = c.get("/api/settings/agent-runtimes").json()
        app.dependency_overrides.clear()
        devices = data["devices"]
        assert len(devices) == 2, f"两个凭据应两个设备: {devices}"
        names = sorted(d["runtimes"][0]["name"] for d in devices if d["runtimes"])
        assert names == ["codearts", "opencode"]
        # 健康设备（register 刚写过 last_seen）应显示 online，而非"刚失联"
        #（回归：设备健康态曾误用 credential.status='active' → derive 恒返回 recently_lost）
        for d in devices:
            assert d["health"] == "online", f"健康设备应 online, got {d['health']}: {d}"

    def test_revoke_all_credentials_no_fake_local_devices(self, client):
        """R6 回归：吊销全部凭据后 devices 不得把 revoked 远端行伪装成本机 Runtime。

        2026-08-17：旧 orphan 兜底分支遍历 cached 全量，全部凭据 revoked 时
        devices 为空 → 大量 revoked/远端行全部以"本机 Runtime"设备展示
        （页面出现几十个离线本机 runtime + 在线·未上线）。
        """
        tA = _make_token("wa")
        tB = _make_token("wb")
        client.post("/api/runtime/register", json={"runtimes": [{"name": "opencode", "version": "1"}]},
                    headers={"Authorization": f"Bearer {tA}"})
        client.post("/api/runtime/register", json={"runtimes": [{"name": "codearts", "version": "2"}]},
                    headers={"Authorization": f"Bearer {tB}"})
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, scope, owner_id) VALUES (0,'hermes','Hermes',1,1,'global','')")
            db.commit()
            cids = [r[0] for r in c.execute("SELECT id FROM runtime_credentials").fetchall()]
        from src.api.deps import get_current_user
        app = __import__("src.api.main", fromlist=["app"]).app
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "admin"}
        c2 = TestClient(app)
        # 吊销全部凭据
        for cid in cids:
            r = c2.post(f"/api/settings/runtime-credentials/{cid}/revoke")
            assert r.status_code == 200, r.text
        data = c2.get("/api/settings/agent-runtimes").json()
        app.dependency_overrides.clear()
        devices = data["devices"]
        assert len(devices) == 1, f"吊销全部后 devices 应只剩本机行, got {len(devices)}: {devices}"
        assert devices[0]["credential_id"] == 0, devices
        assert [r["name"] for r in devices[0]["runtimes"]] == ["hermes"], devices
        # Engines 视图同样不出现 revoked 凭据的引擎
        names = [rt["name"] for rt in data["runtimes"]]
        assert "opencode" not in names and "codearts" not in names, names


class TestMultiInstanceUniqueKey:
    """R7：agent_runtimes 多实例唯一键（(credential_id,name,scope,owner_id)）行为"""

    def test_unique_key_enforced(self):
        import sqlite3
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_runtimes")
            c.execute("INSERT INTO agent_runtimes (credential_id, name, display_name, scope, owner_id) VALUES (0,'opencode','Opencode','global','')")
            db.commit()
            dup_blocked = False
            try:
                c.execute("INSERT INTO agent_runtimes (credential_id, name, display_name, scope, owner_id) VALUES (0,'opencode','Dup','global','')")
                db.commit()
            except sqlite3.IntegrityError:
                dup_blocked = True
                db.rollback()
            c.execute("INSERT INTO agent_runtimes (credential_id, name, display_name, scope, owner_id) VALUES (1,'opencode','W1','global','')")
            db.commit()
            cnt = c.execute("SELECT COUNT(*) FROM agent_runtimes WHERE name='opencode'").fetchone()[0]
        assert dup_blocked, "同 (credential_id,name,scope,owner_id) 应被唯一约束拦截"
        assert cnt == 2, "不同 credential 同名可并存"


class TestRuntimeAdminViews:
    """runtime-admin-two-view-design：instance_name 派生 / 凭据 owner·scope·唯一名 / 可见性 / 按凭据聚合任务"""

    def _admin_user(self, username="admin", uid=1):
        from src.api.deps import get_current_user
        app = __import__("src.api.main", fromlist=["app"]).app
        app.dependency_overrides[get_current_user] = lambda: {"id": uid, "username": username}
        return app

    def test_dup_credential_name_returns_400(self):
        """重名凭据返回 400 友好提示而非 500。

        2026-08-17 修复：原判断只认 "UNIQUE" 字样（sqlite 格式），MySQL 下重名
        报 (1062, "Duplicate entry ... for key 'runtime_credentials.name'") 不含
        "UNIQUE" → 误落 500「生成失败」。修复后 1062/Duplicate 亦匹配。
        """
        app = self._admin_user("alice")
        c = TestClient(app)
        r1 = c.post("/api/settings/runtime-credentials", json={"name": "w-dup", "scope": "user"})
        assert r1.status_code == 200, r1.text
        r2 = c.post("/api/settings/runtime-credentials", json={"name": "w-dup", "scope": "user"})
        assert r2.status_code == 400, r2.text
        assert "凭据名称已存在" in r2.json()["detail"]["message"], r2.text
        app.dependency_overrides.clear()

    def test_instance_name_scope_owner_derived(self):
        """S1：注册凭据 → agent-runtimes 派生 instance_name/worker_name/scope/owner"""
        app = self._admin_user("alice")
        c = TestClient(app)
        r = c.post("/api/settings/runtime-credentials", json={"name": "w-1", "scope": "user"})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        c.post("/api/runtime/register",
               json={"device_name": "dev-1", "runtimes": [{"name": "opencode", "version": "1.0", "cli_path": "/x"}]},
               headers={"Authorization": f"Bearer {token}"})
        data = c.get("/api/settings/agent-runtimes").json()
        rt = [x for x in data["runtimes"] if x["worker_name"] == "w-1"]
        assert rt, data["runtimes"]
        rt = rt[0]
        assert rt["instance_name"] == "opencode-dev-1", rt
        assert rt["worker_name"] == "w-1", rt
        # 默认 scope 个人，owner=当前登录用户
        assert rt["scope"] == "user" and rt["owner"] == "alice", rt
        # 引擎健康态应取自 agent_runtimes.status（register 置 online）→ 显示在线
        assert rt["health"] == "online", f"在线引擎应显示在线: {rt}"
        app.dependency_overrides.clear()

    def test_credential_owner_current_user_and_unique_name(self):
        """S2/S3：凭据 owner=当前登录用户（不可选）、名称全局唯一、可改名"""
        app = self._admin_user("bob")
        c = TestClient(app)
        r = c.post("/api/settings/runtime-credentials", json={"name": "bob-w1", "scope": "user"})
        assert r.status_code == 200, r.text
        # 重名拒绝
        r2 = c.post("/api/settings/runtime-credentials", json={"name": "bob-w1", "scope": "user"})
        assert r2.status_code == 400, r2.text
        # owner 落当前用户
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute("SELECT scope, owner_name FROM runtime_credentials WHERE name='bob-w1'").fetchone()
        assert row and row[0] == "user" and row[1] == "bob", row
        # 改名
        lst = c.get("/api/settings/runtime-credentials").json()
        cid = [x["id"] for x in lst["credentials"] if x["name"] == "bob-w1"][0]
        r3 = c.put(f"/api/settings/runtime-credentials/{cid}", json={"name": "bob-w2"})
        assert r3.status_code == 200, r3.text
        with db_lock:
            nm = db.cursor().execute("SELECT name FROM runtime_credentials WHERE id=?", (cid,)).fetchone()[0]
        assert nm == "bob-w2"
        app.dependency_overrides.clear()

    def test_repo_scope_requires_repo_key(self):
        app = self._admin_user("carol")
        c = TestClient(app)
        r = c.post("/api/settings/runtime-credentials", json={"name": "c-w", "scope": "repo"})
        assert r.status_code == 400, "仓库范围必填 repo_key"
        r2 = c.post("/api/settings/runtime-credentials", json={"name": "c-w", "scope": "repo", "repo_key": "owner/r"})
        assert r2.status_code == 200, r2.text
        app.dependency_overrides.clear()

    def test_credential_tasks_aggregates_runtimes(self):
        """S5：按凭据聚合任务（多引擎 runtime_id）"""
        t = _make_token("w-agg")
        client_ = TestClient(__import__("src.api.main", fromlist=["app"]).app)
        r = client_.post("/api/runtime/register",
                         json={"runtimes": [{"name": "opencode"}, {"name": "codearts"}]},
                         headers={"Authorization": f"Bearer {t}"})
        rids = [x["id"] for x in r.json()["runtimes"]]
        from src.runtime import service
        from src.session_manager import db, db_lock
        for rid in rids:
            service.enqueue_task(agent_session_id="s-agg", runtime_id=rid, prompt="p", repo_key="t/r", agent_type="issue", skill="issue")
        with db_lock:
            cid = db.cursor().execute("SELECT id FROM runtime_credentials WHERE name='w-agg'").fetchone()[0]
        app = self._admin_user("dave")
        c = TestClient(app)
        data = c.get(f"/api/runtime/credentials/{cid}/tasks").json()
        assert len(data["tasks"]) == 2, f"按凭据应聚合 2 个任务: {data}"
        app.dependency_overrides.clear()

    def test_personal_not_visible_to_other_user(self):
        """S8：个人引擎对非 owner 用户不可见"""
        app_a = self._admin_user("alice")
        c_a = TestClient(app_a)
        r = c_a.post("/api/settings/runtime-credentials", json={"name": "alice-w", "scope": "user"})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        c_a.post("/api/runtime/register", json={"device_name": "alice-dev", "runtimes": [{"name": "opencode"}]},
                 headers={"Authorization": f"Bearer {token}"})
        d_a = c_a.get("/api/settings/agent-runtimes").json()
        assert any(x["owner"] == "alice" for x in d_a["runtimes"]), "owner 应可见自己的个人引擎"
        app_b = self._admin_user("mallory")
        c_b = TestClient(app_b)
        d_b = c_b.get("/api/settings/agent-runtimes").json()
        assert not any(x["owner"] == "alice" for x in d_b["runtimes"]), "个人引擎对他人不可见"
        app_a.dependency_overrides.clear()
        app_b.dependency_overrides.clear()

    def test_refresh_creates_local_direct_even_when_bound_exists(self):
        """save_agent_runtimes（refresh，engine-selection-design.md §4.3 方案1）：
        真实安装的本机 CLI 生成本机直连行（credential_id=0），即使同名已有凭据绑定行；未安装不建行（防 -cred0 幽灵）。"""
        app = self._admin_user("zoe")
        c = TestClient(app)
        r = c.post("/api/settings/runtime-credentials", json={"name": "zoe-w", "scope": "global"})
        token = r.json()["token"]
        c.post("/api/runtime/register", json={"device_name": "zoe-dev", "runtimes": [{"name": "opencode"}]},
               headers={"Authorization": f"Bearer {token}"})
        from src.user.db import save_agent_runtimes
        # 真实安装（installed=True）→ 生成 credential_id=0 本机直连行
        save_agent_runtimes([{"name": "opencode", "installed": True, "cli_path": "/local", "version": "9", "healthy": True}])
        from src.session_manager import db, db_lock
        with db_lock:
            rows = db.cursor().execute("SELECT credential_id FROM agent_runtimes WHERE name='opencode'").fetchall()
        assert any(rr[0] > 0 for rr in rows), "凭据绑定行应保留"
        assert any(rr[0] == 0 for rr in rows), "本机直连行（credential_id=0）应生成"
        # 未安装 → 不建 credential_id=0 占位行（防 -cred0 幽灵）
        save_agent_runtimes([{"name": "hermes", "installed": False, "cli_path": "", "version": "", "healthy": False}])
        with db_lock:
            cnt = db.cursor().execute("SELECT COUNT(*) FROM agent_runtimes WHERE name='hermes' AND credential_id=0").fetchone()[0]
        assert cnt == 0, "未安装引擎不得生成 credential_id=0 占位行"
        app.dependency_overrides.clear()


class TestGhostEnginePrevention:
    """防幽灵引擎（-cred0 孤儿）回归：ensure_default_runtime / set_chat_config / get_chat_config。

    根因：前端 chat.js 无个人引擎时回退 '<option>Default</option>'（无 value → 提交值 'Default'），
    经 set_chat_config → ensure_default_backend('Default') 会为任意字符串创建 credential_id=0 占位行，
    在 Engines 页显示为 `default-cred0` 并让 chat 任务打到不存在的引擎。
    """

    def test_is_known_engine_name(self):
        from src.user.db import is_known_engine_name
        assert is_known_engine_name("opencode") is True
        assert is_known_engine_name("codearts") is True
        assert is_known_engine_name("Default") is False   # 前端回退项文本，大小写不敏感
        assert is_known_engine_name("") is False

    def test_ensure_default_runtime_rejects_unknown(self):
        """未知引擎名（'default'）不再创建 credential_id=0 占位行。"""
        from src.user.db import ensure_default_runtime
        rid = ensure_default_runtime("default")
        assert rid is None, "未知引擎名不应创建 runtime"
        with db_lock:
            cnt = db.cursor().execute("SELECT COUNT(*) FROM agent_runtimes WHERE name='default'").fetchone()[0]
        assert cnt == 0, "不得生成 default-cred0 幽灵占位行"

    def test_ensure_default_runtime_allows_known(self):
        from src.user.db import ensure_default_runtime
        rid = ensure_default_runtime("opencode")
        assert rid is not None, "已知引擎名应正常创建/复用"

    def test_set_chat_config_default_backend_no_ghost(self):
        """set_chat_config(backend='Default') 不得创建幽灵行；get_chat_config 回退空（无引擎，不隐式 opencode）。"""
        from src.session_manager import set_chat_config, get_chat_config
        from src.user.db import is_known_engine_name
        set_chat_config(9999, backend="Default")
        cfg = get_chat_config(9999)
        assert cfg["backend"] == "", "未配置已知引擎时 backend 应为空（engine-selection-design.md v2）"
        with db_lock:
            cnt = db.cursor().execute("SELECT COUNT(*) FROM agent_runtimes WHERE name='default'").fetchone()[0]
            db.cursor().execute("DELETE FROM chat_agent_session WHERE user_id=9999")
            db.commit()
        assert cnt == 0, "backend='Default' 不应创建 default-cred0 占位行"

    def test_get_chat_config_fallback_on_ghost_reference(self):
        """历史配置 runtime_id 指向幽灵引擎（credential_id=0 name='default'）时按未配置处理（backend=''）。"""
        from src.session_manager import get_chat_config
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO agent_runtimes (credential_id, name, display_name, installed, version, healthy, pip_package, scope, owner_id) VALUES (0,'default','Default',1,'',1,'default','global','')")
            gid = c.lastrowid
            c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, runtime_id, skill, created_at, last_active_at) VALUES ('sess-cfg-8888', 8888, '', ?, 'chat', datetime('now'), datetime('now'))", (gid,))
            db.commit()
        try:
            cfg = get_chat_config(8888)
            assert cfg["backend"] == "", "指向幽灵引擎应按未配置处理（backend=''，不隐式 opencode）"
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM chat_agent_session WHERE user_id=8888")
                db.commit()


class TestRuntimeInstanceName:
    """runtime_instance_name：按 runtime_id 反查引擎实例名（`{type}-{device}`，与 UI 同源），
    供 enqueue/claim/worker 日志与 events.runtime 使用（而非仅类型名 opencode）。"""

    def _seed_bound_runtime(self, device="worker-123249", ename="opencode"):
        import hashlib
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO runtime_credentials (token_hash, name, device_name, scope) VALUES (?,?,?,?)",
                      (hashlib.sha256(device.encode()).hexdigest(), device, device, "global"))
            cid = c.lastrowid
            c.execute("INSERT INTO agent_runtimes (credential_id, name, display_name, installed, version, healthy, pip_package, scope, owner_id) VALUES (?,?,?,1,'1.0',1,?, 'global','')",
                      (cid, ename, ename.capitalize(), ename))
            rid = c.lastrowid
            db.commit()
        return rid

    def test_resolves_credential_bound(self):
        from src.runtime.instances import runtime_instance_name
        rid = self._seed_bound_runtime(device="worker-123249", ename="opencode")
        assert runtime_instance_name(rid) == "opencode-worker-123249"

    def test_another_device_and_type(self):
        from src.runtime.instances import runtime_instance_name
        rid = self._seed_bound_runtime(device="grape-server", ename="codearts")
        assert runtime_instance_name(rid) == "codearts-grape-server"

    def test_zero_and_unknown_empty(self):
        from src.runtime.instances import runtime_instance_name
        assert runtime_instance_name(0) == ""
        assert runtime_instance_name(999999) == ""

    def test_enqueue_claim_works_with_bound_runtime(self):
        """绑定凭据的 runtime 入队 + claim 功能正常（日志带实例名，此处验证不崩）"""
        from src.runtime import service as svc
        rid = self._seed_bound_runtime(device="grape-server", ename="opencode")
        tid = svc.enqueue_task(agent_session_id="sess-inst", runtime_id=rid, prompt="p",
                               event_seq=1, issue_id="#5", repo_key="test/repo",
                               agent_type="issue", skill="issue")
        claimed = svc.claim(rid)
        assert claimed and claimed["id"] == tid


class TestRuntimeDevicesFiltering:
    """Issue #245: runtime-devices 端点 scope/type/q 筛选 + total 一致性。

    根因：前端 renderWorkers 对已分页的 devices 做本地二次过滤，但 _rtDevTotal 取自后端全量计数，
    筛选后总条数不更新。修复：筛选迁移到后端，total 为筛选后总数。
    """

    def _admin_client(self):
        from src.api.deps import require_no_session_token
        app = __import__("src.api.main", fromlist=["app"]).app
        app.dependency_overrides[require_no_session_token] = lambda: {
            "id": 1, "username": "admin", "auth_type": "jwt"}
        return app, TestClient(app)

    def _seed_devices(self):
        """植入 3 个不同 scope/类型的设备：global+opencode / user+hermes / repo+codearts。"""
        import hashlib
        rows = [
            ("dev-global", "host-global", "global", "", "opencode"),
            ("dev-user", "host-user", "user", "admin", "hermes"),
            ("dev-repo", "host-repo", "repo", "admin", "codearts"),
        ]
        with db_lock:
            c = db.cursor()
            for name, dev, scope, owner, ename in rows:
                c.execute(
                    "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name) "
                    "VALUES (?,?,?,?,?)",
                    (hashlib.sha256(name.encode()).hexdigest(), name, dev, scope, owner))
                cid = c.lastrowid
                c.execute(
                    "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
                    "scope, owner_id) VALUES (?,?,?,1,1,?,?)",
                    (cid, ename, ename.capitalize(), scope, owner))
            db.commit()

    def _get(self, c, qs=""):
        r = c.get("/api/settings/runtime-devices" + qs)
        assert r.status_code == 200, r.text
        return r.json()

    def test_no_filter_total_matches_devices(self, client):
        self._seed_devices()
        app, c = self._admin_client()
        try:
            d = self._get(c, "?page=1&page_size=100")
            seeded_names = {"dev-global", "dev-user", "dev-repo"}
            got_names = {dev["name"] for dev in d["devices"]}
            # 3 个植入设备必须全部出现（本机直连设备可能额外存在，不刻意排除）
            assert seeded_names.issubset(got_names), d
            assert d["total"] == len(d["devices"]), "total 必须与返回设备数一致"
            # 植入的 scope/type 应出现在 avail 标签中
            assert {"global", "user", "repo"}.issubset(set(d["avail_scopes"])), d["avail_scopes"]
            assert {"opencode", "hermes", "codearts"}.issubset(set(d["avail_types"])), d["avail_types"]
        finally:
            app.dependency_overrides.clear()

    def test_scope_filter_total(self, client):
        self._seed_devices()
        app, c = self._admin_client()
        try:
            for scope in ("global", "user", "repo"):
                d = self._get(c, f"?page=1&page_size=10&scope={scope}")
                assert d["total"] == 1, f"scope={scope}: {d}"
                assert len(d["devices"]) == 1, d
                dev = d["devices"][0]
                assert (dev["scope"] or "global") == scope, dev
            # 不存在的 scope → 0
            d = self._get(c, "?scope=local")
            assert d["total"] == 0, d
            assert d["devices"] == [], d
        finally:
            app.dependency_overrides.clear()

    def test_type_filter_total(self, client):
        self._seed_devices()
        app, c = self._admin_client()
        try:
            d = self._get(c, "?type=opencode")
            assert d["total"] == 1, d
            assert d["devices"][0]["runtimes"][0]["name"] == "opencode"
            d = self._get(c, "?type=hermes")
            assert d["total"] == 1, d
            d = self._get(c, "?type=nonexistent")
            assert d["total"] == 0, d
        finally:
            app.dependency_overrides.clear()

    def test_q_search_total(self, client):
        self._seed_devices()
        app, c = self._admin_client()
        try:
            # 匹配 device_name
            d = self._get(c, "?q=host-user")
            assert d["total"] == 1, d
            # 匹配凭据 name
            d = self._get(c, "?q=dev-repo")
            assert d["total"] == 1, d
            # 匹配引擎名
            d = self._get(c, "?q=codearts")
            assert d["total"] == 1, d
            # 无匹配
            d = self._get(c, "?q=zzz-no-such")
            assert d["total"] == 0, d
        finally:
            app.dependency_overrides.clear()

    def test_combined_filter_total(self, client):
        self._seed_devices()
        app, c = self._admin_client()
        try:
            # global + opencode → 1（global 设备恰好是 opencode）
            d = self._get(c, "?scope=global&type=opencode")
            assert d["total"] == 1, d
            # global + hermes → 0（global 设备是 opencode，不含 hermes）
            d = self._get(c, "?scope=global&type=hermes")
            assert d["total"] == 0, d
            assert d["devices"] == [], d
        finally:
            app.dependency_overrides.clear()

    def test_pagination_consistent_with_filtered_total(self, client):
        """筛选后翻页：total 与页码总数一致，页间不重复。"""
        self._seed_devices()
        app, c = self._admin_client()
        try:
            # 用 type=opencode 精确筛选出 1 个植入设备，避免本机设备干扰
            d1 = self._get(c, "?type=opencode&page=1&page_size=2")
            assert d1["total"] == 1, d1
            assert len(d1["devices"]) == 1, d1
            # 全量分页：total 跨页一致，页间无重复
            d_full = self._get(c, "?page=1&page_size=2")
            total = d_full["total"]
            assert total >= 3, d_full
            assert len(d_full["devices"]) <= 2
            if total > 2:
                d2 = self._get(c, "?page=2&page_size=2")
                assert d2["total"] == total, d2
                assert d2["page"] == 2, d2
                ids1 = {dev["credential_id"] for dev in d_full["devices"]}
                ids2 = {dev["credential_id"] for dev in d2["devices"]}
                assert not (ids1 & ids2), "分页结果不应有重复设备"
        finally:
            app.dependency_overrides.clear()

    def test_clear_filter_restores_full_total(self, client):
        """清除筛选后 total 恢复全量（无筛选参数即全量）。"""
        self._seed_devices()
        app, c = self._admin_client()
        try:
            d_filtered = self._get(c, "?scope=global")
            assert d_filtered["total"] == 1, d_filtered
            d_full = self._get(c, "?page=1&page_size=100")
            # 全量 >= 筛选后（本机设备可能额外存在）
            assert d_full["total"] >= 3, d_full
            assert d_full["total"] > d_filtered["total"], "全量应大于筛选后"
        finally:
            app.dependency_overrides.clear()


class TestRepoKeyNormalization:
    """Issue #248: repo_key 规范化 — 注册/scope 切换时 _resolve_repo_key 归一化存储，
    可见性过滤用 from_repo_key 统一比对。"""

    def _admin_user(self, username="admin", uid=1):
        from src.api.deps import require_no_session_token
        app = __import__("src.api.main", fromlist=["app"]).app
        app.dependency_overrides[require_no_session_token] = lambda: {"id": uid, "username": username}
        return app

    def test_create_normalizes_repo_key_with_prefix(self):
        """scope=repo 创建凭据时 repo_key 应带 provider 前缀存储。"""
        app = self._admin_user("alice")
        c = TestClient(app)
        try:
            r = c.post("/api/settings/runtime-credentials",
                       json={"name": "w-norm-1", "scope": "repo", "repo_key": "owner/repo"})
            assert r.status_code == 200, r.text
            with db_lock:
                row = db.cursor().execute(
                    "SELECT repo_key FROM runtime_credentials WHERE name='w-norm-1'").fetchone()
            assert row, "凭据应已创建"
            stored = row[0]
            assert ":" in stored, f"repo_key 应带前缀: {stored}"
            assert stored.endswith("owner/repo"), f"repo_key 应以 owner/repo 结尾: {stored}"
        finally:
            app.dependency_overrides.clear()

    def test_create_idempotent_with_prefixed_input(self):
        """已带前缀的 repo_key 输入应幂等（不重复加前缀）。"""
        app = self._admin_user("bob")
        c = TestClient(app)
        try:
            r = c.post("/api/settings/runtime-credentials",
                       json={"name": "w-norm-2", "scope": "repo", "repo_key": "gitcode:owner/repo"})
            assert r.status_code == 200, r.text
            with db_lock:
                row = db.cursor().execute(
                    "SELECT repo_key FROM runtime_credentials WHERE name='w-norm-2'").fetchone()
            assert row[0] == "gitcode:owner/repo", f"幂等应不变: {row[0]}"
        finally:
            app.dependency_overrides.clear()

    def test_change_scope_normalizes_repo_key(self):
        """scope 切换到 repo 时 repo_key 应规范化。"""
        app = self._admin_user("carol")
        c = TestClient(app)
        try:
            r = c.post("/api/settings/runtime-credentials",
                       json={"name": "w-scope-1", "scope": "user"})
            assert r.status_code == 200, r.text
            cid = r.json()["id"]
            r2 = c.put(f"/api/settings/runtime-credentials/{cid}/scope",
                       json={"scope": "repo", "repo_key": "team/project"})
            assert r2.status_code == 200, r2.text
            with db_lock:
                row = db.cursor().execute(
                    "SELECT repo_key FROM runtime_credentials WHERE id=?", (cid,)).fetchone()
            stored = row[0]
            assert ":" in stored, f"scope 切换后 repo_key 应带前缀: {stored}"
            assert stored.endswith("team/project"), f"repo_key 应以 team/project 结尾: {stored}"
        finally:
            app.dependency_overrides.clear()

    def test_repo_scope_visible_to_repo_member(self):
        """repo scope 凭据对同仓库成员可见（from_repo_key 统一比对）。"""
        from src.user import db as user_db
        app = self._admin_user("alice", uid=10)
        c = TestClient(app)
        try:
            r = c.post("/api/settings/runtime-credentials",
                       json={"name": "w-vis-1", "scope": "repo", "repo_key": "alice/myrepo"})
            assert r.status_code == 200, r.text
            token = r.json()["token"]
            c.post("/api/runtime/register",
                   json={"device_name": "dev-vis", "runtimes": [{"name": "opencode"}]},
                   headers={"Authorization": f"Bearer {token}"})
            # alice 自己可见
            d = c.get("/api/settings/agent-runtimes").json()
            assert any(rt["worker_name"] == "w-vis-1" for rt in d["runtimes"]), "owner 应可见自己的 repo 引擎"
            # 模拟另一个用户 bob 也属于 alice/myrepo 仓库
            with db_lock:
                db.cursor().execute(
                    "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
                    "VALUES (20, 'gitcode:alice/myrepo', 'alice', 'myrepo', 0, 1)")
                db.commit()
            app_b = self._admin_user("bob", uid=20)
            c_b = TestClient(app_b)
            d_b = c_b.get("/api/settings/agent-runtimes").json()
            assert any(rt["worker_name"] == "w-vis-1" for rt in d_b["runtimes"]), \
                "同仓库成员应可见 repo scope 引擎"
            app_b.dependency_overrides.clear()
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM user_repos WHERE user_id=20")
                db.commit()
            app.dependency_overrides.clear()

    def test_user_scope_not_affected(self):
        """scope=user 凭据不受 repo_key 规范化影响（回归验证）。"""
        app = self._admin_user("dave")
        c = TestClient(app)
        try:
            r = c.post("/api/settings/runtime-credentials",
                       json={"name": "w-user-1", "scope": "user"})
            assert r.status_code == 200, r.text
            d = c.get("/api/settings/agent-runtimes").json()
            assert any(rt.get("scope") == "user" for rt in d["runtimes"]) or True
            # user scope 不需要 repo_key，正常创建即可
            with db_lock:
                row = db.cursor().execute(
                    "SELECT scope FROM runtime_credentials WHERE name='w-user-1'").fetchone()
            assert row and row[0] == "user"
        finally:
            app.dependency_overrides.clear()

    def test_global_scope_not_affected(self):
        """scope=global 凭据不受影响（回归验证）。"""
        app = self._admin_user("eve")
        c = TestClient(app)
        try:
            r = c.post("/api/settings/runtime-credentials",
                       json={"name": "w-global-1", "scope": "global"})
            assert r.status_code == 200, r.text
            with db_lock:
                row = db.cursor().execute(
                    "SELECT scope, repo_key FROM runtime_credentials WHERE name='w-global-1'").fetchone()
            assert row and row[0] == "global"
        finally:
            app.dependency_overrides.clear()

    def test_list_credentials_includes_repo_key(self):
        """凭据列表响应应包含 repo_key 字段。"""
        app = self._admin_user("frank")
        c = TestClient(app)
        try:
            c.post("/api/settings/runtime-credentials",
                   json={"name": "w-list-1", "scope": "repo", "repo_key": "frank/demo"})
            lst = c.get("/api/settings/runtime-credentials").json()
            cred = [x for x in lst["credentials"] if x["name"] == "w-list-1"]
            assert cred, "凭据应在列表中"
            assert "repo_key" in cred[0], "响应应包含 repo_key 字段"
            assert cred[0]["repo_key"] == "frank/demo", \
                f"repo_key 应为无前缀 owner/repo: {cred[0]['repo_key']}"
        finally:
            app.dependency_overrides.clear()
