"""Issue #4295：锁内禁止外部 IO 的**防回归门禁**（静态 + 结构断言）。

生产实证：``db_lock`` 是进程级单把 RLock，前台请求只要在临界区里做一次远程调用，
就能把同进程所有 DB 访问串行化到 99.9s。修复把"通知投递"移出临界区，本用例把
这个结论固化成机械可验的断言，避免下一个提交又把外部 IO 塞回锁里：

1. ``scripts/check_db_lock_external_io.py`` 在整棵 ``src/`` 上零命中（挂到 ``make lint``）；
2. ``issues_center.close_issue_tasks`` / ``_close_issue_tasks_bot_takeover`` 的通知调用
   在 ``with db_lock:`` 块**之外**（AST 结构断言，不靠人工记忆）；
3. 门禁本身非空转：对合成的违规代码必须报错（否则"零命中"毫无意义）。
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ISSUES_CENTER = REPO / "src" / "domains" / "runtime" / "issues_center.py"
GATE_SCRIPT = REPO / "scripts" / "check_db_lock_external_io.py"

LOCK_BINDINGS = {"db_lock", "_db_lock"}


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("check_db_lock_external_io", GATE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_gate_script_reports_no_violation_on_src():
    gate = _load_gate_module()
    violations = gate.scan_tree()
    detail = "\n".join(
        f"{v['file']}:{v['lineno']} {v['function']} -> {v['call']}" for v in violations)
    assert violations == [], (
        "db_lock 临界区内出现外部 IO（Issue #4295 门禁）：把远程/通知调用移到 "
        f"with db_lock 之外（锁内取快照 → 锁外 IO → 短写回）\n{detail}")
    assert gate.main([]) == 0


def test_gate_detects_synthetic_violation():
    """门禁非空转：合成一段"锁内调远程"的代码，必须被扫出来。"""
    gate = _load_gate_module()
    source = (
        "def bad(repo_key):\n"
        "    with db_lock:\n"
        "        c = db.cursor()\n"
        "        client.fetch_issue(repo_key)\n"
        "        maybe_notify_review_point_status(repo_key, 1, 'pending_review')\n"
    )
    tree = ast.parse(source)
    scanner = gate._Scanner("synthetic.py", gate._file_lock_aliases(tree, "synthetic.py"))
    scanner.visit(tree)
    calls = {v["call"] for v in scanner.violations}
    assert "client.fetch_issue" in calls
    assert "maybe_notify_review_point_status" in calls


def test_gate_ignores_local_only_and_plain_db_calls():
    gate = _load_gate_module()
    source = (
        "def ok(repo_key):\n"
        "    with db_lock:\n"
        "        c = db.cursor()\n"
        "        c.execute('SELECT 1')\n"
        "        row = c.fetchone()\n"
        "        _get_gitcode_user_by_user_id(row[0])\n"
        "        return row\n"
    )
    tree = ast.parse(source)
    scanner = gate._Scanner("synthetic.py", gate._file_lock_aliases(tree, "synthetic.py"))
    scanner.visit(tree)
    assert scanner.violations == [], "纯 DB/白名单调用不得被误报（否则门禁会被放宽到失效）"


def test_gate_detects_violation_in_acquire_release_block():
    """``acquired = db_lock.acquire(...)`` + try/finally release 形态也要被扫出来。"""
    gate = _load_gate_module()
    source = (
        "def bad(repo_key):\n"
        "    acquired = db_lock.acquire(timeout=30)\n"
        "    try:\n"
        "        client.fetch_issue(repo_key)\n"
        "        maybe_notify_review_point_status(repo_key, 1, 'pending_review')\n"
        "        c = db.cursor()\n"
        "        c.execute('SELECT 1')\n"
        "    finally:\n"
        "        if acquired:\n"
        "            db_lock.release()\n"
    )
    calls = {v["call"] for v in gate.scan_source("src/synthetic.py", source)}
    assert "client.fetch_issue" in calls, "锁内远程调用必须被抓到（acquire/release 形态）"
    assert "maybe_notify_review_point_status" in calls
    assert "c.execute" not in calls, "DB 本地方法不得误报"


def test_gate_acquire_release_ignores_pure_db_block():
    """acquire/release 包裹的纯 DB 块不误报（pipeline_config._get_scene_columns 的真实形态）。"""
    gate = _load_gate_module()
    source = (
        "def ok():\n"
        "    db, lock = _db()\n"
        "    acquired = lock.acquire(timeout=30)\n"
        "    try:\n"
        "        if acquired:\n"
        "            c = db.cursor()\n"
        "            c.execute('SHOW COLUMNS FROM devpipe')\n"
        "            cols = {r[0] for r in c.fetchall()}\n"
        "    finally:\n"
        "        if acquired:\n"
        "            lock.release()\n"
    )
    # 该文件登记了 `lock` 别名（db, lock = _db()）；纯 DB 调用应为 0 命中
    rel = "src/domains/mgmt/pipeline_config.py"
    assert "lock" in gate._file_lock_aliases(ast.parse(source), rel)
    assert gate.scan_source(rel, source) == []


def test_gate_acquire_release_requires_the_registered_lock():
    """用户级 threading.Lock 的 acquire/release 不算 db_lock 临界区。"""
    gate = _load_gate_module()
    source = (
        "def refresh(user_id):\n"
        "    lock = _get_user_refresh_lock(user_id)\n"
        "    acquired = lock.acquire(timeout=5)\n"
        "    try:\n"
        "        return user_db.refresh_gitcode_oauth_token(user_id)\n"
        "    finally:\n"
        "        lock.release()\n"
    )
    assert gate.scan_source("src/api/deps.py", source) == []


def test_gate_does_not_treat_user_level_threading_lock_as_db_lock():
    """``with lock:`` 若是用户级 threading.Lock，不得算 db_lock 临界区。"""
    gate = _load_gate_module()
    source = (
        "def refresh(user_id):\n"
        "    lock = _get_user_refresh_lock(user_id)\n"
        "    with lock:\n"
        "        return user_db.refresh_gitcode_oauth_token(user_id)\n"
    )
    tree = ast.parse(source)
    scanner = gate._Scanner("synthetic.py", gate._file_lock_aliases(tree, "synthetic.py"))
    scanner.visit(tree)
    assert scanner.violations == []


def _lock_spans(fn: ast.FunctionDef) -> list[tuple[int, int]]:
    spans = []
    for node in ast.walk(fn):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                expr = item.context_expr
                name = expr.id if isinstance(expr, ast.Name) else getattr(expr, "attr", "")
                if name in LOCK_BINDINGS:
                    spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _calls_of(fn: ast.FunctionDef, suffix: str) -> list[int]:
    lines = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name == suffix:
                lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("func_name", ["close_issue_tasks", "_close_issue_tasks_bot_takeover"])
def test_review_point_notify_is_dispatched_outside_db_lock(func_name):
    """审核点通知（含目标解析/conv 兜底建/本人 bot 凭证）必须在 db_lock 之外投递。"""
    tree = ast.parse(ISSUES_CENTER.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func_name)
    spans = _lock_spans(fn)
    assert spans, f"{func_name} 里应仍有 db_lock 临界区（状态判定/UPDATE 必须原子）"
    notify_lines = _calls_of(fn, "maybe_notify_review_point_status")
    assert notify_lines, f"{func_name} 必须仍然投递审核点通知（不能为了性能丢掉通知）"
    for line in notify_lines:
        inside = [f"{a}-{b}" for a, b in spans if a <= line <= b]
        assert not inside, (
            f"{func_name} 第 {line} 行的通知投递仍在 with db_lock（{inside}）内："
            "目标解析含多次 DB 往返与凭证解析，会把前台请求的临界区滚到 100s 级")


def test_issues_center_still_keeps_state_updates_inside_lock():
    """反向断言：状态 UPDATE/commit 不能被一并搬到锁外（否则丢原子性）。"""
    tree = ast.parse(ISSUES_CENTER.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "close_issue_tasks")
    body_src = ast.get_source_segment(ISSUES_CENTER.read_text(encoding="utf-8"), fn) or ""
    spans = _lock_spans(fn)
    update_line = next(
        (n.lineno for n in ast.walk(fn)
         if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "execute"
         and "UPDATE issue" in ast.dump(n)),
        None)
    assert update_line, "close_issue_tasks 必须保留 UPDATE issue 语句"
    assert any(a <= update_line <= b for a, b in spans), "状态 UPDATE 必须仍在锁内"
    assert "_rt_db.commit()" in body_src