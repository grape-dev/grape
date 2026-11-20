"""项目级 prompt 上下文必须消费结构化任务产出（Issue #2269 质量整改）。

历史问题：events.result 在流结束兜底时可能被写成「任务已完成」，项目级
get_issue_summary 只读它，于是上下文只剩完成状态，丢失结论/验证/产物。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.api.event_store import (  # noqa: E402
    append_event,
    get_issue_summary,
    get_latest_task_digest,
)
from src.kernel.session_manager import db, db_lock  # noqa: E402

REPO = "gitcode:test/context-digest"
TASK_ID = "t_context_digest"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _cleanup() -> None:
    with db_lock:
        c = db.cursor()
        for table in ("events", "agent_tasks"):
            c.execute(f"DELETE FROM {table} WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM organization WHERE id=?", ("org-ctx",))
        db.commit()


def _mark_org_repo() -> None:
    """把测试仓库标记为团队组织关联仓库。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO organization "
            "(id, name, slug, owner_id, is_personal) VALUES (?,?,?,?,0)",
            ("org-ctx", "org-ctx", "org-ctx-digest", "0"),
        )
        c.execute(
            "INSERT OR REPLACE INTO managed_repos "
            "(repo_key, repo_owner, repo_name, full_name, org_id) "
            "VALUES (?,?,?,?,?)",
            (REPO, "test", "context-digest", "test/context-digest", "org-ctx"),
        )
        db.commit()


def _insert_task(task_id: str, result_text: str,
                 status: str = "completed", issue_id: str = "#6",
                 completed_at: str = "2026-11-16 10:00:00",
                 agent_type: str = "coding") -> None:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO agent_tasks "
            "(id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, "
            "agent_type, status, prompt, result_text, created_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "s1", 0, None, issue_id, REPO, agent_type, status,
             "prompt", result_text, "2026-11-16 09:00:00", completed_at),
        )
        db.commit()


def test_issue_summary_prefers_structured_task_output_over_placeholder_result():
    _mark_org_repo()
    append_event(REPO, "push", "PR #6 审查", issue_id="#6", agent_type="coding",
                 status="completed", result="任务已完成：级联收敛（回报丢失补偿）")
    _insert_task(TASK_ID, json.dumps({
        "actions": ["代码审查（5 轴，本地复跑 38 用例）", "发布 PR #6 审查评论"],
        "conclusion": "PR #6 审查通过：0 Critical/0 Required，3 Optional + 3 Nit",
        "artifacts": ["PR #6 https://example.test/merge_requests/6"],
        "next_step": "等待用户确认合入",
        "issue_summary": "PR #6 审查完成 | 38 用例复跑通过 | 待合入确认",
    }, ensure_ascii=False))

    summary = get_issue_summary(REPO)
    assert "#6" in summary
    assert "38 用例复跑通过" in summary
    assert "PR #6" in summary
    assert "任务已完成：级联收敛" not in summary


def test_issue_summary_falls_back_to_event_result_without_task():
    append_event(REPO, "push", "部署", issue_id="#7", agent_type="sre",
                 status="completed", result="部署完成 EIP=1.2.3.4")
    summary = get_issue_summary(REPO)
    assert "#7" in summary
    assert "部署完成 EIP=1.2.3.4" in summary


def test_latest_task_digest_accepts_task_id_and_issue_variants():
    _insert_task(TASK_ID, json.dumps({"conclusion": "指定任务结论"},
                                     ensure_ascii=False))
    by_id = get_latest_task_digest(REPO, task_id=TASK_ID)
    assert by_id["task_id"] == TASK_ID
    assert by_id["output"]["conclusion"] == "指定任务结论"

    for issue in ("#6", "6"):
        by_issue = get_latest_task_digest(REPO, issue)
        assert by_issue["task_id"] == TASK_ID


def test_plain_text_result_text_becomes_conclusion_fallback():
    _insert_task(TASK_ID, "纯文本结论：接口已上线")
    digest = get_latest_task_digest(REPO, "#6")
    assert digest["output"]["conclusion"] == "纯文本结论：接口已上线"


def test_non_terminal_task_is_ignored():
    _insert_task(TASK_ID, json.dumps({"conclusion": "还在跑"}, ensure_ascii=False),
                 status="running")
    assert get_latest_task_digest(REPO, "#6") == {}

def test_issue_summary_keeps_coding_and_qa_memory_together():
    _mark_org_repo()
    append_event(REPO, "push", "PR #8", issue_id="#8", agent_type="coding",
                 status="completed", result="任务已完成")
    _insert_task("t_coding_8", json.dumps({
        "actions": ["修复 src/probe.py 参数校验", "pytest -q：12 passed"],
        "conclusion": "实现参数校验并补齐回归测试",
        "artifacts": ["分支 feat/issue-8"],
        "issue_summary": "实现参数校验 | 12 passed",
    }, ensure_ascii=False), issue_id="#8",
        completed_at="2026-11-16 09:10:00", agent_type="coding")
    _insert_task("t_qa_8", json.dumps({
        "actions": ["复审 PR head 并独立复跑 12 用例"],
        "conclusion": "QA 复审通过，1 Nit 不阻断",
        "artifacts": ["PR #8 https://example.test/merge_requests/8"],
        "issue_summary": "QA 复审通过 | 12 passed",
    }, ensure_ascii=False), issue_id="#8",
        completed_at="2026-11-16 09:20:00", agent_type="qa")

    from src.api.event_store import get_issue_task_digests

    memories = get_issue_task_digests(REPO, "#8", limit=3)
    assert [m["agent_type"] for m in memories] == ["qa", "coding"], memories

    summary = get_issue_summary(REPO)
    assert "QA 复审通过" in summary
    assert "实现参数校验" in summary
    assert "实现: 实现参数校验" in summary
    assert "12 passed" in summary
    assert "PR #8" in summary


def test_issue_summary_keeps_qa_conclusion_when_coding_is_latest():
    _mark_org_repo()
    append_event(REPO, "push", "PR #9", issue_id="#9", agent_type="coding",
                 status="completed", result="任务已完成")
    _insert_task("t_qa_9", json.dumps({
        "conclusion": "QA 审查通过，2 Nit 不阻断",
        "issue_summary": "QA 审查通过 | 20 passed",
    }, ensure_ascii=False), issue_id="#9", agent_type="qa",
        completed_at="2026-11-16 09:10:00")
    _insert_task("t_coding_9", json.dumps({
        "conclusion": "实现完成并补齐测试",
        "issue_summary": "实现完成 | 20 passed",
    }, ensure_ascii=False), issue_id="#9", agent_type="coding",
        completed_at="2026-11-16 09:20:00")

    summary = get_issue_summary(REPO)
    assert "最新结论: 实现完成" in summary
    assert "审查: QA 审查通过" in summary


def test_non_org_repo_keeps_legacy_result_summary():
    """普通仓库必须保持原逻辑：即使有结构化 task，也只读 events.result。"""
    append_event(REPO, "push", "PR #10", issue_id="#10", agent_type="coding",
                 status="completed", result="原有占位结论：任务已完成")
    _insert_task("t_coding_10", json.dumps({
        "conclusion": "结构化结论不应出现在普通仓库摘要",
        "issue_summary": "结构化结论 | 99 passed",
    }, ensure_ascii=False), issue_id="#10", agent_type="coding",
        completed_at="2026-11-16 09:10:00")

    summary = get_issue_summary(REPO)
    assert "原有占位结论" in summary
    assert "结构化结论" not in summary


def test_task_digest_selection_prefers_implementation_over_later_noop():
    """后续 PR 打开/无变更事件不能把真正的实现卡片挤掉。"""
    from src.api.event_store import get_issue_task_digests

    _mark_org_repo()
    append_event(REPO, "push", "PR #11", issue_id="#11", agent_type="coding",
                 status="completed", result="任务已完成")
    _insert_task("t_impl_11", json.dumps({
        "actions": [
            "新增 src/locking.py：flock 独立锁文件与等待超时",
            "重构 src/todo_store.py：fsync 原子写与损坏恢复",
            "pytest -q：80 passed",
        ],
        "conclusion": "已实现并发安全、崩溃恢复与幂等重试",
        "artifacts": ["分支 feat/issue-11", "PR #11"],
        "issue_summary": "实现完成 | 80 passed",
    }, ensure_ascii=False), issue_id="#11", agent_type="coding",
        completed_at="2026-11-16 09:10:00")
    _insert_task("t_noop_11", json.dumps({
        "actions": ["核对 PR 状态", "本次无新增改动，不重复发评论"],
        "conclusion": "PR #11 为上一轮已交付成果，本事件无需新改动",
        "artifacts": ["PR #11"],
        "issue_summary": "无新变更",
    }, ensure_ascii=False), issue_id="#11", agent_type="coding",
        completed_at="2026-11-16 09:20:00")

    digests = get_issue_task_digests(REPO, "#11", limit=3)
    coding = next(d for d in digests if d["agent_type"] == "coding")
    assert coding["task_id"] == "t_impl_11"
    assert "并发安全" in (coding["output"] or {}).get("conclusion", "")
