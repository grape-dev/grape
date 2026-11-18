"""孤儿轮次兜底：user 行有、agent 行永远没有时不能恒返 running。

2026-11-18 实障：执行进程中途死掉（ACP 秒死 / 服务重启），这一轮不会补 agent 行，
`GET /api/chat/task/{task_id}/status` 的 DB 兜底恒落 `has_user and not has_assistant`
→ 永远 running → 页面永久显示"处理中"（conv 2accb6fb 的 id=5/6/7 就是这样卡住的）。

修法：超过 ORPHAN_TURN_TIMEOUT_SECS 仍无 agent 行 → 返 error「任务已中断」。
顺带修 conversation_id 取错列（原实现取 rows[0][3]=content）。
"""
import datetime as _dt

import pytest

from src.domains.runtime.routes import chat as chat_routes


def _ts(minutes_ago: float) -> str:
    """sqlite 的 created_at 是 UTC 墙钟（datetime('now')），按同样口径造时间。"""
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _patch_rows(monkeypatch, rows):
    monkeypatch.setattr(chat_routes._rt, "read_all_any",
                        lambda name, params: rows if name == "messages_by_task" else [])
    # 内存态必须为空，否则走不到 DB 兜底
    monkeypatch.setattr("src.api.chat_task_manager.ChatTaskManager.get_status",
                        staticmethod(lambda task_id: None), raising=False)
    chat_routes._chat_tasks.clear()


def test_fresh_orphan_turn_still_reports_running(monkeypatch):
    rows = [(11, "user", 1, "帮我看看这个仓库", "conv-abc", _ts(2))]
    _patch_rows(monkeypatch, rows)
    res = chat_routes.chat_task_status("chat_orphan_fresh", user={"id": 1})
    assert res["status"] == "running"
    # 列序回归：conversation_id 必须是第 5 列（旧实现错取 content）
    assert res["conversation_id"] == "conv-abc"


def test_stale_orphan_turn_reports_interrupted(monkeypatch):
    rows = [(12, "user", 1, "帮我看看这个仓库", "conv-abc", _ts(45))]
    _patch_rows(monkeypatch, rows)
    res = chat_routes.chat_task_status("chat_orphan_stale", user={"id": 1})
    assert res["status"] == "error"
    assert "任务已中断" in res["error"]
    assert res["conversation_id"] == "conv-abc"
    assert res["source"] == "db"


def test_agent_reply_wins_over_age(monkeypatch):
    old = _ts(120)
    rows = [(13, "user", 1, "hi", "conv-x", old),
            (14, "agent", 2, "done", "conv-x", old)]
    _patch_rows(monkeypatch, rows)
    res = chat_routes.chat_task_status("chat_with_reply", user={"id": 1})
    assert res["status"] == "done"
    assert res["text"] == "done"


def test_unparsable_timestamp_keeps_running(monkeypatch):
    """时间取不到就不能判中断（宁可持续 running，也不错杀正在跑的任务）。"""
    rows = [(15, "user", 1, "hi", "conv-y", "")]
    _patch_rows(monkeypatch, rows)
    res = chat_routes.chat_task_status("chat_no_ts", user={"id": 1})
    assert res["status"] == "running"
