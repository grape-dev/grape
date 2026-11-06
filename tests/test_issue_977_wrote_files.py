"""Issue #977：task-logs /info 增补 wrote_files 产物清单。

- 数据源：task_messages 的 write/patch tool_use 入参派生（path + content/new_string
  的 utf-8 字节数），不扫工作区。
- 验收：1) write(57KB)+patch 两条 tool_use → wrote_files 含 2 条且 bytes/seq 对得上；
  2) 任务无 write → wrote_files == [] 且其余字段与现状逐字段一致。
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.session_manager import db, db_lock  # noqa: E402


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _insert_task() -> str:
    task_id = f"t_{uuid.uuid4().hex[:8]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, status, created_at) "
            "VALUES (?, 'sess-977', 0, '', 'running', ?)",
            (task_id, _now_iso()))
        db.commit()
    return task_id


def _insert_tool_use(task_id: str, seq: int, tool: str, inp: dict, created_at: str = ""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR IGNORE INTO task_messages (task_id, seq, type, tool, content, input_json, output, created_at) "
            "VALUES (?, ?, 'tool_use', ?, '', ?, '', ?)",
            (task_id, seq, tool, json.dumps(inp, ensure_ascii=False), created_at or _now_iso()))
        db.commit()


def _cleanup(task_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        db.commit()


class TestWroteFiles:
    def test_write_and_patch_yield_two_entries(self):
        """write(57KB) + patch 两条 tool_use → wrote_files 恰 2 条，bytes 与原文一致、seq 对得上。"""
        from src.api.routes.task_logs import get_task_runtime_info
        task_id = _insert_task()
        big_content = "x" * 57 * 1024
        try:
            _insert_tool_use(task_id, 1, "write", {"path": "src/demo.py", "content": big_content}, "2026-11-04T08:00:00+00:00")
            _insert_tool_use(task_id, 2, "patch", {"path": "src/demo.py", "old_string": "x", "new_string": "y" * 100}, "2026-11-04T08:01:00+00:00")
            user = {"id": 1, "username": "tester", "org_id": 0}
            info = get_task_runtime_info(task_id, user=user)
            wf = info["wrote_files"]
            assert len(wf) == 2, f"应恰 2 条（同 path 多次写入各记一条），实际 {len(wf)}"
            assert wf[0]["path"] == "src/demo.py"
            assert wf[0]["bytes"] == len(big_content.encode("utf-8"))
            assert wf[0]["tool"] == "write"
            assert wf[0]["seq"] == 1
            assert wf[0]["first_seen_at"] == "2026-11-04T08:00:00+00:00"
            assert wf[1]["tool"] == "patch"
            assert wf[1]["bytes"] == 100, "patch 应取 new_string 的字节数"
            assert wf[1]["seq"] == 2
            assert wf[1]["first_seen_at"] == "2026-11-04T08:01:00+00:00"
        finally:
            _cleanup(task_id)

    def test_no_write_yields_empty_list_and_other_fields_intact(self):
        """任务无 write 类消息 → wrote_files == []，其余字段与既有口径逐字段一致。"""
        from src.api.routes.task_logs import get_task_runtime_info
        task_id = _insert_task()
        try:
            _insert_tool_use(task_id, 1, "read", {"path": "src/other.py"})
            _insert_tool_use(task_id, 2, "bash", {"command": "ls"})
            user = {"id": 1, "username": "tester", "org_id": 0}
            info = get_task_runtime_info(task_id, user=user)
            assert info["wrote_files"] == []
            expected_keys = {
                "agent_name", "agent_version", "device_name", "last_ip", "owner_name",
                "prompt", "prompt_full_length", "created_at", "started_at", "completed_at",
                "status", "issue_id", "event_seq", "repo_key", "spec",
                "cpu_percent", "memory_percent", "disk_percent", "load_avg",
            }
            assert expected_keys <= set(info.keys()), "原有字段不得缺失"
            assert info["agent_name"] == "" and info["device_name"] == ""
            assert info["prompt"] == "" and info["prompt_full_length"] == 0
            assert info["status"] == "running"
            assert info["event_seq"] == 0 and info["repo_key"] == ""
        finally:
            _cleanup(task_id)
