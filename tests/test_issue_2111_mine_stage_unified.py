"""Issue #2111: /mine 出口空值归一 + detail/mine/confirm-done 状态读写源一致（Spec 锚点④）。

（#1214 移植自 weekly-1102-zrr：import 路径适配 arch11 domains 结构。）

收敛 #1214/#1999 遗留：issue 单表 stage 空值（NULL/''）在 /mine 出口透传空串，
与 detail 出口形态分歧；本切片把 /mine 出口经 _query_tasks 的 stage 空值归一为
DB 默认 vod（与 detail 同口径、同事实源 issue 单表），并验证三出口
（detail/mine/GET status）的 issue.status 读自同一事实源。

Seam：HTTP 公开接口 `GET /api/issues/mine` + `GET /api/issues/{owner}/{repo}/{n}`
+ `POST /api/issues/{id}/confirm-done`（只经公开接口断言，不测内部实现）。
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.main import app
from src.session_manager import db, db_lock
from src.domains.mgmt.auth import grape_account_db

client = TestClient(app)

_uid = None
_headers = {}


def _jwt_access_token(user_id, username="test2111mine"):
    from src.api.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def setup_module():
    global _uid, _headers
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='test2111mine_user'")
        db.commit()
    _uid = grape_account_db.create_grape_account("test2111mine_user", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test2111mine_user")
    _token = _jwt_access_token(_uid["id"], _uid["username"])
    _headers = {"Authorization": f"Bearer {_token}"}


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='test2111mine_user'")
        db.commit()


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key='gitcode:org/issue2111mine'")
        c.execute("DELETE FROM issue_state WHERE repo_key='gitcode:org/issue2111mine'")
        db.commit()


def _insert_issue(issue_number, stage=None, status="processing"):
    """插入带指定 stage 的 issue 行（stage=None 直接入库 NULL，模拟建档窗口）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "task_description, status, stage) VALUES (?,?,?,?,?,?,?,?)",
            (_uid["id"], "test2111mine_user", "gitcode:org/issue2111mine", issue_number,
             "Test #2111-mine-" + str(issue_number), "desc", status, stage))
        db.commit()
        return c.lastrowid


def _mine_items():
    r = client.get("/api/issues/mine", headers=_headers,
                   params={"repo_key": "gitcode:org/issue2111mine", "per_page": 0})
    assert r.status_code == 200
    return {it["issue_number"]: it for it in r.json().get("issues", [])}


class TestMineStageNormalization:

    def test_mine_null_stage_normalized_to_vod(self):
        """stage NULL（建档窗口）→ /mine 出口归一 vod，不再透传空串。"""
        _clean()
        _insert_issue(601, stage=None)
        items = _mine_items()
        assert items[601]["stage"] == "vod"

    def test_mine_empty_string_stage_normalized_to_vod(self):
        _clean()
        _insert_issue(602, stage="")
        items = _mine_items()
        assert items[602]["stage"] == "vod"

    def test_mine_real_stage_untouched(self):
        """非空 stage 不受归一影响（不过度矫正）。"""
        _clean()
        _insert_issue(603, stage="qa")
        items = _mine_items()
        assert items[603]["stage"] == "qa"

    def test_mine_detail_stage_consistent(self):
        """同一条 issue：/mine 与 detail 出口 stage 值一致（无出口分歧）。"""
        _clean()
        _insert_issue(604, stage=None)
        mine = _mine_items()
        r = client.get("/api/issues/org/issue2111mine/604", headers=_headers)
        assert r.status_code == 200
        detail = r.json()
        assert mine[604]["stage"] == detail["stage"] == "vod", \
            "mine 与 detail 的 stage 必须同值（均非空、同事实源口径）"


class TestStatusReadSourceUnified:
    """detail/mine/confirm-done 的 issue 状态读写源一致（同一事实源=issue 单表）。"""

    def test_three_exits_read_same_status_source(self):
        """建 issue → confirm done（VCS 关闭成功）→ 三出口 status 同值 completed。"""
        _clean()
        tid = _insert_issue(610, stage="coding")
        with patch("src.domains.runtime.routes.issues._close_vcs_issues",
                   return_value={"gitcode": "success", "github": "skipped", "message": ""}):
            r = client.post(f"/api/issues/{tid}/confirm-done", headers=_headers,
                            json={"confirmation": "user_confirmed"})
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        # GET /{id}/status（轮询通道）
        r1 = client.get(f"/api/issues/{tid}/status", headers=_headers)
        assert r1.json()["status"] == "completed"
        # /mine 出口
        mine = _mine_items()
        assert mine[610]["status"] == "completed"
        # detail 出口（task_status 字段）
        r2 = client.get("/api/issues/org/issue2111mine/610", headers=_headers)
        assert r2.status_code == 200
        assert r2.json()["task_status"] == "completed"

    def test_confirm_done_close_failure_status_visible_across_exits(self):
        """失败路径（锚点③呼应）：confirm done 关闭失败 → 响应可见 partial，而三出口
        读到的服务端终态仍一致 completed（VCS 残留 open 交对账补偿，不阻塞本地）。"""
        _clean()
        tid = _insert_issue(611, stage="coding")
        with patch("src.domains.runtime.routes.issues._close_vcs_issues",
                   return_value={"gitcode": "failed", "github": "skipped", "message": ""}):
            r = client.post(f"/api/issues/{tid}/confirm-done", headers=_headers,
                            json={"confirmation": "user_confirmed"})
        # arch11 适配：F-30 partial 聚合语义（ok=False/status=partial）是 weekly 另一片，
        # 不随 #1214 移植；arch11 现行口径下 VCS 失败经 issue_closed 字段可见（降级不阻断）。
        assert r.json()["status"] == "completed"
        assert r.json()["issue_closed"]["gitcode"] == "failed"
        r1 = client.get(f"/api/issues/{tid}/status", headers=_headers)
        assert r1.json()["status"] == "completed"
        mine = _mine_items()
        assert mine[611]["status"] == "completed"
