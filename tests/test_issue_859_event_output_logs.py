"""Issue #859: _group_events_by_issue 的 output/logs 永不返回缺陷回归测试。

覆盖：
1. 事件含 structured_output/agent_logs → 分组结果 output/logs 非空且内容正确
2. output 解析失败（非法 JSON）→ 不崩溃，回退 None
3. logs 解析失败（非法 JSON）→ 不崩溃，回退 []
4. 无 structured_output 的事件 → 不含 output 字段（向后兼容）
5. entity_type='pull_request' 事件仍被过滤（回归 Issue #252）
6. runtime/skill 字段取值正确（回归顺带缺陷：旧 SQL 缺列导致 runtime 取到
   entity_type、skill 恒空）
7. numbers 收窄参数仍生效
"""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest
from src.session_manager import db, db_lock

REPO = "gitcode:owner/issue859-test"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()


def _has_structured_col():
    cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(events)").fetchall()]
    return "structured_output" in cols


def _seed_event(seq, issue_id="#859", entity_type="issue", status="completed",
                structured_output=None, agent_logs=None, runtime="", skill=""):
    """直接 INSERT 构造事件行（含 optional structured_output/agent_logs 原始文本）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, "
            "agent_type, runtime, skill, status, agent_logs, result, entity_type, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (seq, REPO, "issue_comment", "gitcode", issue_id, f"t{seq}",
             "coding", runtime, skill, status,
             json.dumps(agent_logs) if agent_logs is not None else "[]",
             "r", entity_type))
        if structured_output is not None or (
                agent_logs is not None and agent_logs != []):
            c.execute("UPDATE events SET structured_output=?, agent_logs=? WHERE seq=?",
                      (json.dumps(structured_output, ensure_ascii=False)
                       if structured_output is not None else None,
                       json.dumps(agent_logs) if agent_logs is not None else "[]",
                       seq))
        db.commit()


# ===== 1. output/logs 非空 =====

class TestOutputLogsReturned:
    def test_output_and_logs_non_empty(self):
        from src.api.routes.issues import _group_events_by_issue
        if not _has_structured_col():
            pytest.skip("events.structured_output column missing")
        structured = {"actions": ["a1"], "conclusion": "结论X", "artifacts": ["PR #1"],
                      "next_step": "a2a_qa", "issue_summary": "s"}
        logs = [{"type": "step_start", "data": {"step": 1}},
                {"type": "tool_use", "data": {"tool": "read"}}]
        _seed_event(85901, structured_output=structured, agent_logs=logs,
                    runtime="opencode", skill="coding-cli-1")

        groups = _group_events_by_issue([REPO])
        evs = groups.get("859", [])
        assert len(evs) == 1
        ev = evs[0]
        assert ev["output"] is not None, "output 永不返回（Issue #859 主缺陷）"
        assert ev["output"]["conclusion"] == "结论X"
        assert ev["output"]["actions"] == ["a1"]
        assert ev["output"]["issue_summary"] == "s"
        assert ev["logs"], "logs 永不返回（Issue #859 主缺陷）"
        assert ev["logs"][0]["type"] == "step_start"
        assert ev["logs"][1]["data"]["tool"] == "read"

    def test_runtime_and_skill_correct(self):
        """回归顺带缺陷：runtime/skill 取值正确（旧实现 runtime 取到 entity_type、skill 恒空）。"""
        from src.api.routes.issues import _group_events_by_issue
        _seed_event(85902, runtime="hermes", skill="issue-skill")
        groups = _group_events_by_issue([REPO])
        ev = groups["859"][0]
        assert ev["runtime"] == "hermes"
        assert ev["skill"] == "issue-skill"


# ===== 2/3. 解析失败回退 =====

class TestParseFallback:
    def test_invalid_output_json_falls_back_none(self):
        from src.api.routes.issues import _group_events_by_issue
        if not _has_structured_col():
            pytest.skip("events.structured_output column missing")
        _seed_event(85903)
        with db_lock:
            db.cursor().execute(
                "UPDATE events SET structured_output='{bad json', agent_logs='{also bad' "
                "WHERE seq=85903")
            db.commit()
        groups = _group_events_by_issue([REPO])
        ev = groups["859"][0]
        assert ev["output"] is None
        assert ev["logs"] == []

    def test_null_output_falls_back_none(self):
        from src.api.routes.issues import _group_events_by_issue
        if not _has_structured_col():
            pytest.skip("events.structured_output column missing")
        _seed_event(85904, structured_output=None, agent_logs=[{"type": "x"}])
        with db_lock:
            db.cursor().execute(
                "UPDATE events SET structured_output=NULL WHERE seq=85904")
            db.commit()
        groups = _group_events_by_issue([REPO])
        ev = groups["859"][0]
        assert ev["output"] is None
        assert ev["logs"] == [{"type": "x"}]


# ===== 4. 向后兼容：无 structured_output 的事件 =====

class TestBackwardCompat:
    def test_event_without_structured_output_still_grouped(self):
        from src.api.routes.issues import _group_events_by_issue
        _seed_event(85905)
        groups = _group_events_by_issue([REPO])
        evs = groups.get("859", [])
        assert len(evs) == 1
        assert evs[0]["event_type"] == "issue_comment"


# ===== 5. entity_type 过滤保留（Issue #252 回归） =====

class TestEntityTypeFilter:
    def test_pr_events_excluded(self):
        from src.api.routes.issues import _group_events_by_issue
        _seed_event(85906, entity_type="issue")
        _seed_event(85907, entity_type="pull_request")
        groups = _group_events_by_issue([REPO])
        evs = groups.get("859", [])
        assert len(evs) == 1, "PR 事件应被过滤（Issue #252）"

    def test_pr_event_with_output_still_excluded(self):
        from src.api.routes.issues import _group_events_by_issue
        if not _has_structured_col():
            pytest.skip("events.structured_output column missing")
        _seed_event(85908, entity_type="issue",
                    structured_output={"conclusion": "issue-done"})
        _seed_event(85909, entity_type="pull_request",
                    structured_output={"conclusion": "pr-done"})
        groups = _group_events_by_issue([REPO])
        evs = groups.get("859", [])
        assert len(evs) == 1
        assert evs[0]["output"]["conclusion"] == "issue-done"


# ===== 6. numbers 收窄 =====

class TestNumbersFilter:
    def test_numbers_clause(self):
        from src.api.routes.issues import _group_events_by_issue
        _seed_event(85910, issue_id="#100")
        _seed_event(85911, issue_id="#200")
        groups = _group_events_by_issue([REPO], numbers=["100"])
        assert list(groups.keys()) == ["100"]
