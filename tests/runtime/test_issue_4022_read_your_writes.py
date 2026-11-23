"""Issue #4022 回归：pending-review 置位后首读即见新状态（read-your-writes）。

背景（qa-cli 反馈 ID 568）：`issues pending-review` 返回 changed:true 后，紧接的
`issues list` 首次读回仍是 active（旧值），需连续多次轮询才稳定；且 `issues detail.task_status`
与 `issues status get.status` 曾出现同一 Issue 相反读数。

覆盖：
1. 预热列表 5s 结果缓存后置位 pending_review，立即读列表必须是 pending_review（无轮询抖动）；
2. 连续多次读列表保持 pending_review（不出现 active 回滚）；
3. 其它写路径（PUT 状态）同样即时可见——缓存一致性不依赖逐个写接口接入失效钩子；
4. `issues list` / `issues detail`（task_status + status 别名）/ `issues status get` 口径一致。

每个用例使用独立仓库，避免列表结果缓存（按 user+仓库+参数键入）在用例间互相污染。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db"))

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.jwt_utils import create_access_token
from src.domains.mgmt.auth import grape_account_db
from src.domains.mgmt.auth.grape_account_db import get_token_version
from src.kernel.session_manager import db, db_lock

client = TestClient(app)

_TOPIC = "Issue #4022 写后即读一致性"
_REPO_KEYS = []
_UID = None


def _jwt_headers(uid):
    return {"Authorization": "Bearer " + create_access_token(
        uid["id"], uid["username"], get_token_version(uid["id"]))}


def _session_headers(uid, repo_key, agent_type="qa"):
    import uuid
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"rw-{uuid.uuid4().hex}", repo_key=repo_key,
        agent_type=agent_type, user_id=uid["id"])
    return {"Authorization": f"Bearer {tok}"}


def _clean_all():
    with db_lock:
        c = db.cursor()
        for _rk in _REPO_KEYS:
            for t in ("events", "issue_state", "agent_tasks", "issue"):
                try:
                    c.execute(f"DELETE FROM {t} WHERE repo_key=?", (_rk,))
                except Exception:
                    pass
        db.commit()


def setup_module():
    global _UID
    grape_account_db.create_grape_account("test4022_user", email=None, password="x")
    _UID = grape_account_db.get_grape_account_by_username("test4022_user")
    assert _UID, "test account required"
    try:
        from src.api import db_setup
        db_setup.init_database()
    except Exception:
        pass
    _clean_all()


def teardown_module():
    _clean_all()


class _Ctx:
    def __init__(self, tag):
        self.owner = f"test4022{tag}"
        self.repo = "rw"
        self.repo_key = f"gitcode:{self.owner}/{self.repo}"
        _REPO_KEYS.append(self.repo_key)
        self.jwt = _jwt_headers(_UID)
        self.session = _session_headers(_UID, self.repo_key)

    def seed(self, number=11, status="active"):
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
                "task_description, status, stage, owner_agent) VALUES (?,?,?,?,?,?,?,?,?)",
                (_UID["id"], "grape_bot", self.repo_key, number, _TOPIC, "d", status, "qa", "qa"))
            card_id = c.lastrowid
            db.commit()
        return card_id

    def list_items(self):
        r = client.get(f"/api/issues/{self.owner}/{self.repo}", headers=self.jwt)
        assert r.status_code == 200, r.text
        return r.json().get("issues") or []

    @staticmethod
    def find(items, number):
        for it in items:
            if str(it.get("issue_number")) == str(number):
                return it
        return None

    def set_pending_review(self, card_id):
        r = client.post(f"/api/issues/{card_id}/pending-review",
                        headers=self.session,
                        json={"review_points": "请确认是否合入该 PR"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["changed"] is True and body["status"] == "pending_review"
        return body


# ---------------------------------------------------------------- 缓存预热 + 写后即读

def test_list_warmed_cache_sees_pending_review_after_write():
    ctx = _Ctx("a")
    card_id = ctx.seed()
    # 1) 预热列表缓存（此时 status=active，缓存条目已写入 5s TTL）
    first = ctx.find(ctx.list_items(), 11)
    assert first is not None and first["status"] == "active"

    # 2) 写置位并立即读列表：必须已经是 pending_review（不轮询）
    ctx.set_pending_review(card_id)
    item = ctx.find(ctx.list_items(), 11)
    assert item is not None
    assert item["status"] == "pending_review", item
    assert item["task_status"] == "pending_review", item

    # 3) 连续多次读不抖动
    for _ in range(5):
        again = ctx.find(ctx.list_items(), 11)
        assert again is not None and again["status"] == "pending_review", again


# ---------------------------------------------------------------- 任意写路径即时可见

def test_list_cache_follows_other_status_write_path():
    ctx = _Ctx("b")
    card_id = ctx.seed(number=12, status="processing")
    ctx.list_items()  # 预热缓存

    r = client.put(f"/api/issues/{card_id}/status",
                   headers=ctx.jwt, json={"status": "pending_review"})
    assert r.status_code == 200, r.text
    item = ctx.find(ctx.list_items(), 12)
    assert item is not None and item["status"] == "pending_review", item

    r = client.put(f"/api/issues/{card_id}/status",
                   headers=ctx.jwt, json={"status": "processing"})
    assert r.status_code == 200, r.text
    item = ctx.find(ctx.list_items(), 12)
    assert item is not None and item["status"] == "processing", item


# ---------------------------------------------------------------- 读接口口径统一

def test_detail_status_get_and_list_agree_after_write():
    ctx = _Ctx("c")
    card_id = ctx.seed(number=13)
    ctx.list_items()  # 预热列表缓存
    ctx.set_pending_review(card_id)

    listed = ctx.find(ctx.list_items(), 13)
    assert listed is not None

    d = client.get(f"/api/issues/{ctx.owner}/{ctx.repo}/13", headers=ctx.jwt)
    assert d.status_code == 200, d.text
    detail = d.json()
    assert detail["task_status"] == "pending_review", detail
    # Issue #4022：detail 新增 status 别名，与 task_status 同值
    assert detail["status"] == detail["task_status"], detail

    s = client.get(f"/api/issues/{card_id}/status", headers=ctx.jwt)
    assert s.status_code == 200, s.text
    status_body = s.json()
    assert status_body["status"] == "pending_review", status_body

    assert listed["status"] == listed["task_status"] == detail["task_status"] == status_body["status"]
    # 同响应内 pipeline 业务状态与顶层 task_status 同源，不得相反
    assert detail.get("pipeline", {}).get("issue_task_status") == detail["task_status"], detail.get("pipeline")