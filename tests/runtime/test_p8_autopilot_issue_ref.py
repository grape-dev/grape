"""P8-consistency 回归：autopilot（agent_type='issue'）任务创建即落 issue_id 回链键。

缺陷（DB 实查 live 库）：agent_tasks 中 issue 型任务 issue_id 为空——gitee/gitcode/github
多条（全是 failed），issue 统计与时间线按 agent_tasks.issue_id 回链分组
（src/domains/insight/routes/dashboard.py:565 显式 IS NOT NULL），失败任务丢统计
→ 三页面数字对不上。根因：autopilot 任务（agent_type 固定 'issue'）创建入队时
issue_id 恒传空——direct 路径 _create_direct_task(issue_id="")、remote 路径
enqueue_task(issue_id="")。修复：创建时即落无数字合成键 _autopilot_issue_ref(task_id)
（确定性：同一 autopilot 任务每次触发相同），失败路径同样带回链。

锁定的行为：
- T1 合成键形态：确定性、无数字（_issue_num→0，不误挂真实 issue 号）、任务间互异；
- T2 direct 失败路径：_execute_autopilot_direct 执行失败（exit_code=1）后
  agent_tasks 行仍带 issue_id（回链在创建时已落，不依赖成功收口）；
- T3 remote 入队路径：_execute_autopilot_remote 传给 enqueue_task 的 issue_id
  为同一合成键（创建入队即落）。

全部走 sqlite 测试库（tests/conftest.py 初始化），不连生产库、不触发远端执行。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock

RK = "gitcode:p8-autopilot/test-ref"
EXEC_ID = 1910000001


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks",):
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key=?", (RK,))
            except Exception:
                pass
        db.commit()


def _agent_task_row(task_id):
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, issue_id, agent_type, status, event_type FROM agent_tasks WHERE id=?",
            (task_id,)).fetchone()
    return row


def test_t1_ref_shape():
    """合成键：确定性、无数字、互异；issue 号解析链全短路。"""
    from src.domains.runtime.engine.scheduler import _autopilot_issue_ref
    from src.domains.runtime.engine.service import _issue_num
    from src.domains.runtime.engine.repogit import is_git_agent

    assert _autopilot_issue_ref(1) == "autopilot-a"
    assert _autopilot_issue_ref(1) == _autopilot_issue_ref(1)  # 确定性
    refs = [_autopilot_issue_ref(i) for i in range(1, 60)]
    assert len(set(refs)) == len(refs)  # 任务间互异（统计分组不粘连）
    for r in refs:
        assert "0" not in r and not any(ch.isdigit() for ch in r), r  # 无数字
        assert _issue_num(r) == 0  # 不会误挂真实 issue 号（collect_knowledge_files 安全）
    # worktree 派生门：issue 型非 git agent，issue_num_from 即便非空也短路
    assert is_git_agent("issue") is False
    # 无效入参不产键（不硬造）
    assert _autopilot_issue_ref(0) == "" and _autopilot_issue_ref(None) == ""


def test_t2_direct_failed_path_keeps_issue_ref(monkeypatch, tmp_path):
    """direct 失败路径：任务行创建即带 issue_id，失败收口不丢回链。"""
    from src.domains.runtime.engine import scheduler

    _clean()
    task = {"id": 7, "skill_name": "", "created_by": 0}
    _fin = {}

    def _fake_finalize(task_id, exec_id, exit_code, final_text):
        _fin["task_id"] = task_id
        _fin["exit_code"] = exit_code
        # 模拟失败收口（真实路径经 update_task_status 统一终态入口）
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_tasks SET status='failed', exit_code=? WHERE id=?",
                      (exit_code, task_id))
            db.commit()

    monkeypatch.setattr(scheduler, "_finalize_autopilot_task", _fake_finalize)
    monkeypatch.setattr(scheduler, "_update_execution_result", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("engine exploded")

    import src.domains.runtime.engine.executor as _ex
    monkeypatch.setattr(_ex, "stream_devops_agent", _boom)

    import src.domains.insight.observability.master_usage as _mu_mod

    class _DummyUsage:
        def start(self):
            pass

        def feed(self, *a, **k):
            pass

        def finish(self, *a, **k):
            pass

    monkeypatch.setattr(_mu_mod, "for_task", lambda *a, **k: _DummyUsage())

    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    scheduler._execute_autopilot_direct(
        RK, task, EXEC_ID, "issue", "",
        0, "test-engine", "test-engine", ws, "do work",
        "", "", {"REPO_FULL_NAME": "p8-autopilot/test-ref"}, "p8-autopilot/test-ref",
        {"repo_key": RK, "issue_id": "", "payload": {}})

    task_id = _fin.get("task_id")
    assert task_id, "失败收口必须已执行（exit_code=1）"
    assert _fin["exit_code"] == 1
    row = _agent_task_row(task_id)
    assert row, "agent_tasks 行必须落库"
    assert row[1] == scheduler._autopilot_issue_ref(7), (
        f"失败任务必须带 issue 回链键，实际 {row[1]!r}")
    assert row[2] == "issue" and row[3] == "failed"
    assert row[4] in ("", "autopilot_event")  # direct 路径 event_type 恒空（不写 events 表）
    _clean()


def test_t3_remote_enqueue_carries_issue_ref(monkeypatch, tmp_path):
    """remote 入队路径：enqueue_task 收到同一合成键（创建入队即落，不靠执行成功）。"""
    from src.domains.runtime.engine import scheduler
    import src.domains.runtime.engine.service as svc
    import src.api.session_store as ss
    import src.kernel.prompt_builder as pb

    _clean()
    captured = {}

    def _fake_enqueue(**kw):
        captured.update(kw)
        return "t_p8remote"

    monkeypatch.setattr(svc, "enqueue_task", _fake_enqueue)
    monkeypatch.setattr(scheduler, "_update_execution_result", lambda *a, **k: None)
    monkeypatch.setattr(ss, "get_or_create_agent_session",
                        lambda *a, **k: {"id": "sess-p8"})
    monkeypatch.setattr(ss, "get_or_create_issue_session",
                        lambda *a, **k: {"id": ""})  # 空指针 → 跳过 UPDATE，零副作用
    monkeypatch.setattr(pb, "_resolve_server_url", lambda *a, **k: "http://test")

    task = {"id": 9, "skill_name": "", "created_by": 0, "engine_runtime_ids": ""}
    scheduler._execute_autopilot_remote(
        RK, task, EXEC_ID + 1, "issue", "",
        0, "test-engine", "test-engine", str(tmp_path), "do work",
        "", "", {"REPO_FULL_NAME": "p8-autopilot/test-ref"}, "p8-autopilot/test-ref",
        {"repo_key": RK, "issue_id": "", "payload": {}})

    assert captured.get("task_id") == "t_p8remote" or captured, "enqueue 必须被调用"
    assert captured["issue_id"] == scheduler._autopilot_issue_ref(9), (
        f"入队任务必须创建即带 issue 回链键，实际 {captured.get('issue_id')!r}")
    assert captured["event_type"] == "autopilot_event"
    _clean()


# ---------- T4 场景锁：三数一致性（验收 P8 修复批收口） ----------
# 场景：失败 autopilot 任务带合成回链键 → issue 级统计计入（不再被
# dashboard.py:565 的 IS NOT NULL 过滤丢掉）；vod 型空回链保持不计（设计口径）。

def test_p8_stats_consistency_failed_autopilot_counted(tmp_path):
    """T4：issue 统计口径含失败 autopilot 任务（合成键），vod 空回链不计。

    验收 P8 实锤：失败 autopilot 任务 issue_id 为空被统计丢弃 → 时间线/
    统计/任务列表三数不一致。修复后创建即落合成键，本用例锁统计侧口径。
    """
    from src.domains.runtime.engine import scheduler as scheduler_mod

    _clean()
    try:
        _ref = scheduler_mod._autopilot_issue_ref(77)
        with db_lock:
            c = db.cursor()
            # 失败 autopilot 任务（带合成回链键）
            c.execute(
                "INSERT INTO agent_tasks (id, repo_key, agent_type, agent_session_id, "
                "runtime_id, prompt, status, issue_id, created_at) "
                "VALUES ('t-p8-stats-1', ?, 'issue', 'sess-p8-1', 1, 'p', 'failed', ?, datetime('now'))",
                (RK, _ref))
            # vod 型空回链（设计口径：不进 issue 统计）
            c.execute(
                "INSERT INTO agent_tasks (id, repo_key, agent_type, agent_session_id, "
                "runtime_id, prompt, status, issue_id, created_at) "
                "VALUES ('t-p8-stats-2', ?, 'vod', 'sess-p8-2', 1, 'p', 'failed', '', datetime('now'))",
                (RK,))
            db.commit()
            # dashboard.py:565 同口径：issue_id IS NOT NULL AND <> '' 按仓过滤
            n_issue = c.execute(
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE repo_key=? AND issue_id IS NOT NULL AND issue_id <> ''",
                (RK,)).fetchone()[0]
            n_all = c.execute(
                "SELECT COUNT(*) FROM agent_tasks WHERE repo_key=?", (RK,)).fetchone()[0]
        assert n_issue == 1, \
            f"失败 autopilot 任务（合成键）应计入 issue 统计，实际 {n_issue}"
        assert n_all == 2, "两行种子应在库"
    finally:
        _clean()
