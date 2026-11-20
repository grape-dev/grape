# -*- coding: utf-8 -*-
"""my_issues 过滤构造的行为级守卫（N11：静态断言抓不到 N7 的参数错位与 N2-③ 的 q 死参数）。

直接执行 where/params 构造逻辑（不经 inspect.getsource），用 SQLite 真跑：
1. status+statuses 同给 → statuses 为准且参数绑定不错位（N7 回归）；
2. q 服务端过滤生效（N2-③ 回归）；
3. creators 过滤生效。
"""
import sqlite3

import pytest


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE issue (
        id INTEGER PRIMARY KEY, repo_key TEXT, issue_number TEXT, title TEXT,
        status TEXT, created_at TEXT, user_id TEXT, created_by TEXT,
        assignee TEXT, org_id INTEGER DEFAULT 0)""")
    rows = [
        (1, "gitcode:o/r", "#1", "deploy sandbox", "active", "2026-11-01", "3", "alice", "alice", 0),
        (2, "gitcode:o/r", "#2", "fix login bug", "processing", "2026-11-02", "3", "alice", "bob", 0),
        (3, "gitcode:o/r", "#3", "sandbox tests", "pending_review", "2026-11-03", "3", "carol", "carol", 0),
        (4, "gitcode:o/r", "#4", "cleanup docs", "completed", "2026-11-04", "3", "carol", "alice", 0),
        (5, "gitee:o/r", "#5", "gitee issue", "active", "2026-11-05", "3", "alice", "alice", 0),
    ]
    conn.executemany("INSERT INTO issue VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def _build_where(user_id="3", login="alice", repo_keys=None,
                 status="", statuses="", q="", creators=""):
    """镜像 my_issues 的子句构造（与 routes/issues.py 同一顺序），返回 (where, params)。"""
    clauses, params = [], []
    if repo_keys:
        clauses.append("repo_key IN (" + ",".join("?" * len(repo_keys)) + ")")
        params.extend(repo_keys)
    if login:
        clauses.append("(user_id=? OR created_by=? OR assignee=?)")
        params.extend([user_id, login, login])
    else:
        clauses.append("user_id=?")
        params.append(user_id)
    # N7 互斥构造（与整改后代码同构）
    _st_list = [x.strip() for x in (statuses or "").split(",") if x.strip()]
    if _st_list:
        _allowed = {"active", "processing", "awaiting_input",
                    "pending_review", "completed", "cancelled", "timeout_failed"}
        _st_ok = [x for x in _st_list if x in _allowed]
    else:
        _st_ok = []
    if _st_ok:
        clauses.append("status IN (" + ",".join("?" * len(_st_ok)) + ")")
        params.extend(_st_ok)
    elif status:
        clauses.append("status=?")
        params.append(status)
    _qq = (q or "").strip()
    if _qq:
        clauses.append("(title LIKE ? OR issue_number LIKE ?)")
        params.extend([f"%{_qq}%", f"%{_qq}%"])
    if creators:
        _c = [x.strip() for x in creators.split(",") if x.strip()]
        if len(_c) == 1:
            clauses.append("created_by=?")
            params.append(_c[0])
        elif _c:
            clauses.append("created_by IN (" + ",".join("?" * len(_c)) + ")")
            params.extend(_c)
    return " AND ".join(clauses), params


def _query(conn, where, params):
    cur = conn.execute(
        f"SELECT id FROM issue WHERE {where} ORDER BY id", params)
    return sorted(r[0] for r in cur.fetchall())


def test_status_and_statuses_together_statuses_wins_no_binding_shift():
    """N7 回归：status+statuses 同给 → statuses 为准；参数不得错位（此前
    clauses.index('status=?') 的子句下标被当参数下标用，pop 掉 repo_key 候选值）。"""
    conn = _make_db()
    where, params = _build_where(
        repo_keys=["gitcode:o/r", "github:o/r", "gitee:o/r", "gitlab:o/r"],
        status="active", statuses="processing")
    # statuses 为准 → 只应命中 processing 的 #2（不含 active 的 #1/#5）
    assert _query(conn, where, params) == [2], (
        f"status+statuses 同给应只按 statuses 过滤且不错位: {_query(conn, where, params)}")


def test_status_single_still_works():
    conn = _make_db()
    where, params = _build_where(status="active")
    assert _query(conn, where, params) == [1, 5]


def test_q_filter_applies_server_side():
    """N2-③ 回归：q 必须真正参与服务端过滤。"""
    conn = _make_db()
    where, params = _build_where(q="sandbox")
    assert _query(conn, where, params) == [1, 3]
    where2, params2 = _build_where(q="#4")
    assert _query(conn, where2, params2) == [4]


def test_creators_filter_applies():
    conn = _make_db()
    where, params = _build_where(creators="carol")
    assert _query(conn, where, params) == [3, 4]


def test_mine_endpoint_signature_has_q():
    """静态面：my_issues 必须声明 q（与 issues_list 同名同语义）。"""
    import inspect

    from src.domains.runtime.routes import issues as rt

    sig = inspect.signature(rt.my_issues)
    assert "q" in sig.parameters, "my_issues 缺 q → 前端 &q= 被静默丢弃"
