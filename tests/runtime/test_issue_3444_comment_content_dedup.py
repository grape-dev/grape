"""Issue #3444：评论→Issue 事件内容去重测试。

覆盖：
- 正向：同 Issue 同内容、不同评论 id、同秒 → 仅首条派发，后到落 skipped 留痕，
  summary/event_skipped 可查去重依据（源评论 id / 原事件 seq）。
- 负面：内容不同不误去重；跨时间窗同内容不误去重；窗口关闭时放行。
- 回归：同评论 id 重复投递仍走既有 duplicate_comment_delivery 语义。
"""
import json

import pytest

from src.kernel.session_manager import db, db_lock
from src.api import event_pipeline as ep
from src.domains.runtime import event_store as es


RK = "gitcode:test/i3444"

_clean_sql = [
    "DELETE FROM events WHERE repo_key LIKE 'gitcode:test/i3444%'",
    "DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:test/i3444%'",
    "DELETE FROM event_active_guard WHERE repo_key LIKE 'gitcode:test/i3444%'",
    "DELETE FROM grape_t_comment_dispatch_guard WHERE repo_key LIKE 'gitcode:test/i3444%'",
    "DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:test/i3444%'",
    "DELETE FROM issue WHERE repo_key LIKE 'gitcode:test/i3444%'",
    "DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test/i3444%'",
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(ep, "_enqueue_pipeline_task", lambda *a, **k: None)
    _clean()
    _seed_issue()
    yield
    _clean()


def _clean():
    with db_lock:
        c = db.cursor()
        for sql in _clean_sql:
            c.execute(sql)
        db.commit()


def _seed_issue(num: int = 8, stage: str = "coding", status: str = "processing"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title,"
            " task_description, status, stage, owner_agent)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (2, "alice", RK, num, f"需求 #{num}", "desc", status, stage, stage))
        db.commit()


def _run(payload: dict, header: str = "Note Hook"):
    raw = json.dumps(payload).encode()
    headers = {"X-GitCode-Event": header}
    return ep.run_pipeline(raw, headers, account="test/i3444", repo_key_override="test/i3444")


def _issue_comment_payload(username="alice", note="Issue 评论", num=8, cid=80001):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i3444"},
        "object_attributes": {"noteable_type": "Issue", "iid": num, "note": note,
                              "id": cid, "noteable_iid": num},
        "issue": {"iid": num, "number": num},
        "user": {"username": username},
    }


def _pr_comment_payload(username="alice", note="PR 评论", pr=7, issue=8, cid=70001):
    return {
        "object_kind": "note",
        "project": {"path_with_namespace": "test/i3444"},
        "object_attributes": {"noteable_type": "MergeRequest", "iid": pr, "note": note,
                              "id": cid},
        "merge_request": {"iid": pr, "source_branch": f"feat/issue-{issue}",
                          "title": f"feat: #{issue}"},
        "user": {"username": username},
    }


def _events():
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT seq, status, event_type, issue_id, summary, payload FROM events "
            "WHERE repo_key=? ORDER BY seq", (RK,))
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


class TestContentDedupPositive:
    def test_same_content_distinct_ids_dedup_with_evidence(self, monkeypatch):
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        r1 = _run(_issue_comment_payload(note="同一条澄清", cid=80011))
        r2 = _run(_issue_comment_payload(note="同一条澄清", cid=80012))
        rows = _events()
        normal = [r for r in rows if r[1] != "skipped"]
        deduped = [r for r in rows if r[1] == "skipped"]
        assert len(normal) == 1, (rows, r1, r2)
        assert len(deduped) == 1, (rows, r1, r2)
        assert r2.get("skipped") == "duplicate_comment_content", r2
        assert r2.get("dedup_of_seq") == normal[0][0], (r2, rows)
        dedup = deduped[0]
        assert f"源评论 id=80011" in (dedup[4] or ""), dedup
        assert f"原事件 seq={normal[0][0]}" in (dedup[4] or ""), dedup
        meta = (json.loads(dedup[5] or "{}") or {}).get("_dedup") or {}
        assert meta.get("dedup_of_seq") == normal[0][0], meta
        assert meta.get("source_comment_id") == "80011", meta
        assert _skipped("duplicate_comment_content") == 1

    def test_note_hook_pr_also_dedups(self, monkeypatch):
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        _run(_pr_comment_payload(note="PR 同内容", cid=70011))
        r2 = _run(_pr_comment_payload(note="PR 同内容", cid=70012))
        rows = _events()
        assert len([r for r in rows if r[1] == "skipped"]) == 1, rows
        assert r2.get("skipped") == "duplicate_comment_content", r2


class TestContentDedupNegative:
    def test_distinct_content_not_deduped(self, monkeypatch):
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        _run(_issue_comment_payload(note="第一条澄清", cid=80021))
        r2 = _run(_issue_comment_payload(note="第二条澄清", cid=80022))
        rows = _events()
        assert len([r for r in rows if r[1] != "skipped"]) == 2, (rows, r2)
        assert _skipped("duplicate_comment_content") == 0
        assert r2.get("skipped") is None, r2

    def test_cross_window_not_deduped(self, monkeypatch):
        _clock = iter([1700000000.0, 1700000000.0, 1700000003.0, 1700000003.0])
        monkeypatch.setattr(es, "comment_dedup_now", lambda: next(_clock))
        _run(_issue_comment_payload(note="跨窗评论", cid=80031))
        r2 = _run(_issue_comment_payload(note="跨窗评论", cid=80032))
        rows = _events()
        assert len([r for r in rows if r[1] != "skipped"]) == 2, (rows, r2)
        assert r2.get("skipped") is None, r2
        assert _skipped("duplicate_comment_content") == 0

    def test_window_disabled_allows_duplicates(self, monkeypatch):
        monkeypatch.setenv("GRAPE_COMMENT_DEDUP_WINDOW_SECONDS", "0")
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        _run(_issue_comment_payload(note="关闭窗口", cid=80041))
        r2 = _run(_issue_comment_payload(note="关闭窗口", cid=80042))
        rows = _events()
        assert len([r for r in rows if r[1] != "skipped"]) == 2, (rows, r2)
        assert _skipped("duplicate_comment_content") == 0


class TestCommentIdIdempotencyRegression:
    def test_same_comment_id_keeps_delivery_semantics(self, monkeypatch):
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        _run(_issue_comment_payload(note="同 id 重投", cid=80051))
        r2 = _run(_issue_comment_payload(note="同 id 重投", cid=80051))
        rows = _events()
        assert len(rows) == 1, rows
        assert r2.get("skipped") == "duplicate_comment_delivery", r2
        assert _skipped("duplicate_comment_delivery") == 1
        assert _skipped("duplicate_comment_content") == 0

class TestMissingCommentIdContentDedup:
    """QA 复审：缺失/空评论 id 时仍按内容判重。"""

    @pytest.mark.parametrize("cid", [None, ""])
    def test_missing_or_empty_comment_id_still_dedups_by_content(self, monkeypatch, cid):
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        _run(_issue_comment_payload(note="无 id 评论", cid=cid))
        r2 = _run(_issue_comment_payload(note="无 id 评论", cid=cid))
        rows = _events()
        normal = [r for r in rows if r[1] != "skipped"]
        deduped = [r for r in rows if r[1] == "skipped"]
        assert len(normal) == 1, (rows, r2)
        assert len(deduped) == 1, (rows, r2)
        assert r2.get("skipped") == "duplicate_comment_content", r2
        assert _skipped("duplicate_comment_content") == 1


class TestBlankCommentDedup:
    """QA 复审：空/纯空白评论也写入 _dedup 指纹，后续同内容空评论可判重。"""

    @pytest.mark.parametrize("note", ["", "   ", "\n\t "])
    def test_blank_content_writes_dedup_and_dedups(self, monkeypatch, note):
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        _run(_issue_comment_payload(note=note, cid=80061))
        rows = _events()
        meta = (json.loads(rows[0][5] or "{}") or {}).get("_dedup") or {}
        assert meta.get("content_hash"), (rows, meta)
        r2 = _run(_issue_comment_payload(note=note, cid=80062))
        assert r2.get("skipped") == "duplicate_comment_content", r2
        assert _skipped("duplicate_comment_content") == 1


class TestDedupEvidenceInsertFailure:
    """QA 复审：留痕写入失败时回退正常落库，不得伪装成已去重。"""

    def test_evidence_failure_falls_back_to_normal_append(self, monkeypatch):
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        monkeypatch.setattr(es, "append_comment_dedup_event", lambda *a, **k: 0)
        _run(_issue_comment_payload(note="留痕失败", cid=80071))
        r2 = _run(_issue_comment_payload(note="留痕失败", cid=80072))
        rows = _events()
        assert len([r for r in rows if r[1] != "skipped"]) == 2, (rows, r2)
        assert r2.get("skipped") is None, r2
        assert _skipped("duplicate_comment_content") == 0


class TestCommentIdFirstPriority:
    """QA 复审低危：同评论 id 重投且内容与另一评论 id 原事件相同时保持 id-first。"""

    def test_same_id_redelivery_keeps_delivery_semantics(self, monkeypatch):
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        _run(_issue_comment_payload(note="内容 A", cid=80081))
        _run(_issue_comment_payload(note="内容 B", cid=80082))
        r3 = _run(_issue_comment_payload(note="内容 B", cid=80081))
        rows = _events()
        assert len([r for r in rows if r[1] != "skipped"]) == 2, (rows, r3)
        assert r3.get("skipped") == "duplicate_comment_delivery", r3
        assert _skipped("duplicate_comment_delivery") == 1
        assert _skipped("duplicate_comment_content") == 0


class TestDedupLeftoverDoesNotPolluteNodeStatus:
    """Issue #3444 复审（阻塞项）：去重 skipped 留痕不参与节点状态聚合。

    重复评论产生的 status='skipped' 留痕携带真实 agent_type 且 seq 更新，
    若被当作「最新事件」会把仍在 running / 已 completed 的真实节点状态
    覆盖成 skipped/pending（pipeline_status / issues_center 两处口径）。
    """

    def test_compute_node_statuses_ignores_dedup_leftover(self):
        from src.api.pipeline_status import (
            compute_node_statuses, is_dedup_leftover_event,
            STATUS_COMPLETED, STATUS_SKIPPED as NODE_STATUS_SKIPPED)
        real = {"seq": 101, "agent_type": "issue", "status": "completed"}
        leftover = {"seq": 102, "agent_type": "issue", "status": "skipped",
                    "payload": {"_dedup": {"deduped": True,
                                            "reason": "duplicate_comment_content"}}}
        assert is_dedup_leftover_event(leftover)
        assert not is_dedup_leftover_event(real)
        nodes, statuses = compute_node_statuses(
            [{"id": "issue", "tag": "issue", "name": "Issue"}],
            [real, leftover], "coding")
        # 留痕 seq 更新也不得覆盖真实 completed
        assert statuses[0] == STATUS_COMPLETED, (nodes, statuses)
        assert nodes[0]["latest_event"]["seq"] == 101

    def test_compute_node_statuses_leftover_alone_keeps_node_pending(self):
        """节点只有留痕（无真实事件）时维持待执行，不渲染成 skipped。"""
        from src.api.pipeline_status import compute_node_statuses, STATUS_PENDING
        leftover = {"seq": 201, "agent_type": "issue", "status": "skipped",
                    "payload": {"_dedup": {"deduped": True}}}
        _, statuses = compute_node_statuses(
            [{"id": "issue", "tag": "issue"}], [leftover], "coding")
        assert statuses[0] == STATUS_PENDING

    def test_batch_fetch_agent_statuses_ignores_dedup_leftover(self):
        """issues_center 每 agent 最新事件同样排除 skipped/deduped 留痕。"""
        from src.domains.runtime.issues_center import _batch_fetch_agent_statuses
        from src.domains.runtime.routes.pipeline import STAGE_STATUS_MAP
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM events WHERE repo_key=?", (RK,))
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, source, agent_type,"
                " title, actor, summary, payload, status, result)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (RK, "#8", "issue_comment", "gitcode", "issue",
                 "评论", "alice", "summary", "{}", "completed", ""))
            c.execute(
                "INSERT INTO events (repo_key, issue_id, event_type, source, agent_type,"
                " title, actor, summary, payload, status, result)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (RK, "#8", "issue_comment", "gitcode", "issue",
                 "重复评论已去重", "alice", "summary",
                 json.dumps({"_dedup": {"deduped": True}}), "skipped", ""))
            db.commit()
        out = _batch_fetch_agent_statuses([(RK, "#8")])
        row = [r for r in out[(RK, "#8")] if r["agent"] == "issue"][0]
        assert row["status"] == "completed", row
        assert row["status_num"] == STAGE_STATUS_MAP["completed"], row

    def test_stage_status_map_converges_skipped_deduped(self):
        """routes/pipeline STAGE_STATUS_MAP 与 EVENT_STATUS_MAP 单一来源：
        skipped/deduped 不再缺键回落 0(pending)。"""
        from src.api.pipeline_status import EVENT_STATUS_MAP, STATUS_SKIPPED
        from src.domains.runtime.routes.pipeline import STAGE_STATUS_MAP
        assert STAGE_STATUS_MAP is EVENT_STATUS_MAP
        for key in ("skipped", "deduped", "takeover", "pending", "running",
                    "completed", "failed"):
            assert STAGE_STATUS_MAP.get(key) == EVENT_STATUS_MAP[key], key
        assert STAGE_STATUS_MAP["skipped"] == STATUS_SKIPPED
        assert STAGE_STATUS_MAP["deduped"] == STATUS_SKIPPED

    def test_end_to_end_duplicate_comment_keeps_real_status(self, monkeypatch):
        """回归：重复评论产生 skipped 留痕后，节点状态仍由真实事件（pending）决定，
        不被留痕覆盖成 skipped/pending-after-skipped 等错误口径。"""
        from src.api.pipeline_status import (
            compute_node_statuses, STATUS_PENDING, STATUS_SKIPPED as NODE_STATUS_SKIPPED)
        monkeypatch.setattr(es, "comment_dedup_now", lambda: 1700000000.0)
        _run(_issue_comment_payload(note="端到端同内容", cid=80091))
        r2 = _run(_issue_comment_payload(note="端到端同内容", cid=80092))
        assert r2.get("skipped") == "duplicate_comment_content", r2
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, agent_type, status, payload FROM events "
                "WHERE repo_key=? AND issue_id='#8' ORDER BY seq", (RK,))
            rows = c.fetchall()
        agents = sorted({r[1] for r in rows})
        assert len(agents) == 1 and agents[0] == "coding", rows
        nodes = [{"id": at, "tag": at, "name": at} for at in agents]
        nodes, statuses = compute_node_statuses(nodes, [
            {"seq": r[0], "agent_type": r[1], "status": r[2],
             "payload": json.loads(r[3] or "{}")}
            for r in rows], "coding")
        # 真实事件 pending 保持待执行，seq 更新的 skipped 留痕不得覆盖节点状态
        assert statuses[0] == STATUS_PENDING, (rows, statuses)
        assert statuses[0] != NODE_STATUS_SKIPPED, (rows, statuses)
        assert nodes[0]["latest_event"]["seq"] == rows[0][0], (rows, nodes)
