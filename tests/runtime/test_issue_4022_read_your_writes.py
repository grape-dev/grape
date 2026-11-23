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
    # QA Spec-1：独立 /pipeline 接口与 detail 同按内部主键回读，issue_task_status 口径一致
    p = client.get(f"/api/issues/{ctx.owner}/{ctx.repo}/13/pipeline", headers=ctx.jwt)
    assert p.status_code == 200, p.text
    assert p.json().get("issue_task_status") == detail["task_status"], p.json()
    assert p.json().get("issue_stage"), p.json()


# ---------------------------------------------------------------- QA 复审补充用例

def _seed_mirror_rows(ctx, number, status_a, status_b):
    """镜像 repo_key 同号多行：gitcode: 行（优先）与 github: 行。返回 gitcode 行主键。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "task_description, status, stage, owner_agent) VALUES (?,?,?,?,?,?,?,?,?)",
            (_UID["id"], "grape_bot", ctx.repo_key, number, _TOPIC, "d", status_a, "qa", "qa"))
        _primary = c.lastrowid
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "task_description, status, stage, owner_agent) VALUES (?,?,?,?,?,?,?,?,?)",
            (_UID["id"], "grape_bot", f"github:{ctx.owner}/{ctx.repo}", number,
             _TOPIC, "d", status_b, "sre", "sre"))
        db.commit()
    return _primary


def test_mirror_repo_rows_pin_same_authoritative_row():
    """QA Spec-1：镜像 repo 同号多行时，detail/pipeline 各字段钉同一权威行。"""
    ctx = _Ctx("d")
    number = 21
    primary_id = _seed_mirror_rows(ctx, number, "active", "completed")

    d = client.get(f"/api/issues/{ctx.owner}/{ctx.repo}/{number}", headers=ctx.jwt)
    assert d.status_code == 200, d.text
    detail = d.json()
    # 顶层 stage/status 与 task_status 来自 repo_keys 优先级首个命中行（gitcode:）
    assert detail["issue_id"] == primary_id, detail
    assert detail["task_status"] == "active", detail
    assert detail["stage"] == "qa", detail
    # pipeline 内嵌业务状态按同一内部主键回读，不得读到 github: 行的 completed/sre
    _pipe = detail.get("pipeline") or {}
    assert _pipe.get("issue_task_status") == "active", _pipe
    assert _pipe.get("issue_stage") == "qa", _pipe

    # 独立 /pipeline 同样主键化：issue_stage/issue_task_status 与 detail 一致
    p = client.get(f"/api/issues/{ctx.owner}/{ctx.repo}/{number}/pipeline", headers=ctx.jwt)
    assert p.status_code == 200, p.text
    _pj = p.json()
    assert _pj.get("issue_task_status") == "active", _pj
    assert _pj.get("issue_stage") == "qa", _pj


def test_review_pass_reject_then_list_sees_new_status():
    """QA Spec-2：review-pass / review-reject 写路径同样写后即读（不依赖失效钩子）。"""
    ctx = _Ctx("e")
    card_id = ctx.seed(number=15)
    ctx.list_items()  # 预热缓存
    ctx.set_pending_review(card_id)

    r = client.post(f"/api/issues/{card_id}/review-pass",
                    headers=ctx.session, json={"target": "processing"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True and body["status"] == "processing", body
    item = ctx.find(ctx.list_items(), 15)
    assert item is not None and item["status"] == "processing", item

    # 再置 pending_review 后打回（review-reject target=processing）
    ctx.set_pending_review(card_id)
    r = client.post(f"/api/issues/{card_id}/review-reject",
                    headers=ctx.session, json={"target": "processing", "reason": "需要修改"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True and body["status"] == "processing", body
    for _ in range(3):
        item = ctx.find(ctx.list_items(), 15)
        assert item is not None and item["status"] == "processing", item
    d = client.get(f"/api/issues/{ctx.owner}/{ctx.repo}/15", headers=ctx.jwt)
    assert d.status_code == 200 and d.json()["task_status"] == "processing", d.text


def test_confirm_done_then_list_sees_completed():
    """QA Spec-2：confirm-done（人工确认完成）写路径写后即读。"""
    ctx = _Ctx("f")
    card_id = ctx.seed(number=16)
    ctx.list_items()  # 预热缓存

    r = client.post(f"/api/issues/{card_id}/confirm-done",
                    headers=ctx.jwt, json={"confirmation": "verified"})
    assert r.status_code == 200, r.text
    item = ctx.find(ctx.list_items(), 16)
    assert item is not None and item["status"] == "completed", item


def test_freshness_check_exception_falls_back_to_recompute(monkeypatch):
    """QA Spec-2：回读异常必须 fail-closed 重算，绝不能返回过期缓存。"""
    ctx = _Ctx("g")
    card_id = ctx.seed(number=17, status="active")
    ctx.list_items()  # 预热缓存（status=active）

    # 直接 SQL 改状态（模拟任意外部写路径），再让核对函数抛异常：
    # `_cached_list_is_current` 必须 fail-closed 判过期 → 重算读到新值
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE issue SET status='processing' WHERE id=?", (card_id,))
        db.commit()

    from src.domains.runtime.routes import issues as _routes
    def _boom(_ids):
        raise RuntimeError("simulated readback failure")
    monkeypatch.setattr(_routes, "_issue_status_by_ids", _boom)
    try:
        item = ctx.find(ctx.list_items(), 17)
        assert item is not None and item["status"] == "processing", item
    finally:
        monkeypatch.undo()


def test_row_deleted_invalidates_warmed_cache():
    """QA Spec-2：缓存行被删除后，列表必须重算并下掉该卡片（不得继续返回旧卡）。"""
    ctx = _Ctx("h")
    card_id = ctx.seed(number=18, status="active")
    ctx.list_items()  # 预热缓存

    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE id=?", (card_id,))
        db.commit()

    item = ctx.find(ctx.list_items(), 18)
    assert item is None, item


def test_status_snapshot_collapses_repeated_checks(monkeypatch):
    """QA Standards-1：快照窗口内重复核对只回读 1 次 DB（不逐请求抢全局 db_lock），
    且写入后（写代变化）下一次核对整窗刷新——写后即读不因快照削弱。"""
    from src.domains.runtime.routes import issues as _routes
    ctx = _Ctx("i")
    card_id = ctx.seed(number=19, status="active")

    # 重置快照窗口，确保本用例从空快照开始
    with _routes._issue_status_snapshot_lock:
        _routes._issue_status_snapshot.update({"ts": 0.0, "changes": -1, "data": {}})

    class _CountingRtDb:
        def __init__(self, real):
            self._real = real
            self.queries = 0
        def cursor(self, *a, **k):
            outer = self
            class _CountingCursor:
                def __init__(s, cur):
                    s._cur = cur
                def execute(s, sql, params=()):
                    outer.queries += 1
                    return s._cur.execute(sql, params)
                def __getattr__(s, n):
                    return getattr(s._cur, n)
            return _CountingCursor(self._real.cursor(*a, **k))
        def __getattr__(self, n):
            return getattr(self._real, n)

    _proxy = _CountingRtDb(_routes._rt_db)
    monkeypatch.setattr(_routes, "_rt_db", _proxy)
    try:
        # 6 次并发看板刷新同页核对 → 1 次回读（快照复用，QA 原目标「6 并发削成 1 次」）
        for _ in range(6):
            out = _routes._issue_status_by_ids([card_id])
            assert out.get(card_id) == "active", out
        assert _proxy.queries == 1, _proxy.queries

        # 写入后（total_changes 变化）下一次核对必须刷新并读到新值
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE issue SET status='processing' WHERE id=?", (card_id,))
            db.commit()
        out = _routes._issue_status_by_ids([card_id])
        assert out.get(card_id) == "processing", out
        assert _proxy.queries == 2, _proxy.queries

        # 行删除后：快照刷新记 None → 判漂移（由上层重算）
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE id=?", (card_id,))
            db.commit()
        out = _routes._issue_status_by_ids([card_id])
        assert out.get(card_id) is None, out
    finally:
        monkeypatch.undo()
    # 清快照，避免影响后续用例
    with _routes._issue_status_snapshot_lock:
        _routes._issue_status_snapshot.update({"ts": 0.0, "changes": -1, "data": {}})