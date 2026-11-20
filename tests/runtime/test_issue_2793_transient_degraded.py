"""Issue #2793：尾部瞬时故障降级 completed 回归 + `git push` 写证据口径。

基线 arch11-weekly-1102-zrr 的 transient_retry.py（源自 7abf1f6b0）已有降级分支，
本用例固定其条件为「瞬时 ∧ 写证据 ∧ 有实质输出」，并确保以下零回归：
① 纯崩溃（有写证据但无实质输出）维持 fail 路径；
② 无写证据维持自动重试路径；
③ auth_config 不重试不降级。
同时覆盖 has_write_evidence 新增 `git push`（含 `grape git push` / repogit 包装；
`--dry-run` 不算）口径。
"""
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import transient_retry as tr

RK = "gitcode:issue2793/degraded"
TIDS = ["t_i2793_deg_push", "t_i2793_deg_crash", "t_i2793_deg_noevd",
        "t_i2793_deg_auth", "t_i2793_deg_post"]


class _FakeTimer:
    fired = []

    def __init__(self, delay, fn, args=None, kwargs=None):
        self.delay, self.fn, self.args = delay, fn, args or ()
        self.daemon = False

    def start(self):
        _FakeTimer.fired.append((self.delay, self.args))

    def cancel(self):
        pass


@pytest.fixture(autouse=True)
def _fixture(monkeypatch):
    monkeypatch.setattr(tr.threading, "Timer", _FakeTimer)
    _FakeTimer.fired.clear()
    _clean()
    yield
    _clean()


def _clean():
    with db_lock:
        c = db.cursor()
        for tid in TIDS:
            try:
                c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
                c.execute("DELETE FROM task_messages WHERE task_id=?", (tid,))
            except Exception:
                pass
        db.commit()


def _seed(tid):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "status, prompt, cwd, skill_files, env_extra, attempts, started_at) "
            "VALUES (?,?,0,?, 'qa', 'running', 'x', '', '{}', '{}', 0, "
            "'2020-01-01T00:00:00+00:00')",
            (tid, "sess-" + tid, RK))
        db.commit()


def _row(tid):
    with db_lock:
        c = db.cursor()
        return c.execute("SELECT status, exit_code, errors FROM agent_tasks WHERE id=?",
                         (tid,)).fetchone()


def _codes(tid):
    try:
        return [e.get("error_code") for e in json.loads(_row(tid)[2] or "[]")
                if isinstance(e, dict)]
    except Exception:
        return []


def _tu(cmd):
    return [{"tool": "bash", "input": {"command": cmd}}]


# ── has_write_evidence：git push 口径 ──────────────────────────

class TestGitPushEvidence:
    @pytest.mark.parametrize("cmd", [
        "git push origin feat/issue-2793",
        "grape git push origin feat/issue-2793",
        "cd repo && git push -u origin HEAD",
        "python -c 'from x import repogit; repogit.push()'",
    ])
    def test_push_is_write_evidence(self, cmd):
        assert tr.has_write_evidence(_tu(cmd), "") is True

    @pytest.mark.parametrize("cmd", [
        "git push --dry-run origin main",
        "git status",
        "git log --oneline -5",
        "echo pushed nothing",
        "echo git push origin main",
        "printf 'git push origin main'",
        "echo 'git push origin main'",
    ])
    def test_non_write_commands_not_evidence(self, cmd):
        assert tr.has_write_evidence(_tu(cmd), "") is False

    @pytest.mark.parametrize("cmd", [
        "git push --dry-run origin a && git push origin b",
        "git push --dry-run origin a; git push origin b",
        "git push --dry-run origin a || git push origin b",
        "git push --dry-run origin a && echo done && git push -u origin HEAD",
    ])
    def test_dry_run_excluded_per_subcommand(self, cmd):
        """--dry-run 只豁免所在子命令，不得吞掉同一条命令里的真实 push。"""
        assert tr.has_write_evidence(_tu(cmd), "") is True

    @pytest.mark.parametrize("cmd", [
        "git push --dry-run origin a && git status",
        "git push --dry-run origin a; echo done",
        "git push --dry-run origin a",
    ])
    def test_all_push_subcommands_dry_run_not_evidence(self, cmd):
        assert tr.has_write_evidence(_tu(cmd), "") is False

    def test_existing_post_comment_still_evidence(self):
        cmd = "curl -X POST https://gitcode.com/api/v5/repos/o/r/issues/1/comments"
        assert tr.has_write_evidence(_tu(cmd), "") is True

    def test_structured_output_still_evidence(self):
        text = json.dumps({"actions": ["done"], "conclusion": "ok"})
        assert tr.has_write_evidence([], text) is True


# ── 降级路径与零回归 ───────────────────────────────────────────

class TestTailTransientDegrade:
    def test_push_evidence_and_output_degrades_completed(self):
        tid = "t_i2793_deg_push"
        _seed(tid)
        r = tr.retry_transient_task(
            tid, exit_code=1,
            error_text="stream aborted while finalizing push upstream",
            has_output=True,
            tool_uses=_tu("grape git push origin feat/issue-2793"),
            result_text="代码已推送，PR 已创建")
        assert r is True
        status, exit_code, _ = _row(tid)
        assert status == "completed"
        assert exit_code == 0
        assert "TRANSIENT_DEGRADED_COMPLETED" in _codes(tid)

    def test_post_comment_evidence_and_output_still_degrades(self):
        tid = "t_i2793_deg_post"
        _seed(tid)
        r = tr.retry_transient_task(
            tid, exit_code=1, error_text="connection reset while posting comment",
            has_output=True,
            tool_uses=_tu("curl -X POST https://gitcode.com/api/v5/repos/o/r/issues/1/comments"),
            result_text="审查结论已产出")
        assert r is True
        assert _row(tid)[0] == "completed"
        assert "TRANSIENT_DEGRADED_COMPLETED" in _codes(tid)

    def test_pure_crash_keeps_fail_path(self):
        tid = "t_i2793_deg_crash"
        _seed(tid)
        r = tr.retry_transient_task(
            tid, exit_code=1, error_text="connection reset by peer",
            has_output=False,
            tool_uses=_tu("git push origin feat/x"), result_text="")
        assert r is False
        assert _row(tid)[0] == "running"
        assert "TRANSIENT_DEGRADED_COMPLETED" not in _codes(tid)

    def test_no_write_evidence_keeps_retry_path(self):
        tid = "t_i2793_deg_noevd"
        _seed(tid)
        r = tr.retry_transient_task(
            tid, exit_code=1, error_text="connection reset by peer",
            has_output=True, tool_uses=[], result_text="部分输出")
        assert r is True
        assert _FakeTimer.fired, "无写证据应安排自动重试"
        assert _row(tid)[0] == "running"
        assert "TRANSIENT_DEGRADED_COMPLETED" not in _codes(tid)

    def test_auth_config_not_degraded(self):
        tid = "t_i2793_deg_auth"
        _seed(tid)
        r = tr.retry_transient_task(
            tid, exit_code=1, error_text="auth_config missing API key",
            has_output=True, tool_uses=_tu("git push origin x"), result_text="部分输出")
        assert r is False
        assert _row(tid)[0] == "running"
        assert "TRANSIENT_DEGRADED_COMPLETED" not in _codes(tid)