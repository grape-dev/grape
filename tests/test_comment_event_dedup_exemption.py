"""Issue #1615：评论族去重豁免 + 评论内容 ID 幂等回归测试。

覆盖：
- T1：有未终态事件时 note_hook_pr 仍建事件（活跃守卫不拦截评论族）
- T2：不同评论 ID 的 note_hook_pr 均落库
- T3：同评论 ID 重投仅 1 条事件 + duplicate_comment_delivery 留痕
- T4：issue_comment 同规则（同 ID 幂等、不同 ID 均落库）
- T5：issue_opened 60s 去重不回归（豁免未外溢）
- T6：bot actor 的 note_hook_pr 仍在上游被拦为 self_loop
"""
import json
from unittest.mock import patch, MagicMock

from src.session_manager import db, db_lock
from src.api import event_pipeline as ep


RK = "gitcode:test/i1615"

_clean_sql = [
    "DELETE FROM events WHERE repo_key LIKE 'gitcode:test/i1615%'",
    "DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:test/i1615%'",
    "DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:test/i1615%'",
    "DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:test/i1615%'",
    "DELETE FROM issue WHERE repo_key LIKE 'gitcode:test/i1615%'",
    "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test/i1615%'",
]


def _clean():
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            c.execute(sql)
        db.commit()


def _run(payload: dict, header: str = "Note Hook"):
    raw = json.dumps(payload).encode()
    headers = {"X-GitCode-Event": header}
    return ep.run_pipeline(raw, headers, account="test/i1615", repo_key_override="test/i1615")


def _seed_issue(num: int = 8, stage: str = "coding", status: str = "processing"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title,"
            " task_description, status, stage, owner_agent)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (2, "alice", RK, num, f"需求 #{num}", "desc", status, stage, stage))
        db.commit()


def _seed_active_guard(issue: str = "#8", agent: str = "coding"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR IGNORE INTO event_active_guard (repo_key, issue_id, agent_type, seq, created_at)"
            " VALUES (?,?,?,0,datetime('now'))", (RK, issue, agent))
        db.commit()


def _pr_comment_payload(username="alice", note="PR 评论", pr=7, issue=8, cid=70001):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i1615"},
        "object_attributes": {"noteable_type": "MergeRequest", "iid": pr, "note": note,
                              "id": cid},
        "merge_request": {"iid": pr, "source_branch": f"feat/issue-{issue}",
                          "title": f"feat: #{issue}"},
        "user": {"username": username},
    }


def _issue_comment_payload(username="alice", note="Issue 评论", num=8, cid=80001):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i1615"},
        "object_attributes": {"noteable_type": "Issue", "iid": num, "note": note,
                              "id": cid, "noteable_iid": num},
        "issue": {"iid": num, "number": num},
        "user": {"username": username},
    }


def _issue_opened_payload(username="alice", num=5, title="T"):
    return {"object_kind": "issue", "project": {"path_with_namespace": "test/i1615"},
            "object_attributes": {"id": num, "iid": num, "title": title, "action": "open"},
            "user": {"username": username}}


def _events(event_type=None, issue=None):
    with db_lock:
        c = db.cursor()
        sql = "SELECT seq, status, event_type, issue_id, idempotency_key FROM events WHERE repo_key=?"
        args = [RK]
        if event_type:
            sql += " AND event_type=?"
            args.append(event_type)
        if issue:
            sql += " AND issue_id=?"
            args.append(issue)
        sql += " ORDER BY seq"
        c.execute(sql, args)
        return c.fetchall()


def _skipped(reason=None):
    with db_lock:
        c = db.cursor()
        if reason:
            c.execute("SELECT COUNT(*) FROM event_skipped WHERE repo_key=? AND reason=?",
                      (RK, reason))
        else:
            c.execute("SELECT COUNT(*) FROM event_skipped WHERE repo_key=?", (RK,))
        return c.fetchone()[0]


def _guard_rows(issue="#8", agent="coding"):
    with db_lock:
        row = db.cursor().execute(
            "SELECT COUNT(*) FROM event_active_guard WHERE repo_key=? AND issue_id=? AND agent_type=?",
            (RK, issue, agent)).fetchone()
        return row[0]


class TestNoteHookPrExemption:
    def test_t1_note_hook_pr_created_while_active_event_exists(self, monkeypatch):
        monkeypatch.setattr(ep, "_enqueue_pipeline_task", lambda *a, **k: None)
        _clean()
        _seed_issue(8, "coding")
        _seed_active_guard("#8", "coding")
        res = _run(_pr_comment_payload(cid=70011))
        rows = _events("note_hook_pr", "#8")
        assert len(rows) == 1, (rows, res)
        assert rows[0][1] == "pending", rows
        assert _skipped("dedup:active_coding_for_#8") == 0
        assert _skipped("dedup:recent_note_hook_pr_60s_for_coding") == 0
        assert _guard_rows() == 1, "评论族豁免不得新增/改写活跃守卫行"

    def test_t2_distinct_comment_ids_both_land(self):
        _clean()
        _seed_issue(8, "coding")
        _run(_pr_comment_payload(note="第一条", cid=70021))
        _run(_pr_comment_payload(note="第二条", cid=70022))
        rows = _events("note_hook_pr", "#8")
        assert len(rows) == 2, rows
        assert rows[0][0] != rows[1][0], "不同评论 ID 应产生不同 seq"

    def test_t3_same_comment_id_dedup_with_audit(self):
        _clean()
        _seed_issue(8, "coding")
        _run(_pr_comment_payload(cid=70031))
        res2 = _run(_pr_comment_payload(cid=70031))
        rows = _events("note_hook_pr", "#8")
        assert len(rows) == 1, rows
        assert rows[0][4] == "comment:70031", rows
        assert res2.get("skipped") == "duplicate_comment_delivery", res2
        assert _skipped("duplicate_comment_delivery") == 1


class TestIssueCommentExemption:
    def test_t4_same_id_dedup_and_distinct_ids_land(self):
        _clean()
        _seed_issue(8, "coding")
        _run(_issue_comment_payload(note="a", cid=80041))
        res_dup = _run(_issue_comment_payload(note="a", cid=80041))
        _run(_issue_comment_payload(note="b", cid=80042))
        rows = _events("issue_comment", "#8")
        assert len(rows) == 2, rows
        assert res_dup.get("skipped") == "duplicate_comment_delivery", res_dup
        assert _skipped("duplicate_comment_delivery") == 1
        keys = sorted(r[4] for r in rows)
        assert keys == ["comment:80041", "comment:80042"], keys


class TestNonCommentEventDedupRegression:
    def test_t5_issue_opened_60s_window_still_dedups(self):
        _clean()
        r1 = _run(_issue_opened_payload(num=5), header="issue_opened")
        r2 = _run(_issue_opened_payload(num=5), header="issue_opened")
        rows = _events("issue_opened", "#5")
        assert len(rows) == 1, (rows, r1, r2)
        assert r2.get("skipped") in ("duplicate_active_task", "duplicate_recent_window"), r2
        assert rows[0][4] in (None, ""), "非评论事件不得写 comment: 幂等键"


class TestBotActorStillSelfLoop:
    def test_t6_bot_note_hook_pr_self_loop(self):
        _clean()
        _seed_issue(8, "coding")
        from src.token_resolver import TokenResult
        _bot_level = TokenResult(token="REPO_BOT", level="repo_bot",
                                 provider=MagicMock(), repo_key=RK)
        with patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]), \
             patch("src.token_resolver.TokenResolver.resolve_config_level",
                   side_effect=lambda rk: _bot_level):
            _run(_pr_comment_payload(username="grape_bot", cid=70061))
        assert _events("note_hook_pr", "#8") == [], "bot 评论不得建事件"
        assert _skipped("self_loop") == 1