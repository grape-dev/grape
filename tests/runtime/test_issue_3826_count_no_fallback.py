# -*- coding: utf-8 -*-
"""Issue #3826：issue COUNT 失败日志可观测化 + 三库拓扑**绝不回落进程主库**。

背景（生产实测 2026-11-23）：#3771（`36157553e`）把 `_count_issues()` 改走 `_rt_db.cursor()`
之后，4 台主控仍在刷 `issues mine: count failed: (1146, "Table 'grape_prod_mgmt.issue' doesn't
exist")`。旧实现 `_DomainDbProxy.cursor()` 自带第二套语义 ——
`current_domain() != "runtime"` 且 `uses_own_dsn("runtime") == false` 时静默回落
`session_manager.db`（mgmt 主库），与内核 `db_registry.route_of()`「缺 DSN 显式
DomainUnavailable，绝不回落」契约并存。二者日志同形，导致「旧进程未重启」与「新代码打错库」
无法区分，验收反复失效。

本用例锁四件事：
1. 三库拓扑缺 `GRAPE_DSN_RUNTIME` 时 `_rt_db.cursor()` 抛 `DomainUnavailable`，且**完全不碰**
   `session_manager.db`；`_count_issues` 降级为 `None`（`total=None` + 满页推断 `has_more`）；
2. COUNT 失败 WARNING 带 `route=<domain|local|unavailable>` + `target_db=<库名>` + `pid` + `host`，
   且不含账号/口令/完整 DSN；
3. 拓扑棘轮：`_DomainDbProxy` 只认 `route_of("runtime")`，不得再出现 `uses_own_dsn` 回落写法；
4. 全仓 COUNT 审计：`src/` 内 `COUNT(*) FROM issue` 仅 3 处（路由层唯一收口点 + 迁移前置探测 +
   runtime 域 public 原语），路由层那处必须在 `_count_issues` 内。
"""
import inspect
import os
import re
import socket
from pathlib import Path

import pytest

from src.domains.runtime.routes import issues as rt
from src.kernel import db_registry

_RUNTIME_DSN = "mysql://rt_user:S3cretPwd@10.0.0.9:3306/grape_prod_runtime?charset=utf8mb4"
_PROCESS_DB = "grape_prod_mgmt"


class _FallbackBomb:
    """进程主库哨兵：任何触达都证明发生了「静默回落 mgmt 库」（本 Issue 的根因）。"""

    def cursor(self, *a, **kw):  # pragma: no cover - 触达即失败
        raise AssertionError("COUNT/游标回落到了进程主库（Issue #3826 回归）")

    def commit(self, *a, **kw):  # pragma: no cover
        raise AssertionError("commit 回落到了进程主库（Issue #3826 回归）")

    def rollback(self, *a, **kw):  # pragma: no cover
        raise AssertionError("rollback 回落到了进程主库（Issue #3826 回归）")


class _FailingMain:
    """进程主库（local 路由）：游标可用但执行失败，用于验证日志字段的 local 形态。"""

    def __init__(self, error):
        self.error = error

    def cursor(self, *a, **kw):
        err = self.error

        class _Cur:
            def execute(self, *a, **kw):
                raise err

            def fetchone(self):  # pragma: no cover - execute 已抛
                return (0,)

            def close(self):
                pass

        return _Cur()

    def commit(self):  # pragma: no cover
        pass

    def rollback(self):  # pragma: no cover
        pass


def _three_db_topology(monkeypatch, runtime_dsn: "str | None" = _RUNTIME_DSN):
    """真实三库拓扑（不改 db_registry 函数）：进程域=mgmt，runtime 域 DSN 可配/可不配。"""
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
    monkeypatch.setenv("DEVOPS_DB_NAME", _PROCESS_DB)
    if runtime_dsn:
        monkeypatch.setenv("GRAPE_DSN_RUNTIME", runtime_dsn)
    else:
        monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)
    # 其余域的 DSN 与本事无关，清掉避免宿主机 env 干扰拓扑判定
    monkeypatch.delenv("GRAPE_DSN_MGMT", raising=False)
    monkeypatch.delenv("GRAPE_DSN_INSIGHT", raising=False)


_MINE_DEFAULTS = dict(status="", repo_key="", sort_order="desc", mode="default",
                      created_by_me="", assignees="", creators="", statuses="", q="")


def _stub_queries(monkeypatch, items):
    import src.domains.runtime.issues_center as ic
    monkeypatch.setattr(ic, "_query_tasks", lambda *a, **k: [dict(it) for it in items])
    monkeypatch.setattr(ic, "_get_gitcode_user_by_user_id", lambda *a, **k: "alice")


def _call_mine(**overrides):
    kwargs = dict(_MINE_DEFAULTS)
    kwargs.update(overrides)
    return rt.my_issues(**kwargs)


# ── ① 三库拓扑缺 GRAPE_DSN_RUNTIME：显式失败，不回落 mgmt 主库 ────────────────
def test_missing_runtime_dsn_cursor_raises_instead_of_falling_back(monkeypatch):
    _three_db_topology(monkeypatch, runtime_dsn=None)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _FallbackBomb(), raising=False)

    with pytest.raises(db_registry.DomainUnavailable) as ei:
        rt._rt_db.cursor()

    assert ei.value.domain == "runtime"
    assert "GRAPE_DSN_RUNTIME" in str(ei.value), (
        "异常必须点名缺失的变量，否则线上仍无法与「旧进程」区分")


def test_missing_runtime_dsn_commit_rollback_do_not_touch_process_db(monkeypatch):
    """无连接可提交/回滚：no-op（不抛、不触达主库），避免在 except 里掩盖原始异常。"""
    _three_db_topology(monkeypatch, runtime_dsn=None)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _FallbackBomb(), raising=False)

    rt._rt_db.commit()
    rt._rt_db.rollback()


def test_count_issues_degrades_to_none_without_fallback(monkeypatch, caplog):
    _three_db_topology(monkeypatch, runtime_dsn=None)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _FallbackBomb(), raising=False)

    with caplog.at_level("WARNING"):
        assert rt._count_issues("1=1", [], label="issues mine") is None

    assert "issues mine: count failed" in caplog.text
    assert "route=unavailable" in caplog.text


def test_mine_paged_count_unavailable_returns_null_total_and_page_inferred_has_more(monkeypatch):
    """分页降级口径：total=None（不回误导性 0），has_more 按「本页是否满页」推断。"""
    _three_db_topology(monkeypatch, runtime_dsn=None)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _FallbackBomb(), raising=False)
    _stub_queries(monkeypatch, [{"id": i, "title": f"issue-{i}"} for i in range(20)])

    out = _call_mine(per_page=20, page=1, user={"id": "u3826"})

    assert out["total"] is None
    assert out["has_more"] is True, "满页应推断还有下一页"
    assert len(out["issues"]) == 20


def test_mine_paged_count_unavailable_partial_page_has_more_false(monkeypatch):
    _three_db_topology(monkeypatch, runtime_dsn=None)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _FallbackBomb(), raising=False)
    _stub_queries(monkeypatch, [{"id": i, "title": f"issue-{i}"} for i in range(4)])

    out = _call_mine(per_page=20, page=3, user={"id": "u3826"})

    assert out["total"] is None
    assert out["has_more"] is False, "未满页应推断已到末页"


# ── ② 失败日志必须自带连接事实（route / target_db / pid / host，无凭据） ──────
def test_count_failure_log_carries_route_target_pid_host(monkeypatch, caplog):
    _three_db_topology(monkeypatch)

    class _BoomConn:
        def cursor(self, *a, **kw):
            raise RuntimeError("runtime db down")

        def close(self):
            pass

    monkeypatch.setattr(db_registry, "new_connection",
                        lambda d, autocommit=True: _BoomConn())

    with caplog.at_level("WARNING"):
        assert rt._count_issues("1=1", [], label="issues mine") is None

    text = caplog.text
    assert "issues mine: count failed" in text
    assert "route=domain" in text, f"域路由未标注: {text}"
    assert f"target_db=grape_prod_runtime" in text, f"目标库名缺失: {text}"
    assert f"pid={os.getpid()}" in text, f"pid 缺失: {text}"
    assert f"host={socket.gethostname()}" in text, f"host 缺失: {text}"
    # 脱敏硬约束：日志会被集中采集/转贴，不得出现账号口令或完整 DSN
    assert "S3cretPwd" not in text, "口令泄漏进日志"
    assert "rt_user" not in text, "账号泄漏进日志"
    assert "mysql://" not in text, "完整 DSN 泄漏进日志"


def test_count_failure_log_reports_local_route(monkeypatch, caplog):
    """单库/本域拓扑：route=local，目标是进程主库名（DEVOPS_DB_NAME）。"""
    monkeypatch.delenv("GRAPE_DOMAIN_LIB", raising=False)
    for d in ("MGMT", "RUNTIME", "INSIGHT"):
        monkeypatch.delenv(f"GRAPE_DSN_{d}", raising=False)
    monkeypatch.setenv("DEVOPS_DB_NAME", _PROCESS_DB)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _FailingMain(RuntimeError("process db down")), raising=False)

    with caplog.at_level("WARNING"):
        assert rt._count_issues("1=1", [], label="issues") is None

    text = caplog.text
    assert "issues: count failed" in text
    assert "route=local" in text, f"local 路由未标注: {text}"
    assert f"target_db={_PROCESS_DB}" in text, f"进程库名缺失: {text}"
    assert f"pid={os.getpid()}" in text


# ── ③ 拓扑棘轮：代理只认内核 route_of ──────────────────────────────────────
def test_domain_proxy_uses_kernel_route_of_only():
    src = inspect.getsource(rt._DomainDbProxy)
    assert 'route_of("runtime")' in src, "代理未走内核唯一路由判定"
    assert "uses_own_dsn" not in src, (
        "_DomainDbProxy 仍有自己的回落判定（uses_own_dsn ? DSN : 主库），"
        "与 db_registry.route_of 契约并存 → Issue #3826 根因")


# ── ④ 全仓 COUNT 审计：无第三处裸 SQL ──────────────────────────────────────
_SRC = Path(rt.__file__).resolve().parents[3]  # …/src/domains/runtime/routes/issues.py → …/src
_COUNT_RE = re.compile(r"COUNT\(\*\)\s+FROM\s+issue\b", re.IGNORECASE)
_ALLOWED_COUNT_SITES = {
    "domains/runtime/routes/issues.py": 1,            # _count_issues（唯一收口点）
    "domains/runtime/migrations_ddl.py": 1,           # tasks→issue 重命名前置探测（迁移期本域执行）
    "domains/runtime/public/__init__.py": 1,          # issue_vcs_checked_count（域 public 原语）
}


def test_no_third_bare_issue_count_in_src():
    found = {}
    for path in _SRC.rglob("*.py"):
        rel = str(path.relative_to(_SRC))
        n = len(_COUNT_RE.findall(path.read_text(encoding="utf-8")))
        if n:
            found[rel] = n

    assert found == _ALLOWED_COUNT_SITES, (
        "issue 表 COUNT 站点发生漂移：路由层必须收口到 _count_issues（走 _rt_db/runtime 域），"
        f"实际={found}，允许={_ALLOWED_COUNT_SITES}")


def test_route_level_issue_count_lives_in_single_helper():
    helper = inspect.getsource(rt._count_issues)
    assert "SELECT COUNT(*) FROM issue WHERE {where}" in helper
    assert "_rt_db.cursor()" in helper, "COUNT 收口必须走 runtime 域代理"