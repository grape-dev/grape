"""chat 远端任务失败提示优化回归测试（chat-remote-failure-hint-design §六）。

背景（2026-10-29 实锤事故）：远端 worker 引擎崩溃（opencode CLI 1.17.7 ↔ Desktop 1.18.18
共享 opencode.db schema 冲突 → exit=1）时，chat 只回光秃秃「查询失败」，真实原因丢失。

根因链路：
- worker 上报失败原因只写 agent_tasks.errors（service.fail_task），result_text 恒空；
- chat 远端轮询只读 result_text → 失败路径永远是「查询失败: 」（空尾）。

本文件守住：
1. fail_task 双写 result_text（失败原因可见）；
2. friendly_failure_hint 按类输出 (hint, action)，覆盖引擎/CLI 不兼容、超时、token、兜底；
3. _format_chat_failure_text 输出恒为「查询失败：<hint>；<action>」，绝不出现空尾。
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.session_manager import db, db_lock
from src.runtime import service


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    yield
    _wipe()


def _wipe():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test/%'")
        db.commit()


def _insert_event(seq, status="running"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status) VALUES (?, ?, ?, ?)",
            (seq, "gitcode:test/repo", "issue_opened", status),
        )
        db.commit()


def _insert_task(task_id, event_seq, status="running", created_offset_s=0, repo_key="gitcode:test/repo"):
    ts = (datetime.now(timezone.utc) + timedelta(seconds=created_offset_s)).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "sess-1", 7, event_seq, "#3", repo_key, "chat", "p", status, ts),
        )
        db.commit()


def _task_fields(tid):
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT result_text, errors, exit_code FROM agent_tasks WHERE id=?", (tid,)
        ).fetchone()
    return {"result_text": row[0] or "", "errors": row[1] or "", "exit_code": row[2]} if row else {}


class TestFailTaskDualWrite:
    """QA-01 / QA-02：fail_task 双写 result_text，幂等守卫不回归。"""

    def test_engine_failure_writes_result_text(self):
        _insert_event(681, status="running")
        _insert_task("t_w", 681, status="running", created_offset_s=-5)

        service.fail_task(
            "t_w",
            "engine exit_code=1, errors: SQLiteError: no such column: replacement_seq",
            exit_code=1,
        )

        f = _task_fields("t_w")
        assert f["result_text"], "失败路径 result_text 不得再恒空"
        assert "replacement_seq" in f["result_text"] or "exit_code" in f["result_text"]
        assert "engine exit_code=1" in f["result_text"]
        assert "replacement_seq" in f["errors"], "errors 保留原始排障详情"

    def test_result_text_truncated_not_huge(self):
        _insert_event(682, status="running")
        _insert_task("t_tr", 682, status="running", created_offset_s=-5)

        service.fail_task("t_tr", "x" * 5000, exit_code=1)

        f = _task_fields("t_tr")
        assert len(f["result_text"]) <= 500

    def test_idempotent_guards_unchanged(self):
        """已 failed 的重复上报仍被幂等跳过（不回归 2026-10-19 补报重放修复）。"""
        _insert_event(683, status="running")
        _insert_task("t_id", 683, status="running", created_offset_s=-5)

        service.fail_task("t_id", "first", exit_code=-1)
        first = _task_fields("t_id")
        service.fail_task("t_id", "second replay", exit_code=-1)
        after = _task_fields("t_id")

        assert first["result_text"] == after["result_text"], "幂等跳过不得改写已落库失败原因"

    def test_transient_retry_exhausted_still_has_result_text(self):
        """Issue #403 耗尽判死路径（含 _result_show 摘要）也写 result_text。"""
        _insert_event(684, status="running")
        _insert_task("t_tr2", 684, status="running", created_offset_s=-5)

        service.fail_task("t_tr2", "upstream 5xx", exit_code=-1)

        f = _task_fields("t_tr2")
        assert f["result_text"], "耗尽判死路径也必须有 result_text"
        assert "upstream" in f["result_text"]


class TestFriendlyFailureHint:
    """QA-03~06：分类函数输出 (hint, action)，绝不空尾。"""

    def test_engine_cli_incompatible(self):
        hint, action = service.friendly_failure_hint({
            "errors": '["engine exit_code=1, errors: SQLiteError: no such column: replacement_seq"]',
            "result_text": "",
            "stderr": "",
            "exit_code": 1,
        })
        assert "引擎执行异常" in hint
        assert action and "检查" in action

    def test_timeout(self):
        hint, action = service.friendly_failure_hint({
            "errors": '["task timed out after 3600s"]',
            "result_text": "",
            "stderr": "",
            "exit_code": -1,
        })
        assert "超时" in hint
        assert action

    def test_token_expired(self):
        hint, action = service.friendly_failure_hint({
            "errors": '[{"error_code": "TOKEN_EXPIRED", "message": "user token expired or revoked"}]',
            "result_text": "TOKEN_EXPIRED",
            "stderr": "",
            "exit_code": -1,
        })
        assert "授权" in hint or "token" in hint.lower()
        assert "重新授权" in action

    def test_no_token(self):
        hint, action = service.friendly_failure_hint({
            "errors": '[{"error_code": "NO_TOKEN_AVAILABLE"}]',
            "result_text": "",
            "stderr": "",
            "exit_code": -1,
        })
        assert "凭据" in hint
        assert action

    def test_unknown_error_fallback(self):
        hint, action = service.friendly_failure_hint({
            "errors": '["weird custom failure abc"]',
            "result_text": "",
            "stderr": "",
            "exit_code": 2,
        })
        assert "abc" in hint, "兜底应保留原始片段供排障"
        assert "worker" in action or "任务详情" in action

    def test_empty_task_fallback(self):
        hint, action = service.friendly_failure_hint(None)
        assert hint and action


class TestFormatChatFailureText:
    """QA-07~08：chat 展示点输出友好文案，绝不空尾。"""

    def test_failure_non_empty_suffix(self):
        from src.api.routes.chat import _format_chat_failure_text
        _insert_event(685, status="running")
        _insert_task("t_cv", 685, status="running", created_offset_s=-5)
        service.fail_task(
            "t_cv",
            "engine exit_code=1, errors: SQLiteError: no such column: replacement_seq",
            exit_code=1,
        )

        text = _format_chat_failure_text("t_cv")

        assert text.startswith("查询失败：")
        assert len(text) > len("查询失败：")
        assert not text.endswith(("查询失败：", "查询失败: ", "：", " "))
        assert "；" in text and text.split("；")[-1].strip(), "必须带行动建议"

    def test_unknown_task_id_has_stable_message(self):
        from src.api.routes.chat import _format_chat_failure_text
        text = _format_chat_failure_text("t_does_not_exist")
        assert text.startswith("查询失败：")
        assert text.rstrip("：") != "查询失败"
        assert len(text) > len("查询失败：")

    def test_no_regression_completed_path(self):
        """QA-08：正常完成路径 result_text 保持结构化结论，不受失败分类影响。"""
        _insert_event(686, status="running")
        _insert_task("t_ok", 686, status="running", created_offset_s=-5)

        service.update_task_status(
            "t_ok", "completed", exit_code=0,
            result_text="结论：任务完成，PR #5 已合入")

        f = _task_fields("t_ok")
        assert f["result_text"] == "结论：任务完成，PR #5 已合入"
        assert f["exit_code"] == 0
        # 展示层 completed 分支直接透传 result_text，不拼接「查询失败」前缀
        from src.api.routes.chat import _format_chat_failure_text
        tk = service.get_task("t_ok")
        assert not (tk or {}).get("result_text", "").startswith("查询失败")