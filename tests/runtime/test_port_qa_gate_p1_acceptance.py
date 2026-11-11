"""P1 验收：QA 门禁（#1739）落到 arch 布局后的行为契约。

只驱动真实函数（classify_qa_conclusion / qa_gate_check / _review_pass /
auto_finalize_issue_on_last_node），造数走真实表结构与 session_store 接口。

跑法（仓库根目录）：PYTHONPATH=. python -m pytest -q tests/runtime/test_port_qa_gate_p1_acceptance.py
"""
import os

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.db_setup import init_database  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402

REPO = "gitcode:acme/port-p1"
NUM = 3001
ALL_COMPLETED = {"vod": "completed", "issue": "completed", "coding": "completed",
                 "qa": "completed", "sre": "completed"}


def setup_module():
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, "
                  "full_name, webhook_id, bot_gitcode_token) VALUES (?,?,?,?,?,?)",
                  (REPO, "acme", "port-p1", "acme/port-p1", 1, "bot_p1_token"))
        db.commit()


def teardown_module():
    with db_lock:
        c = db.cursor()
        for _t in ("issue", "events", "event_skipped", "managed_repos"):
            c.execute(f"DELETE FROM {_t} WHERE repo_key=?", (REPO,))
        db.commit()


def _reset(stage="sre", status="processing"):
    from src.api.session_store import set_issue_stage
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("INSERT OR REPLACE INTO issue (user_id, assignee, repo_key, issue_number, "
                  "status, completed_at, completed_reason, scene_id) "
                  "VALUES (0, '', ?, ?, ?, NULL, NULL, '')", (REPO, NUM, status))
        db.commit()
    set_issue_stage(REPO, f"#{NUM}", stage, stage)


def _seed_events(statuses: dict, qa_result: str = ""):
    with db_lock:
        c = db.cursor()
        for ag, st in statuses.items():
            c.execute("INSERT INTO events (repo_key, issue_id, event_type, agent_type, "
                      "status, result, scene_id) VALUES (?,?,?,?,?,?,?)",
                      (REPO, f"#{NUM}", "a2a_event", ag, st,
                       qa_result if ag == "qa" else "", ""))
        db.commit()


def _issue_row():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, completed_reason, stage FROM issue "
                  "WHERE repo_key=? AND issue_number=?", (REPO, NUM))
        return c.fetchone()


QA_FAIL_TEXT = "A1 FAIL 135 项（collect/原始案例.md 缺失）/ A2·A3·A4 PASS"
QA_PASS_TEXT = "报告 QA 8/8 通过放行（13-20 全 PASS；LLM 叙事复核四项通过）"


class TestQaGatePorted:
    def test_classification_contract(self):
        """判定契约：本批 #1963 修的正/反向样本在 arch 树上同样成立。"""
        from src.api.qa_gate import classify_qa_conclusion
        assert classify_qa_conclusion("completed", QA_PASS_TEXT) == "pass"
        assert classify_qa_conclusion("completed", QA_FAIL_TEXT) == "fail"
        assert classify_qa_conclusion("completed", "未发现任何阻塞项，全部通过") == "pass"
        assert classify_qa_conclusion("completed", "未全部驳回，仍有 2 项待整改") == "fail"
        assert classify_qa_conclusion("running", QA_PASS_TEXT) == "unknown"

    def test_gate_check_blocks_failed_qa(self):
        _reset()
        _seed_events(ALL_COMPLETED, qa_result=QA_FAIL_TEXT)
        from src.api.qa_gate import qa_gate_check
        info = qa_gate_check(REPO, NUM)
        assert info["blocked"] is True and info["conclusion"] == "fail", info

    def test_gate_check_allows_passed_qa(self):
        _reset()
        _seed_events(ALL_COMPLETED, qa_result=QA_PASS_TEXT)
        from src.api.qa_gate import qa_gate_check
        info = qa_gate_check(REPO, NUM)
        assert info["blocked"] is False and info["conclusion"] == "pass", info

    def test_review_pass_refuses_done_when_qa_failed(self):
        """接线点 _review_pass：qa fail → noop:qa_failed，且 issue 不得 completed。"""
        _reset(stage="pending_review")
        _seed_events(ALL_COMPLETED, qa_result=QA_FAIL_TEXT)
        from src.api.issues_center import _review_pass
        rc = _review_pass(REPO, NUM, target="done", evidence="agent says pass")
        assert rc == "noop:qa_failed", rc
        row = _issue_row()
        assert (row[0] or "") != "completed", row

    def test_complete_issue_done_reuses_gate_result(self):
        """接线点 _complete_issue_done：传入 blocked 的 qa_info → 不落 completed。"""
        _reset(stage="pending_review")
        from src.api.issues_center import _complete_issue_done
        blocked = {"blocked": True, "conclusion": "fail", "event_seq": 42}
        assert _complete_issue_done(REPO, NUM, "e", qa_info=blocked) is False
        assert (_issue_row()[0] or "") != "completed"

    def test_auto_finalize_blocked_moves_to_pending_review(self):
        """接线点 auto_finalize：#1739 拦截后必须落 pending_review（#1963 层 3）。

        前提：完成的 agent 必须是自己所在流水线的末节点（arch 默认场景末节点 = sre），
        否则会先返回 noop:not_last_node 到不了门禁。
        """
        _reset(stage="sre")
        _seed_events(ALL_COMPLETED, qa_result=QA_FAIL_TEXT)
        from src.api.issues_center import auto_finalize_issue_on_last_node
        rc = auto_finalize_issue_on_last_node(REPO, f"#{NUM}", "sre", source="port-p1")
        assert rc in ("qa_failed_blocked", "qa_failed_blocked_stage_error"), rc
        row = _issue_row()
        assert (row[0] or "") != "completed", row

    def test_gate_open_when_no_qa_event(self):
        """场景有 qa 节点但没有 qa 事件 → 门禁不阻断（fail-open，不误伤存量卡）。"""
        _reset(stage="sre")
        _seed_events({"vod": "completed", "issue": "completed", "coding": "completed"})
        from src.api.qa_gate import qa_gate_check
        info = qa_gate_check(REPO, NUM)
        assert info["blocked"] is False, info