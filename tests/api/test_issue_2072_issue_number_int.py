"""Issue #2072 回归测试：issue_number 全端点返回 JSON 整数（int）。

复现：dev 实测 GET /api/issues/mine 的 issue_number 为字符串（DB 列/Pydantic/其他端点均为 int）。
根因：`_query_tasks()` 与任务卡 `GET /{issue_id}/status` 响应构造处直出 DB 行值，
DB 层（MySQL 兼容层 / GitCode 同步混入数据）产出的字符串编号未归一即泄漏到响应。

两层防护：
1. 契约层（HTTP）：四处公开端点断言 issue_number/number 为 int 且 JSON 序列化为 number
   ——SQLite 测试库的 INTEGER affinity 会把可归一文本写回 integer，故该层注入 int 行，
   锁定契约防回归；
2. 归一层（单元）：monkeypatch `_query_tasks` 的 fetch 结果注入字符串编号行
   （模拟 dev 上 MySQL 兼容层返回 str 的真实形态），断言响应构造层显式归一为 int
   ——Issue #2072 的红测试（修复前直出 str → 断言失败）。
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.jwt_utils import create_access_token
from src.domains.mgmt.auth import grape_account_db

client = TestClient(app)

REPO_KEY = "gitcode:o2072/r2072"
OWNER, REPO = "o2072", "r2072"


def _db():
    from src.kernel.session_manager import db, db_lock
    return db, db_lock


def _user():
    """创建临时测试账号（username 原样用于签 JWT——deps.py #343 一致性校验）。"""
    import uuid
    u = grape_account_db.create_grape_account(f"o2072_{uuid.uuid4().hex[:6]}", email=None, password="pw")
    return u["id"], u["username"]


def _seed(uid: int) -> dict:
    """注入 issue 行（编号 2071/2072），返回定位信息。

    SQLite INTEGER affinity 会把可归一文本写回 integer，无法在存储层注入字符串
    ——字符串形态由归一层单测（_StrRowFakeDB）直接在 fetch 边界注入。
    """
    db, db_lock = _db()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, org_id) "
            "VALUES (?, ?, ?, 2071, 'str-synced row', 'processing', 0)",
            (uid, "o2072_user", REPO_KEY))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, org_id) "
            "VALUES (?, ?, ?, 2072, 'int row', 'active', 0)",
            (uid, "o2072_user", REPO_KEY))
        db.commit()
        row = c.execute(
            "SELECT id FROM issue WHERE repo_key=? AND issue_number=2072", (REPO_KEY,)).fetchone()
    return {"repo_key": REPO_KEY, "owner": OWNER, "repo": REPO, "card_id": row[0]}


def _auth(uid: int, uname: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(uid, uname)}"}


@pytest.fixture()
def seeded():
    uid, uname = _user()
    info = _seed(uid)
    yield uid, uname, info
    db, db_lock = _db()
    with db_lock:
        db.cursor().execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        db.commit()


class _StrRowFakeCursor:
    """委托真实 cursor，仅对 `_query_tasks` 主查询注入字符串编号行。

    dev 上 MySQL 兼容层/同步混入数据使 `issue.issue_number` 行以 str 形态返回；
    SQLite 测试库因 INTEGER affinity 无法在存储层复现，故在 fetch 边界注入同构数据。
    """

    _MAIN_SELECT = "SELECT id, assignee, repo_key, issue_number"

    def __init__(self, inner, injected):
        self._inner = inner
        self._injected = injected
        self._rows = None

    def execute(self, sql, params=None):
        if self._MAIN_SELECT in sql and "FROM issue WHERE" in sql:
            self._rows = self._injected
            return self
        return self._inner.execute(sql, params)

    def fetchall(self):
        if self._rows is not None:
            return self._rows
        return self._inner.fetchall()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_query_tasks_normalizes_string_issue_number(seeded, monkeypatch):
    """归一层红测试（Issue #2072 主复现面）。

    `_query_tasks` 响应构造层必须把 DB 返回的字符串编号归一为 int；
    修复前 `"issue_number": r[3]` 直出 str → 本断言红。
    """
    import src.api.issues_center as ic
    db, db_lock = _db()
    uid, uname, info = seeded
    # 与 _query_tasks SELECT 列序一致（include_user_id=False）：17 列
    injected = [
        (101, "o2072_user", REPO_KEY, "2071", "str-synced row", "", "conv1", "processing",
         "", "2026-11-12 01:00:00", "", 0, 0, "", "creator", 1, ""),
        (102, "o2072_user", REPO_KEY, 2072, "int row", "", "conv2", "active",
         "", "2026-11-12 02:00:00", "", 0, 0, "", "creator", 1, ""),
    ]

    class _FakeDB:
        def __getattr__(self, name):
            return getattr(db, name)

        def cursor(self):
            return _StrRowFakeCursor(db.cursor(), injected)

    monkeypatch.setattr(ic, "db", _FakeDB())
    out = ic._query_tasks("user_id=?", [uid], "created_at DESC")
    assert out, "应返回注入的 2 行"
    for it in out:
        num = it["issue_number"]
        assert isinstance(num, int), f"issue_number 应归一为 int，实为 {type(num).__name__}: {num!r}"
    assert {it["issue_number"] for it in out} == {2071, 2072}


def test_issues_mine_issue_number_is_int(seeded):
    """/api/issues/mine：issue_number 全为 int 且 JSON 序列化为 number（契约锁定）。"""
    uid, uname, info = seeded
    r = client.get("/api/issues/mine", headers=_auth(uid, uname))
    assert r.status_code == 200
    issues = r.json()["issues"]
    assert issues, "应至少返回 2 条种子数据"
    for it in issues:
        num = it.get("issue_number")
        assert isinstance(num, int), f"issue_number 应为 int，实为 {type(num).__name__}: {num!r}"
    assert {it["issue_number"] for it in issues} == {2071, 2072}


def test_issues_list_issue_number_is_int(seeded):
    """仓库列表 GET /{owner}/{repo}：issue_number 与 number 别名均为 int。"""
    uid, uname, info = seeded
    r = client.get(f"/api/issues/{OWNER}/{REPO}", headers=_auth(uid, uname))
    assert r.status_code == 200
    items = r.json()["issues"]
    assert items
    for it in items:
        assert isinstance(it.get("issue_number"), int), it.get("issue_number")
        if it.get("number") is not None:
            assert isinstance(it["number"], int), it["number"]


def test_issue_status_card_issue_number_is_int(seeded):
    """任务卡轮询 GET /{issue_id}/status：issue_number 为 int（契约锁定）。"""
    uid, uname, info = seeded
    r = client.get(f"/api/issues/{info['card_id']}/status", headers=_auth(uid, uname))
    assert r.status_code == 200
    assert isinstance(r.json()["issue_number"], int)


def test_issue_detail_number_is_int(seeded):
    """详情 GET /{owner}/{repo}/{number}：number 为 int（契约锁定，防回归）。"""
    uid, uname, info = seeded
    r = client.get(f"/api/issues/{OWNER}/{REPO}/2072", headers=_auth(uid, uname))
    assert r.status_code == 200
    assert isinstance(r.json()["number"], int)
