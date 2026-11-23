# -*- coding: utf-8 -*-
"""Issue #3771：「我的需求」分页 COUNT 必须走 runtime 域连接。

背景（生产实测）：`GET /api/issues/mine` 由 mgmt 进程服务，`issue` 表归 runtime 域
（`configs/table_ownership.json`）。`issues.py:2327` 曾裸用 `session_manager.db.cursor()`
执行 `SELECT COUNT(*) FROM issue`，三库拓扑下打到 mgmt 库同名视图 → 恒 1146；异常被吞后
`total` 落成 0，`has_more` 因 `_total=0` 恒为 false（1,693 条/小时 WARNING）。

本用例锁四件事：
1. mgmt 上下文（runtime 域有自有 DSN）下 COUNT 取 runtime 域连接，`total` 与真实条数一致；
2. COUNT 异常按 `issues_list` 既有降级口径返回 `total=None` + 满页推断 `has_more`（不再回 0）；
3. `per_page=0`（legacy 全量）不产生任何跨域连接，响应形态不变；
4. 静态守门：`issue` 表 COUNT 在 `issues.py` 只剩一处（`_count_issues`），无 `db.cursor()` 裸调用。
"""
import ast
import inspect
from pathlib import Path

import pytest

from src.domains.runtime.routes import issues as rt
from src.kernel import db_registry


class _FakeCursor:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.executed = []
        self.closed = False

    def execute(self, sql, args=None):
        self.executed.append((sql, args))
        if self.error is not None:
            raise self.error
        return self

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [self.row] if self.row is not None else []

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self, *a, **kw):
        return self._cursor

    def close(self):
        self.closed = True


def _stub_queries(monkeypatch, items):
    """打桩行数据查询与登录名反查（本用例只验证 COUNT 的连接域与分页元数据）。"""
    import src.domains.runtime.issues_center as ic
    monkeypatch.setattr(ic, "_query_tasks",
                        lambda *a, **k: [dict(it) for it in items])
    monkeypatch.setattr(ic, "_get_gitcode_user_by_user_id",
                        lambda *a, **k: "alice")


def _patch_mgmt_topology(monkeypatch, cursor):
    """模拟三库拓扑：进程域=mgmt，runtime 域有自有 DSN（每次取独立连接）。

    Issue #3826：路由判定收敛到内核 `db_registry.route_of()` 单一事实源后，本 seam 由
    `uses_own_dsn` 迁到 `route_of` 的依赖面（`lib_enabled` / `current_domain` / `dsn_of`），
    让用例走真实路由逻辑而不是绕开它。
    """
    conn = _FakeConn(cursor)
    created = []

    def _new_connection(domain, autocommit=True):
        created.append(domain)
        return conn

    monkeypatch.setattr(db_registry, "lib_enabled", lambda: True)
    monkeypatch.setattr(db_registry, "current_domain", lambda: "mgmt")
    monkeypatch.setattr(db_registry, "dsn_of",
                        lambda d: "mysql://u:p@h:3306/grape_prod_runtime" if d == "runtime" else "")
    monkeypatch.setattr(db_registry, "new_connection", _new_connection)
    return created


def _items(n):
    return [{"id": i, "title": f"issue-{i}"} for i in range(n)]


_MINE_DEFAULTS = dict(status="", repo_key="", sort_order="desc", mode="default",
                      created_by_me="", assignees="", creators="", statuses="", q="")


def _call_mine(**overrides):
    """直接调用端点（绕开 FastAPI Query 默认值）：字符串过滤参数显式给空串。"""
    kwargs = dict(_MINE_DEFAULTS)
    kwargs.update(overrides)
    return rt.my_issues(**kwargs)


# ── ① mgmt 上下文：COUNT 走 runtime 域 ────────────────────────────────────
def test_mine_paged_count_uses_runtime_domain(monkeypatch):
    items = _items(20)
    _stub_queries(monkeypatch, items)
    cur = _FakeCursor(row=(1693,))
    created = _patch_mgmt_topology(monkeypatch, cur)

    out = _call_mine(per_page=20, page=1, user={"id": "u3771"})

    assert created == ["runtime"], f"COUNT 未取 runtime 域连接，实际取域={created}"
    assert len(cur.executed) == 1, f"域连接上应恰好一条 COUNT，实际={cur.executed}"
    sql, args = cur.executed[0]
    assert sql.startswith("SELECT COUNT(*) FROM issue WHERE"), sql
    assert isinstance(args, list), f"COUNT 参数应为列表，实际={type(args)}"
    # 真实条数（runtime 域）而非 mgmt 库同名视图的 1146，也不是异常吞掉后的 0
    assert out["total"] == 1693, f"total 未取域连接真实值: {out['total']}"
    assert out["total"] not in (0, 1146)
    assert out["has_more"] is True, "首页 20 条 + total=1693 应还有下一页"
    assert out["page"] == 1 and out["per_page"] == 20
    assert out["issues"] == items


def test_mine_last_page_has_more_false(monkeypatch):
    """末页（offset 1680 + 20 条 ≥ total 1693）has_more 必须为 false。"""
    _stub_queries(monkeypatch, _items(13))
    cur = _FakeCursor(row=(1693,))
    _patch_mgmt_topology(monkeypatch, cur)

    out = _call_mine(per_page=20, page=85, user={"id": "u3771"})

    assert out["total"] == 1693
    assert out["has_more"] is False


def test_mine_count_keeps_q_filter_in_where(monkeypatch):
    """q 过滤口径不变：COUNT 的 where/params 仍带 LIKE 条件，total 仍为精确值。"""
    _stub_queries(monkeypatch, _items(3))
    cur = _FakeCursor(row=(17,))
    _patch_mgmt_topology(monkeypatch, cur)

    out = _call_mine(per_page=20, page=1, q="deploy", user={"id": "u3771"})

    assert "title LIKE ?" in cur.executed[0][0]
    assert "%deploy%" in cur.executed[0][1]
    assert out["total"] == 17


# ── ② COUNT 异常：total=None + 满页推断 dict ──────────────────────────────
def test_mine_count_failure_degrades_to_null_total(monkeypatch):
    _stub_queries(monkeypatch, _items(20))
    cur = _FakeCursor(error=RuntimeError("runtime db down"))
    _patch_mgmt_topology(monkeypatch, cur)

    out = _call_mine(per_page=20, page=1, user={"id": "u3771"})

    assert out["total"] is None, f"COUNT 失败不应落成误导性的 0，实际={out['total']}"
    assert out["has_more"] is True, "满页（20/20）应推断还有下一页"
    assert len(out["issues"]) == 20


def test_mine_count_failure_partial_page_has_more_false(monkeypatch):
    _stub_queries(monkeypatch, _items(5))
    cur = _FakeCursor(error=RuntimeError("runtime db down"))
    _patch_mgmt_topology(monkeypatch, cur)

    out = _call_mine(per_page=20, page=2, user={"id": "u3771"})

    assert out["total"] is None
    assert out["has_more"] is False, "未满页应推断已到末页"


# ── ③ per_page=0：无跨域读，legacy 形态不变 ──────────────────────────────
def test_mine_per_page_zero_no_domain_read(monkeypatch):
    items = _items(3)
    _stub_queries(monkeypatch, items)
    cur = _FakeCursor(row=(1693,))
    created = _patch_mgmt_topology(monkeypatch, cur)

    out = _call_mine(per_page=0, user={"id": "u3771"})

    assert created == [], f"per_page=0 不应发起 COUNT/跨域连接，实际={created}"
    assert cur.executed == []
    assert "total" not in out and "has_more" not in out
    assert out["issues"] == items


# ── ④ 静态守门：COUNT 单点收口 ───────────────────────────────────────────
def _module_source() -> str:
    return Path(rt.__file__).read_text(encoding="utf-8")


def _bare_db_cursor_lines(src: str):
    """返回源码里 `xxx = db.cursor(...)` 形态的调用行号（绕过 docstring 误报）。"""
    hits = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cursor"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "db"):
            hits.append(node.lineno)
    return hits


def test_issue_count_goes_through_single_helper():
    src = _module_source()
    assert hasattr(rt, "_count_issues"), "issue 表 COUNT 未收口到 _count_issues"
    marker = "SELECT COUNT(*) FROM issue WHERE {where}"
    assert src.count(marker) == 1, (
        f"issue 表 COUNT 应只有一处（_count_issues），实际 {src.count(marker)} 处")
    assert marker in inspect.getsource(rt._count_issues)
    assert "_rt_db.cursor()" in inspect.getsource(rt._count_issues), (
        "COUNT 收口必须走 runtime 域代理 _rt_db")


def test_no_bare_process_db_cursor_in_issues_routes():
    hits = _bare_db_cursor_lines(_module_source())
    assert hits == [], (
        f"issues.py 出现裸 session_manager.db.cursor() 调用（行 {hits}）——"
        "issue 表归 runtime 域，须走 _rt_db（Issue #3771 回归）")


def test_count_helper_returns_none_on_failure(monkeypatch):
    """辅助函数契约：失败返回 None（由调用方决定降级），不抛也不回 0。"""
    cur = _FakeCursor(error=RuntimeError("boom"))
    _patch_mgmt_topology(monkeypatch, cur)
    assert rt._count_issues("1=1", [], label="issues mine") is None


@pytest.mark.parametrize("label", ["issues", "issues mine"])
def test_count_helper_keeps_existing_warning_text(monkeypatch, caplog, label):
    """监控口径不变：两条路径仍分别记录 `issues: count failed` / `issues mine: count failed`。"""
    cur = _FakeCursor(error=RuntimeError("boom"))
    _patch_mgmt_topology(monkeypatch, cur)
    with caplog.at_level("WARNING"):
        assert rt._count_issues("1=1", [], label=label) is None
    assert f"{label}: count failed" in caplog.text