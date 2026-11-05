"""Issue #926：需求中心分屏逐步加载——后端分页合并/过滤/瘦身测试。

覆盖 /api/issues/{o}/{r}：
- q / statuses 服务端过滤（issue 行 + PR 伪卡片同口径）
- PR 伪卡片并入统一 LIMIT/OFFSET 分页窗口（total 计入、逐页无重复无丢失）
- 分页响应瘦身（不回 attachments 清单，仅 attachments_count）；per_page=0 全量兼容保持
- statuses 全非法值 → 400（与 status pattern 校验口径一致）
- default 模式深分页 + PR 卡交错：无重复无丢失（窗口化取数）
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient
from src.api.main import app
from src.auth import grape_account_db
from src.api.jwt_utils import create_access_token
from src.auth.grape_account_db import get_token_version
from src.session_manager import db, db_lock

client = TestClient(app)


class TestIssue926PagedLoading:
    P_OWNER = "test926i"
    P_REPO = "paged"
    P_REPO_KEY = f"gitcode:{P_OWNER}/{P_REPO}"

    def setup_class(cls):
        cls._uid = grape_account_db.create_grape_account("test926_user", email=None, password="x")
        if not cls._uid:
            cls._uid = grape_account_db.get_grape_account_by_username("test926_user")
        cls._headers = {"Authorization": "Bearer " + create_access_token(
            cls._uid["id"], cls._uid["username"], get_token_version(cls._uid["id"]))}
        try:
            from src.api import db_setup
            db_setup.init_database()
        except Exception:
            pass

    def _mk_many(self, start, count, status, title_tpl="Task #{i}"):
        from datetime import datetime, timedelta
        _base = datetime(2026, 10, 1)
        with db_lock:
            c = db.cursor()
            for i in range(start, start + count):
                c.execute(
                    "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_at) "
                    "SELECT ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM issue WHERE repo_key=? AND issue_number=?)",
                    (self._uid["id"], "grape_bot", self.P_REPO_KEY, i,
                     title_tpl.format(i=i), status,
                     (_base + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                     self.P_REPO_KEY, i))
            db.commit()

    def _mk_pr_events(self, prs, status="completed"):
        with db_lock:
            c = db.cursor()
            for pr in prs:
                c.execute(
                    "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, summary, status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (self.P_REPO_KEY, f"#pr_{pr}_scene-926", "note_hook_pr", "qa",
                     f"PR {pr} searchtoken", f"Sum pr{pr}", status))
            db.commit()

    def _get(self, qs=""):
        return client.get(f"/api/issues/{self.P_OWNER}/{self.P_REPO}{qs}", headers=self._headers)

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=?", (self.P_REPO_KEY,))
            c.execute("DELETE FROM events WHERE repo_key=?", (self.P_REPO_KEY,))
            db.commit()

    def setup_method(self):
        self._clean()

    def teardown_method(self):
        self._clean()

    def test_q_filters_issues_and_pr_cards(self):
        self._mk_many(6100, 3, "processing", title_tpl="alpha #{i}")
        self._mk_many(6110, 2, "completed", title_tpl="beta #{i}")
        self._mk_pr_events([21])
        d = self._get("?mode=time&per_page=50&" + "q=" + "beta").json()
        nums = {it["issue_number"] for it in d["issues"]}
        assert all("beta" in (it.get("title") or "") or it.get("is_pr_event") for it in d["issues"])
        assert 6110 in nums and 6111 in nums and 6100 not in nums
        # PR 卡标题含 searchtoken，q=PR 不应返回 issue 行
        d2 = self._get("?mode=time&per_page=50&q=searchtoken").json()
        assert [it for it in d2["issues"] if not it.get("is_pr_event")] == []
        assert len([it for it in d2["issues"] if it.get("is_pr_event")]) == 1

    def test_statuses_multi_filter(self):
        self._mk_many(6200, 2, "active")
        self._mk_many(6210, 2, "processing")
        self._mk_many(6220, 2, "completed")
        d = self._get("?mode=time&per_page=50&statuses=active,processing").json()
        st = {it["status"] for it in d["issues"]}
        assert st <= {"active", "processing"}
        assert len(d["issues"]) == 4
        # 单状态
        d2 = self._get("?mode=time&per_page=50&statuses=completed").json()
        assert len(d2["issues"]) == 2
        assert all(it["status"] == "completed" for it in d2["issues"])
        # 白名单外的值被忽略（防注入/脏参数，合法值仍生效）
        d3 = self._get("?mode=time&per_page=50&statuses=completed,evil").json()
        assert len(d3["issues"]) == 2
        # 全非法值：与 status 参数 pattern 校验口径一致 → 400（而非静默退化为无过滤）
        d4 = self._get("?mode=time&per_page=50&statuses=evil,evil2")
        assert d4.status_code == 400

    def test_paged_attachments_slimmed(self):
        self._mk_many(6300, 3, "processing")
        d = self._get("?mode=time&per_page=2&page=1").json()
        assert len(d["issues"]) == 2
        for it in d["issues"]:
            assert "attachments" not in it          # 分页模式不内联清单字段（前端按 count 异步填充）
            assert isinstance(it.get("attachments_count"), int)  # 只回 count
        # per_page=0 全量兼容：attachments 仍为完整清单
        d0 = self._get("?mode=time&recent_terminal=0").json()
        assert all(isinstance(it.get("attachments"), list) for it in d0["issues"])

    def test_scroll_pages_no_dup_no_loss(self):
        """连续滚动 5 屏：无重复无丢失，累计条数与 total 一致。"""
        self._mk_many(6400, 12, "processing")
        self._mk_many(6412, 3, "completed")
        seen, pg = [], 1
        while True:
            d = self._get(f"?mode=time&per_page=3&page={pg}").json()
            seen.extend([it["issue_number"] for it in d["issues"]])
            if not d["has_more"]:
                break
            pg += 1
            assert pg < 30
        assert d["total"] == 15
        assert len(seen) == 15 and len(set(seen)) == 15

    def test_default_mode_merged_ordering(self):
        """mode=default：合并窗口内非终态在前（active<processing<…），组内 created_at 降序。"""
        self._mk_many(6500, 2, "completed")
        self._mk_many(6510, 2, "processing")
        self._mk_many(6520, 2, "active")
        d = self._get("?mode=default&per_page=6&page=1").json()
        sts = [it["status"] for it in d["issues"]]
        assert sts == ["active", "active", "processing", "processing", "completed", "completed"]

    def test_default_mode_awaiting_pending_not_lost(self):
        """Issue #926 复审：default 模式 SQL/Python 排序口径统一为 4 级（含 awaiting_input/pending_review），
        旧版 awaiting_input 行被排入终态桶后可能永不出现在任何页——现按 4 级序逐页无丢失。"""
        self._mk_many(6550, 2, "awaiting_input")
        self._mk_many(6560, 2, "pending_review")
        self._mk_many(6570, 2, "active")
        self._mk_many(6580, 2, "processing")
        seen, pg = [], 1
        while True:
            d = self._get(f"?mode=default&per_page=2&page={pg}").json()
            seen.extend([(it["issue_number"], it["status"]) for it in d["issues"]])
            if not d["has_more"]:
                break
            pg += 1
            assert pg < 30
        assert len(seen) == 8 and len(set(n for n, _ in seen)) == 8
        # 逐页滚动序遵循 default 状态优先序（active=0 < processing=1 < awaiting_input=2 < pending_review=3 < 终态=4）
        pri = {"active": 0, "processing": 1, "awaiting_input": 2, "pending_review": 3, "completed": 4}
        seq = [pri[s] for _, s in seen]
        assert seq == sorted(seq), f"页间滚动序违背 default 状态优先序: {seq}"

    def test_default_mode_deep_pages_with_pr_cards(self):
        """Issue #926 复审：default 模式深分页 + PR 卡交错合并（窗口化取数）无重复无丢失。"""
        self._mk_many(6600, 12, "processing")
        self._mk_many(6612, 4, "awaiting_input")   # 第 2 级，与 processing 行跨页交错
        self._mk_pr_events([31, 32])               # PR 伪卡并入合并序
        seen, prs, pg = [], [], 1
        while True:
            d = self._get(f"?mode=default&per_page=3&page={pg}").json()
            for it in d["issues"]:
                if it.get("is_pr_event"):
                    prs.append(it["issue_number"])
                else:
                    seen.append(it["issue_number"])
            if not d["has_more"]:
                break
            pg += 1
            assert pg < 30
        assert d["total"] == 18
        assert len(seen) == 16 and len(set(seen)) == 16   # 16 条 issue 行不丢不重
        assert sorted(prs) == [31, 32]                    # 2 张 PR 卡恰好各出现一次
