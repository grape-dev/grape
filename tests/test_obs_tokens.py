"""Token 三级归因单测（引擎 / 任务 / 阶段事件）。"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.observability import tokens as T  # noqa: E402

UID = 7801
REPO = "gitcode:org/tok-x"
NOW = datetime.now(timezone.utc)


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM obs_llm_usage WHERE task_id LIKE 't_tok_%'")
            db.commit()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _setup():
    _clean()
    yield
    _clean()


def _mk(task_id, engine_id=1, issue_id="#1", stage="vod", minutes_ago=30, inp=100, out=50,
        cache_r=10, cache_w=5, reason=7, run_kind="task", ts=None):
    from src.session_manager import db, db_lock
    when = ts or (NOW - timedelta(minutes=minutes_ago)).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO obs_llm_usage (dedup_key, task_id, seq, source, repo_key,"
                  " agent_type, engine_id, user_id, run_kind, run_id, created_at,"
                  " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
                  " reasoning_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"t:{task_id}:{minutes_ago}", task_id, 1, "report", REPO, stage,
                   engine_id, UID, run_kind, task_id if run_kind == "task" else task_id,
                   when, inp, out, cache_r, cache_w, reason))
        db.commit()


def test_total_math_and_engine_split():
    """total = input+output+cache_read+cache_write（**不含 reasoning**）。"""
    _mk("t_tok_a", engine_id=1, inp=100, out=50, cache_r=10, cache_w=5, reason=7)
    _mk("t_tok_b", engine_id=2, inp=200, out=20, cache_r=0, cache_w=0, reason=999)
    d = T.engine_totals(global_scope=True, range_str="24h", now=NOW)
    assert d["rows"] == 2, d
    # 165 = 100+50+10+5；220 = 200+20+0+0 → 合计 385；reasoning(7+999) 不进 total
    assert d["total"]["total"] == 385, d["total"]
    assert d["total"]["reasoning"] == 1006, d["total"]
    by_id = {e["engine_id"]: e for e in d["engines"]}
    assert by_id[1]["total"] == 165 and by_id[2]["total"] == 220, by_id
    assert by_id[1]["tasks"] == 1 and by_id[1]["engine_id"] == 1
    # 引擎降序
    assert [e["engine_id"] for e in d["engines"]] == [2, 1], d["engines"]


def test_cst_style_row_excluded_by_exact_window():
    """CST 口径写入的行（naive 时间 = UTC+8h，落在"未来"）不得被算进窗口。"""
    from src.api.time_utils import normalize_event_time
    _mk("t_tok_ok", engine_id=1, minutes_ago=30)
    # naive "现在" → 解释成 CST → UTC 视角是 now+8h（未来）
    naive = (datetime.now() ).strftime("%Y-%m-%d %H:%M:%S")
    _mk("t_tok_cst", engine_id=9, minutes_ago=0, ts=naive)
    assert normalize_event_time(naive)  # 有解析
    d = T.engine_totals(global_scope=True, range_str="24h", now=NOW)
    assert {e["engine_id"] for e in d["engines"]} == {1}, d["engines"]


def test_scope_empty_never_degrades():
    """可见集合为空 → 空结果（不降级成全量）。"""
    _mk("t_tok_any", engine_id=1)
    d = T.engine_totals(user={"id": 424242, "org_id": 0, "org_role": "viewer"},
                        range_str="24h", now=NOW)
    assert d["engines"] == [] and d["total"]["total"] == 0, d
    _, _, ok = T._scope({}, "", False, None)
    assert ok is False


def test_stage_grouping_one_issue_two_stages():
    """阶段事件 = issue × agent_type：#1 走 vod → issue 是两行。"""
    rows = [
        {"task_id": "t1", "agent_type": "vod", "issue_id": "#1", "engine_id": 1, "repo_key": REPO,
         "run_kind": "task", "run_id": "t1", "created_at": NOW.isoformat(),
         "input_tokens": 10, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "reasoning_tokens": 0, "cost_usd": 0},
        {"task_id": "t2", "agent_type": "issue", "issue_id": "#1", "engine_id": 1, "repo_key": REPO,
         "run_kind": "task", "run_id": "t2", "created_at": NOW.isoformat(),
         "input_tokens": 20, "output_tokens": 2, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "reasoning_tokens": 0, "cost_usd": 0},
        {"task_id": "t3", "agent_type": "vod", "issue_id": "#2", "engine_id": 2, "repo_key": REPO,
         "run_kind": "task", "run_id": "t3", "created_at": NOW.isoformat(),
         "input_tokens": 30, "output_tokens": 3, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "reasoning_tokens": 0, "cost_usd": 0},
    ]
    monkey = pytest.MonkeyPatch()
    monkey.setattr(T, "_fetch", lambda *a, **k: rows)
    try:
        d = T.stage_totals(global_scope=True, range_str="24h", now=NOW)
        got = {(s["issue_id"], s["stage"]): s["total"] for s in d["stages"]}
        assert got == {("#1", "vod"): 11, ("#1", "issue"): 22, ("#2", "vod"): 33}, got
        one = T.issue_stages("#1", global_scope=True, range_str="24h", now=NOW)
        assert {s["stage"] for s in one["stages"]} == {"vod", "issue"}, one
        t = T.task_totals(global_scope=True, range_str="24h", now=NOW)
        assert t["total"]["total"] == 66, t["total"]
        # 三层合计相等（验收硬断言口径）
        s = T.summary(global_scope=True, range_str="24h", now=NOW)
        assert s["engines"] == s["tasks"] == s["stages"] == 66 and s["equal"], s
    finally:
        monkey.undo()
