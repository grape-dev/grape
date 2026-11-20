"""Issue #2082（#2082 移植）：issues 组服务端分页语义（list 默认注入/mine 分页/milestones 信封）。

Port from weekly-1102-zrr tests/test_issues_api.py（TestIssuesDefaultPaging2082 /
TestMineIssuesPaging2082）与 tests/test_git_repo_api.py（milestones 分页组），
适配 arch11 domains 路径，独立成文件。
"""
import os
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.kernel.session_manager import db, db_lock
from src.api.deps import get_current_user_or_api_key

UID = "u2082"
OWNER = "test_issues_2082"
REPO = "hello"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"

client = TestClient(app)
_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    from src.api.db_setup import init_database
    init_database()
    from src.domains.mgmt.auth import grape_account_db
    _uid = grape_account_db.create_grape_account("test_issues_2082", email=None, password="test123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test_issues_2082")
    # mine 端点走 require_org_member（JWT）——签真实 JWT 头（D105 UUID 兼容）
    from src.api.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    global _headers
    _headers = {"Authorization": "Bearer " + create_access_token(_uid["id"], _uid["username"], get_token_version(_uid["id"]))}


def teardown_module():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM grape_accounts WHERE username='test_issues_2082'")
        db.commit()


BOT = "bot_tok_2082"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token", lambda *a, **k: BOT)
    monkeypatch.setattr("src.domains.mgmt.user.db.get_github_token", lambda *a, **k: BOT)


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = \
        lambda: {"id": UID, "username": "git_tester", "auth_type": "session_token",
                 "agent_type": "vod", "user_id": 0, "session_key": "s1",
                 "repo_key": f"gitcode:{OWNER}/{REPO}"}
    yield TestClient(app)
    app.dependency_overrides.clear()



# ===================== Issue #2082：issues list/mine 默认分页（分页三段补齐） =====================

class TestIssuesDefaultPaging2082:
    """Issue #2082：/api/issues/{o}/{r} 无 per_page 时注入默认 20（灰度开关 ISSUES_DEFAULT_PER_PAGE）。

    语义统一：page 1-based、per_page 默认 20、上限 100（超限夹取不报 400）、
    per_page=0 显式全量（逃生口）；灰度开关置 0 回退 legacy 全量+终态截断。
    """

    OWNER = "test2082"
    REPO = "defpage"
    REPO_KEY = "gitcode:test2082/defpage"

    def _mk_many(self, start, count, status):
        """批量建卡片行（created_at 随编号单调递增，mode=time 下编号序=时间序）。"""
        from datetime import datetime, timedelta
        _base = datetime(2026, 11, 1)
        with db_lock:
            c = db.cursor()
            for i in range(start, start + count):
                c.execute(
                    "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_at) "
                    "SELECT ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM issue WHERE repo_key=? AND issue_number=?)",
                    (_uid["id"], "grape_bot", self.REPO_KEY, i, f"Task #{i}", status,
                     (_base + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                     self.REPO_KEY, i))
            db.commit()

    def _get(self, qs=""):
        return client.get(f"/api/issues/{self.OWNER}/{self.REPO}{qs}", headers=_headers)

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=?", (self.REPO_KEY,))
            c.execute("DELETE FROM events WHERE repo_key=?", (self.REPO_KEY,))
            db.commit()

    def setup_method(self):
        self._clean()
        import os as _os
        _os.environ.pop("ISSUES_DEFAULT_PER_PAGE", None)

    def teardown_method(self):
        self._clean()
        import os as _os
        _os.environ.pop("ISSUES_DEFAULT_PER_PAGE", None)

    def test_default_injects_per_page_20(self):
        """不带分页参数 → 默认注入 per_page=20，信封含 page/per_page/total/has_more。"""
        self._mk_many(6000, 25, "processing")
        resp = self._get("?mode=time")
        assert resp.status_code == 200
        d = resp.json()
        assert len(d["issues"]) == 20, "默认应只返回 20 条（终结全量倾泻）"
        assert d["page"] == 1 and d["per_page"] == 20
        assert d["total"] == 25
        assert d["has_more"] is True

    def test_default_last_page_has_more_false(self):
        """末页判定：total=25、per_page=20 → page=2 仅 5 条且 has_more=False。"""
        self._mk_many(6000, 25, "processing")
        d = self._get("?mode=time&page=2").json()
        assert d["page"] == 2 and d["per_page"] == 20
        assert len(d["issues"]) == 5
        assert d["has_more"] is False

    def test_per_page_0_explicit_full_escape(self):
        """per_page=0 逃生口：显式全量（legacy 形态 {issues, total}，无分页字段）。"""
        self._mk_many(6000, 35, "completed")
        resp = self._get("?mode=time&per_page=0")
        assert resp.status_code == 200
        d = resp.json()
        assert len(d["issues"]) == 35
        assert d["total"] == 35
        assert "has_more" not in d and "page" not in d

    def test_per_page_over_100_clamped(self):
        """per_page 超上限（101）→ 夹取 100，不 422/400（Agent 中断防护）。"""
        self._mk_many(6000, 105, "processing")
        d = self._get("?mode=time&per_page=101").json()
        assert d["per_page"] == 100
        assert len(d["issues"]) == 100
        assert d["total"] == 105
        assert d["has_more"] is True

    def test_per_page_100_boundary(self):
        """per_page=100（上限值）原样生效，不夹取。"""
        self._mk_many(6000, 105, "processing")
        d = self._get("?mode=time&per_page=100").json()
        assert d["per_page"] == 100
        assert len(d["issues"]) == 100

    def test_env_switch_off_returns_legacy(self):
        """灰度开关 ISSUES_DEFAULT_PER_PAGE=0 → 默认请求回退 legacy 全量+终态截断（现状一致）。"""
        import os as _os
        _os.environ["ISSUES_DEFAULT_PER_PAGE"] = "0"
        # 2 条非终态 + 35 条终态 → legacy 默认 = 全量非终态 + 终态最近 30 = 32 条
        self._mk_many(6000, 2, "processing")
        self._mk_many(6100, 35, "completed")
        resp = self._get("?mode=time")
        assert resp.status_code == 200
        d = resp.json()
        assert len(d["issues"]) == 32
        assert d["total"] == 32
        # legacy 形态：无 page/per_page/has_more 分页键
        assert "has_more" not in d and "page" not in d and "per_page" not in d

    def test_env_switch_default20_no_env(self):
        """灰度开关未设置 → 默认 20（本测试兜底防实现把缺省误置 0）。"""
        self._mk_many(6000, 25, "processing")
        d = self._get("?mode=time").json()
        assert d["per_page"] == 20




class TestMineIssuesPaging2082:
    """Issue #2082：/api/issues/mine 分页链路补齐（page/per_page 解析 + 分页信封）。

    语义与 issues list 统一：page 1-based、per_page 缺省=默认 20（灰度开关）、
    上限 100 夹取、per_page=0 显式全量（legacy 形态 {issues, sort_order, mode}）。
    独立仓库段隔离（repo_key 过滤），total/has_more 与过滤后集合一致。
    """

    REPO_SEG = "test2082/mine"
    REPO_KEY = "gitcode:test2082/mine"

    def _mk_many(self, start, count, status):
        from datetime import datetime, timedelta
        _base = datetime(2026, 11, 1)
        with db_lock:
            c = db.cursor()
            for i in range(start, start + count):
                c.execute(
                    "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, created_at) "
                    "SELECT ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM issue WHERE repo_key=? AND issue_number=?)",
                    (_uid["id"], "grape_bot", self.REPO_KEY, i, f"Task #{i}", status,
                     (_base + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                     self.REPO_KEY, i))
            db.commit()

    def _get(self, qs=""):
        if qs.startswith("?"):
            qs = "&" + qs[1:]
        from src.api.main import app as _app
        _saved = dict(_app.dependency_overrides)
        _app.dependency_overrides.clear()  # mine 走真 JWT（_client 的假 override 会吞掉用户身份）
        try:
            return client.get(f"/api/issues/mine?repo_key={self.REPO_SEG}{qs}", headers=_headers)
        finally:
            _app.dependency_overrides.update(_saved)

    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM issue WHERE repo_key=?", (self.REPO_KEY,))
            db.commit()

    def setup_method(self):
        self._clean()
        import os as _os
        _os.environ.pop("ISSUES_DEFAULT_PER_PAGE", None)

    def teardown_method(self):
        self._clean()
        import os as _os
        _os.environ.pop("ISSUES_DEFAULT_PER_PAGE", None)

    def test_mine_default_injects_per_page_20(self):
        """不带分页参数 → 默认 20，信封含 page/per_page/total/has_more（与 list 语义统一）。"""
        self._mk_many(6200, 25, "processing")
        resp = self._get("?mode=time")
        assert resp.status_code == 200
        d = resp.json()
        assert len(d["issues"]) == 20
        assert d["page"] == 1 and d["per_page"] == 20
        assert d["total"] == 25
        assert d["has_more"] is True
        # 存量字段不回归
        assert d["mode"] == "time" and d["sort_order"] == "desc"

    def test_mine_page_turn_no_overlap_no_gap(self):
        """page=2&per_page=20 翻页：1、2 页并集不重不漏，末页 has_more=False。"""
        self._mk_many(6200, 25, "processing")
        nums = []
        d1 = self._get("?mode=time&page=1&per_page=20").json()
        nums.extend(it["issue_number"] for it in d1["issues"])
        d2 = self._get("?mode=time&page=2&per_page=20").json()
        nums.extend(it["issue_number"] for it in d2["issues"])
        assert d1["total"] == 25 and d2["total"] == 25
        assert len(nums) == 25 and len(set(nums)) == 25, "翻页不重不漏"
        assert d1["has_more"] is True and d2["has_more"] is False
        assert len(d2["issues"]) == 5

    def test_mine_per_page_0_explicit_full_escape(self):
        """per_page=0 逃生口：全量返回，legacy 形态 {issues, sort_order, mode}。"""
        self._mk_many(6200, 25, "processing")
        resp = self._get("?mode=time&per_page=0")
        assert resp.status_code == 200
        d = resp.json()
        assert len(d["issues"]) == 25
        assert set(d.keys()) == {"issues", "sort_order", "mode"}, "全量模式应与现状形态完全一致"

    def test_mine_env_switch_off_legacy(self):
        """灰度开关 ISSUES_DEFAULT_PER_PAGE=0 → 默认请求回退 legacy 全量（现状一致）。"""
        import os as _os
        _os.environ["ISSUES_DEFAULT_PER_PAGE"] = "0"
        self._mk_many(6200, 25, "processing")
        resp = self._get("?mode=time")
        assert resp.status_code == 200
        d = resp.json()
        assert len(d["issues"]) == 25
        assert set(d.keys()) == {"issues", "sort_order", "mode"}

    def test_mine_per_page_over_100_clamped(self):
        """per_page=101 → 夹取 100（不 422/400），信封回显夹取值。"""
        self._mk_many(6200, 105, "processing")
        d = self._get("?mode=time&per_page=101").json()
        assert d["per_page"] == 100
        assert len(d["issues"]) == 100
        assert d["total"] == 105
        assert d["has_more"] is True

    def test_mine_last_page_has_more_false(self):
        """末页边界：total=25、per_page=20 → page=2 仅 5 条、has_more=False。"""
        self._mk_many(6200, 25, "processing")
        d = self._get("?mode=time&page=2").json()
        assert len(d["issues"]) == 5
        assert d["has_more"] is False


# ===================== Issue #2082：milestones 默认分页 + 信封 =====================

def _ms_provider(monkeypatch, total=25, page_size=100):
    """里程碑上游 mock：按 page 参数返回 total 条（分页每页 page_size 条），记录请求 URL。

    返回 (milestones列表, calls)。条目标题 `m{n}`、state 交替 active/closed（覆盖归一化）。
    """
    items = [{"id": i + 1, "number": i + 1, "title": f"m{i + 1}",
              "state": "active" if i % 2 == 0 else "closed"} for i in range(total)]
    calls = []

    def _fake_req(method, url, token="", body=None):
        calls.append({"method": method, "url": url})
        if method == "GET" and "/milestones" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            page_no = int(q.get("page", ["1"])[0])
            per = int(q.get("per_page", [str(page_size)])[0])
            window = items[(page_no - 1) * per: page_no * per]
            return list(window)
        return {"ok": True}
    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    return items, calls


def test_milestones_default_envelope_paged_20(monkeypatch, _client):
    """Issue #2082：默认请求 → 信封 {milestones,total,page,per_page,has_more}，默认 ≤20 条不静默截断。"""
    items, _ = _ms_provider(monkeypatch, total=25)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/milestones")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d, dict), "默认应为信封对象（D3：信封为默认）"
    assert d["page"] == 1 and d["per_page"] == 20
    assert d["total"] == 25
    assert len(d["milestones"]) == 20
    assert d["has_more"] is True
    # 归一化仍在信封内生效（active → open）
    assert all(m["state"] in ("open", "closed") for m in d["milestones"])


def test_milestones_page_turn_no_overlap_no_gap(monkeypatch, _client):
    """page=2&per_page=2 翻页正确：不重不漏、total 恒定、末页 has_more=False。"""
    items, _ = _ms_provider(monkeypatch, total=5)
    base = f"/api/git-repo/{OWNER}/{REPO}/milestones"
    nums = []
    for pg in (1, 2, 3):
        d = _client.get(f"{base}?per_page=2&page={pg}").json()
        assert d["total"] == 5
        nums.extend(m["number"] for m in d["milestones"])
    assert nums == [1, 2, 3, 4, 5], "翻页不重不漏（顺序=上游序）"
    d_last = _client.get(f"{base}?per_page=2&page=3").json()
    assert len(d_last["milestones"]) == 1 and d_last["has_more"] is False
    d_beyond = _client.get(f"{base}?per_page=2&page=4").json()
    assert d_beyond["milestones"] == [] and d_beyond["has_more"] is False


def test_milestones_per_page_0_explicit_full(monkeypatch, _client):
    """per_page=0 逃生口：全量返回，has_more=False、per_page=0。"""
    _items, _ = _ms_provider(monkeypatch, total=25)
    d = _client.get(f"/api/git-repo/{OWNER}/{REPO}/milestones?per_page=0").json()
    assert d["per_page"] == 0
    assert len(d["milestones"]) == 25
    assert d["total"] == 25
    assert d["has_more"] is False


def test_milestones_per_page_over_100_clamped(monkeypatch, _client):
    """per_page=101 → 夹取 100（provider 上限一致口径，不 422/400）。"""
    _items, _ = _ms_provider(monkeypatch, total=105)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/milestones?per_page=101")
    assert r.status_code == 200
    d = r.json()
    assert d["per_page"] == 100
    assert len(d["milestones"]) == 100
    assert d["total"] == 105
    assert d["has_more"] is True


def test_milestones_provider_fetch_capped_100_and_multi_page(monkeypatch, _client):
    """服务端向上游翻页取全量：per_page 恒为 100（provider 上限夹取），短页终止。"""
    items, calls = _ms_provider(monkeypatch, total=105)
    d = _client.get(f"/api/git-repo/{OWNER}/{REPO}/milestones").json()
    assert d["total"] == 105, "105 条 > 单页 100 → 需两轮上游拉取"
    ms_urls = [c["url"] for c in calls if "/milestones" in c["url"]]
    assert len(ms_urls) == 2, "105 条应触发 2 轮上游请求（100 + 5 短页终止）"
    assert all("per_page=100" in u for u in ms_urls), "上游每页恒 100（夹取）"
    assert "page=1" in ms_urls[0] and "page=2" in ms_urls[1]
    assert len(d["milestones"]) == 20


def test_milestones_shape_array_compat_full_list(monkeypatch, _client):
    """shape=array 兼容期逃生口：顶层数组（`jq '.[]'` 口径零回归），且为全量不再静默截断。"""
    _items, _ = _ms_provider(monkeypatch, total=25)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/milestones?shape=array")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d, list), "shape=array 应返回顶层数组（兼容期）"
    assert len(d) == 25, "array 兼容形态=全量（防旧调用方静默丢数据）"
    assert [m["number"] for m in d] == list(range(1, 26))


def test_milestones_invalid_shape_422(_client):
    """非法 shape → 422（契约 pattern 校验，不静默降级）。"""
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/milestones?shape=bogus")
    assert r.status_code == 422
