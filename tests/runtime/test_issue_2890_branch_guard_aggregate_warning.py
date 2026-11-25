"""Issue #2890：pre-push 分支守卫缺表时聚合为唯一一条 fail-open 告警。

背景：缺 ``branch_guard_state`` 表时，守卫各环节独立 ``log.warning`` 会在单次 push
刷出 5 条 WARNING JSON（state read ×3 / PR query / state write），既无统一「已放行」
结论行，又易被上层 Agent 误判为推送失败而重试。
"""
import io
import json
import logging
import sqlite3
import subprocess

import src.kernel.session_manager as sm
from src.domains.runtime.engine import branch_guard as bg

REPO_KEY = "gitcode:owner/repo"
BRANCH = "feat/issue-2890"


def _ref_line(branch=BRANCH, local="local-sha", remote="old-sha"):
    return f"refs/heads/{branch} {local} refs/heads/{branch} {remote}\n"


class _MissingTableCursor:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, params=()):
        if "branch_guard_state" in sql:
            raise sqlite3.OperationalError("no such table: branch_guard_state")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _MissingTableDB:
    """模拟会话库缺 branch_guard_state 表（distributed_locks 等其它表正常）。"""

    def __init__(self, real):
        self._real = real

    def cursor(self):
        return _MissingTableCursor(self._real.cursor())

    def execute(self, sql, params=()):
        if "branch_guard_state" in sql:
            raise sqlite3.OperationalError("no such table: branch_guard_state")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _guard_warning_payloads(stderr: str) -> list:
    """收集 stderr 上所有 WARNING 级 JSON 行（聚合行与任何走 logger 的告警）。

    不能只按 ``component == "branch_guard"`` 过滤：仓库 logger 实际名为
    ``devops-bot``（src/kernel/config.py），单点 ``log.warning`` 若回归也不会是
    ``branch_guard`` 组件，按组件过滤会把回归漏掉。
    """
    payloads = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and str(payload.get("level", "")).upper() == "WARNING":
            payloads.append(payload)
    return payloads


def _warning_log_messages(caplog) -> list:
    """hook 执行期间 WARNING 及以上级别的日志消息（含 logger 落到 stderr 之前的）。"""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


def test_missing_table_emits_single_readable_fail_open_hint(monkeypatch, capsys, caplog):
    """缺表场景：stderr 只有 1 条可读中文提示，退出码 0（Issue #2923 收口）。"""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(sm, "db", _MissingTableDB(sm.db))
    # PR 查询（grape CLI）同时失败，凑齐 state read ×3 + PR query + state write 五类异常。
    monkeypatch.setattr(
        bg.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=a[0] if a else [], returncode=1, stdout="", stderr="network down"))
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))

    assert bg.hook_main([REPO_KEY]) == 0

    err = capsys.readouterr().err
    hint_lines = [line for line in err.splitlines() if "[branch-guard]" in line]
    assert len(hint_lines) == 1, err
    assert "分支基线核验暂不可用" in hint_lines[0]
    assert "推送已放行" in hint_lines[0]
    # 用户侧不再出现原始异常文案（Issue #2923）。
    assert "no such table" not in err
    assert "network down" not in err
    # 明细保留在 debug 日志，供排障定位具体阶段。
    debug_messages = [record.getMessage() for record in caplog.records
                      if record.levelno == logging.DEBUG]
    assert any("state read failed" in message for message in debug_messages)
    assert any("PR query failed" in message for message in debug_messages)
    # 回归护栏：单点异常不得再经 log.warning 各自刷一条 WARNING（logger 名
    # devops-bot 的 JSON 不一定被 capsys 捕获），否则「stderr 仅 1 条告警」被打破。
    assert _warning_log_messages(caplog) == [], caplog.records


def test_no_guard_errors_emits_no_fail_open_alert(monkeypatch, capsys):
    """无守卫异常时行为不变：不输出聚合告警。"""
    monkeypatch.setattr(bg, "acquire_branch_lease", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
    monkeypatch.setattr(bg, "reconcile_pending", lambda *a, **k: "")
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "open"})
    monkeypatch.setattr(bg, "record_pending_push", lambda *a, **k: {})
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))

    assert bg.hook_main([REPO_KEY]) == 0
    err = capsys.readouterr().err
    assert _guard_warning_payloads(err) == []
    assert "fail_open" not in err


def test_aggregate_alert_write_failure_does_not_block_push(monkeypatch):
    """聚合输出自身异常不得阻断 push（fail-open 优先）。"""

    class _Boom:
        def write(self, *a, **k):
            raise RuntimeError("stderr closed")

        def flush(self):
            raise RuntimeError("stderr closed")

    monkeypatch.setattr(bg.sys, "stderr", _Boom())
    bg._emit_fail_open_alert(REPO_KEY, [{"stage": "state_read", "error": "x"}])


def test_real_block_still_fail_closed_without_fail_open_alert(monkeypatch, capsys):
    """真实违规（PR 已合并）仍返回 1，且不附加 fail_open 聚合告警。"""
    monkeypatch.setattr(bg, "acquire_branch_lease", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
    monkeypatch.setattr(bg, "reconcile_pending", lambda *a, **k: "")
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "merged", "number": 42,
                                         "base": "weekly-mys"})
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))

    assert bg.hook_main([REPO_KEY]) == 1
    err = capsys.readouterr().err
    assert "PR #42" in err
    assert _guard_warning_payloads(err) == []