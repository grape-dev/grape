"""feedback 意见反馈对外 OpenAPI 契约测试（feedback-openapi-expose-design.md §六 A）。

覆盖：
- 5 个公开 op 进契约，scope（submit=dual，mine/context/delete/upload=user_only）
- 管理 3 端点不进契约
- 无 token 401 / api_key 三通道 / tk_ 提交（source=agent）与 user_only 403 / admin 鉴权
- tk_ 提交忽略 body 伪造（A10）；每任务反馈 ≤2（A11）
"""
import sys, os, uuid

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from fastapi.testclient import TestClient
from src.api.main import app
from src.api import auth_token, openapi
from src.api.jwt_utils import create_access_token
from src.auth import grape_account_db
from src.session_manager import db, db_lock

client = TestClient(app)


def _schema():
    r = client.get("/openapi.json")
    assert r.status_code == 200
    return r.json()


def _user():
    u = grape_account_db.create_grape_account(f"fb_{uuid.uuid4().hex[:6]}",
                                              email=None, password="pw")
    return u


def _jwt(uid, username):
    return create_access_token(uid, username, grape_account_db.get_token_version(uid))


def _api_key(uid):
    return auth_token.generate_api_token(uid, name="t")["token"]


def _session_token(repo_key="o/r", agent_type="coding", session_key=None):
    return auth_token.issue_session_token(
        session_key=session_key or f"task-{uuid.uuid4().hex[:6]}",
        repo_key=repo_key, agent_type=agent_type, user_id=0)


def _admin_jwt():
    """插入 admin_accounts + 签发 role=admin JWT。"""
    name = f"adm_{uuid.uuid4().hex[:6]}"
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO admin_accounts (username, password_hash, is_super, token_version) "
                  "VALUES (?, '', 1, 0)", (name,))
        db.commit()
        aid = c.lastrowid
    return create_access_token(aid, name, 0, "", "admin"), aid


def _insert_feedback(user_id, username="", **kw):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO user_feedback "
            "(user_id, username, category, title, description, status, source) "
            "VALUES (?,?,?,?,?, 'open', ?)",
            (user_id, username, kw.get("category", "bug"),
             kw.get("title", "测试"), kw.get("description", "desc"), kw.get("source", "user")))
        db.commit()
        return c.lastrowid


def _cleanup(*fids):
    with db_lock:
        for fid in fids:
            db.execute("DELETE FROM user_feedback WHERE id=?", (fid,))
        db.commit()


class TestFeedbackOpenAPIContract:
    def test_public_ops_in_contract(self):
        """A1：5 个公开 op 进契约；管理 3 端点不在（A3）。"""
        s = _schema()
        paths = set(s["paths"])
        for p in ("/api/feedback", "/api/feedback/mine",
                  "/api/feedback/context/{event_seq}",
                  "/api/feedback/{fid}", "/api/feedback/upload"):
            assert p in paths, f"缺公开路径: {p}"
        for p in ("/api/feedback/list", "/api/feedback/stats",
                  "/api/feedback/{fid}/status"):
            assert p not in paths, f"管理端点不应进契约: {p}"
        ops = {op["operationId"] for p in paths for op in s["paths"][p].values()
               if isinstance(op, dict)}
        for opid in ("feedback_submit", "feedback_mine", "feedback_context",
                     "feedback_delete", "feedback_upload"):
            assert opid in ops, f"缺 op: {opid}"

    def test_scopes(self):
        """A2：submit=dual；mine/context/delete/upload=user_only。"""
        s = _schema()
        scope_of = {}
        for p, ops in s["paths"].items():
            for m, op in ops.items():
                scope_of[(m, p)] = op.get("x-token-scope")
        assert scope_of[("post", "/api/feedback")] == "dual"
        for p in ("/api/feedback/mine", "/api/feedback/context/{event_seq}",
                  "/api/feedback/{fid}", "/api/feedback/upload"):
            assert scope_of[("get" if "context" in p or p == "/api/feedback/mine" else
                             ("delete" if "{fid}" in p else "post"), p)] == "user_only", p

    def test_no_token_401(self):
        """A4：无 token → 401。"""
        assert client.post("/api/feedback",
                           json={"category": "bug", "title": "t", "description": "d"}).status_code == 401
        assert client.get("/api/feedback/mine").status_code == 401


class TestFeedbackOpenAPIUserScopes:
    def setup_method(self):
        self.user = _user()
        self.jwt = _jwt(self.user["id"], self.user["username"])
        self.api_key = _api_key(self.user["id"])

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM user_feedback WHERE user_id=?", (self.user["id"],))
            _cleanup(*[r[0] for r in c.fetchall()])

    def test_api_key_three_channels_mine(self):
        """A5：api_key 三通道调 mine → 200。"""
        h = {"Authorization": f"Bearer {self.api_key}"}
        assert client.get("/api/feedback/mine", headers=h).status_code == 200
        assert client.get("/api/feedback/mine",
                          headers={"PRIVATE-TOKEN": self.api_key}).status_code == 200
        assert client.get("/api/feedback/mine?access_token=" + self.api_key).status_code == 200

    def test_session_token_submit_agent(self):
        """A6：tk_ submit → 200，落库 source='agent'。"""
        tk = _session_token(repo_key="fb:o/r", agent_type="coding")
        r = client.post("/api/feedback",
                        json={"category": "suggestion", "title": "agent反馈",
                              "description": "cli 不友好"},
                        headers={"Authorization": f"Bearer {tk}"})
        assert r.status_code == 200
        fid = r.json()["id"]
        with db_lock:
            row = db.execute(
                "SELECT source, repo_key, agent_type, task_id FROM user_feedback WHERE id=?",
                (fid,)).fetchone()
        assert row[0] == "agent"
        assert row[1] == "fb:o/r"          # token 绑定仓库
        assert row[2] == "coding"
        assert row[3]                       # session_key 落 task_id
        _cleanup(fid)

    def test_session_token_user_only_403(self):
        """A7：tk_ mine/delete/upload → 403。"""
        tk = _session_token()
        assert client.get("/api/feedback/mine",
                          headers={"Authorization": f"Bearer {tk}"}).status_code == 403
        fid = _insert_feedback(self.user["id"], self.user["username"])
        r = client.delete(f"/api/feedback/{fid}",
                          headers={"Authorization": f"Bearer {tk}"})
        assert r.status_code == 403
        _cleanup(fid)

    def test_tk_forged_body_ignored(self):
        """A10：tk_ 提交 body 伪造 repo_key → 落库以 token 为准。"""
        tk = _session_token(repo_key="fb:real/repo")
        r = client.post("/api/feedback",
                        json={"category": "bug", "title": "伪造", "description": "d",
                              "repo_key": "fb:evil/repo"},
                        headers={"Authorization": f"Bearer {tk}"})
        assert r.status_code == 200
        fid = r.json()["id"]
        with db_lock:
            row = db.execute("SELECT repo_key FROM user_feedback WHERE id=?", (fid,)).fetchone()
        assert row[0] == "fb:real/repo"
        _cleanup(fid)

    def test_tk_per_task_limit(self):
        """A11：同一 tk_（session_key）反馈 ≤2，第 3 条 429。"""
        sk = f"task-{uuid.uuid4().hex[:6]}"
        tk = _session_token(session_key=sk)
        fids = []
        try:
            for i in range(3):
                r = client.post("/api/feedback",
                                json={"category": "bug", "title": f"t{i}",
                                      "description": "d"},
                                headers={"Authorization": f"Bearer {tk}"})
                if i < 2:
                    assert r.status_code == 200, f"第{i+1}条应 200"
                    fids.append(r.json()["id"])
                else:
                    assert r.status_code == 429, f"第3条应 429: {r.status_code}"
        finally:
            _cleanup(*fids)


class TestFeedbackAdminGuard:
    def setup_method(self):
        self.user = _user()
        self.jwt = _jwt(self.user["id"], self.user["username"])
        self.admin_token, self.admin_id = _admin_jwt()

    def teardown_method(self):
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM user_feedback WHERE user_id=?", (self.user["id"],))
            _cleanup(*[r[0] for r in c.fetchall()])
            db.execute("DELETE FROM admin_accounts WHERE id=?", (self.admin_id,))
            db.commit()

    def test_normal_user_status_403(self):
        """A8：普通用户 JWT 调 PUT /status → 403。"""
        fid = _insert_feedback(self.user["id"], self.user["username"])
        try:
            r = client.put(f"/api/feedback/{fid}/status",
                           json={"status": "resolved"},
                           headers={"Authorization": f"Bearer {self.jwt}"})
            assert r.status_code == 403
        finally:
            _cleanup(fid)

    def test_admin_status_200(self):
        """A9：admin JWT 调 PUT /status → 200；handler_name=admin token 的 username。"""
        fid = _insert_feedback(self.user["id"], self.user["username"])
        try:
            r = client.put(f"/api/feedback/{fid}/status",
                           json={"status": "in_progress", "handler_remark": "rm"},
                           headers={"Authorization": f"Bearer {self.admin_token}"})
            assert r.status_code == 200, r.text
            with db_lock:
                row = db.execute(
                    "SELECT status, handler_name FROM user_feedback WHERE id=?", (fid,)).fetchone()
            assert row[0] == "in_progress"
            assert row[1]                                # admin token username 落 handler_name
        finally:
            _cleanup(fid)

    def test_admin_list_source_filter(self):
        """list 支持 source 筛选。"""
        fid = _insert_feedback(0, "", source="agent", title="agent条目")
        try:
            r = client.get("/api/feedback/list?source=agent",
                           headers={"Authorization": f"Bearer {self.admin_token}"})
            assert r.status_code == 200, r.text
            titles = [it["title"] for it in r.json()["items"]]
            assert "agent条目" in titles
            r2 = client.get("/api/feedback/list?source=user",
                            headers={"Authorization": f"Bearer {self.admin_token}"})
            assert "agent条目" not in [it["title"] for it in r2.json()["items"]]
        finally:
            _cleanup(fid)

    def test_list_starts_no_auth_401(self):
        """管理端点无 token → 401。"""
        assert client.get("/api/feedback/list").status_code == 401
        assert client.get("/api/feedback/stats").status_code == 401