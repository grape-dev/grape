"""停机 force-fail 正确化测试（graceful-shutdown-requeue-design.md Step 1）。

看护场景：
1) 本机 LOCAL 在跑任务 → terminate_active_agents 强制失败：task.errors 注明系统重启中断、
   events.result 同步写入（避免详情页兜底 summary 显示无意义文案（#2337「A2A: → coding」））；
2) 每个被 force-fail 的本机任务统一 release_busy_slot（不漏槽）；
3) REMOTE（credential_id>0）任务跳过，不误标、不改 busy；
4) 前端 events.js 失败原因不再 fallback summary（源码断言）。
"""
import pytest

from src.session_manager import db, db_lock

RT_LOCAL = 9101
RT_REMOTE = 9102
CRED_REMOTE = 9202
EV_REASON = "任务被系统重启/停机中断"
EV_SUMMARY = "A2A: → coding"


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "agent_runtimes", "events", "runtime_credentials"):
            try:
                c.execute(f"DELETE FROM {t} WHERE runtime_id IN ({RT_LOCAL},{RT_REMOTE}) "
                          "OR id LIKE 't_shdn%' OR repo_key='shutdown/test'")
            except Exception:
                pass
        db.commit()


def _seed_runtime(rid, cid, busy=1):
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        if "busy_slots" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN busy_slots INTEGER DEFAULT 0")
        if "max_concurrent" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN max_concurrent INTEGER DEFAULT 4")
        c.execute("INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
                  "busy_slots, max_concurrent) VALUES (?,?,?,'online',1,1,?,4)",
                  (rid, f"rt-{rid}", cid, busy))
        db.commit()


def _seed_task_and_event(tid, rid, seq):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, agent_type, event_type, "
            "status, prompt, cwd, skill_files, env_extra, event_seq) "
            "VALUES (?,?,?,?,?, 'vod', 'a2a_event', 'running', 'x', '', '{}', '{}', ?)",
            (tid, "sess-" + tid, rid, "shutdown/test", "#1", seq))
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute("INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status, summary, result) "
                  "VALUES (?,?,?,?,?, 'running', ?, '')",
                  (seq, "shutdown/test", "#1", "a2a_event", "vod", EV_SUMMARY))
        db.commit()


def _task_status(tid):
    with db_lock:
        r = db.cursor().execute("SELECT status, errors, exit_code FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        return r


def _event(seq):
    with db_lock:
        r = db.cursor().execute("SELECT status, result, summary FROM events WHERE seq=?", (seq,)).fetchone()
        return r


def _busy(rid):
    with db_lock:
        r = db.cursor().execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        return r[0] if r else None


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


class TestShutdownForceFail:
    def test_local_fail_reason_in_task_and_event(self):
        from src.agent.executor import terminate_active_agents
        """本机任务 force-fail：errors 注明系统重启中断，events.result 同步写入。"""
        _seed_runtime(RT_LOCAL, 0, busy=1)
        _seed_task_and_event("t_shdn_local", RT_LOCAL, 9001)
        terminate_active_agents(grace_seconds=0)
        st = _task_status("t_shdn_local")
        assert st[0] == "failed" and st[2] == -1
        assert EV_REASON in (st[1] or "")
        ev = _event(9001)
        assert ev[0] == "failed"
        assert EV_REASON in (ev[1] or ""), "events.result 未写入，前端将兜底 summary 显示无意义文案"

    def test_local_releases_busy_slot(self):
        from src.agent.executor import terminate_active_agents
        """force-fail 的本机任务统一释放 busy 槽（busy=1 → 0）。"""
        _seed_runtime(RT_LOCAL, 0, busy=1)
        _seed_task_and_event("t_shdn_busy", RT_LOCAL, 9002)
        assert _busy(RT_LOCAL) == 1
        terminate_active_agents(grace_seconds=0)
        assert _busy(RT_LOCAL) == 0, "停机路径漏释放 busy 槽"

    def test_remote_skipped(self):
        from src.agent.executor import terminate_active_agents
        """REMOTE（worker 承载）任务跳过：状态不变、busy 不变、event 不变。"""
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task_and_event("t_shdn_remote", RT_REMOTE, 9003)
        terminate_active_agents(grace_seconds=0)
        assert _task_status("t_shdn_remote")[0] == "running", "REMOTE 任务不应被 force-fail"
        assert _busy(RT_REMOTE) == 1
        assert _event(9003)[0] == "running"

    def test_step2_parent_waits_for_children(self):
        """run.py 父进程 SIGTERM 后 join(timeout) 等待子进程优雅收尾（源码断言）。"""
        src = open("src/api/run.py", encoding="utf-8").read()
        assert "def _shutdown" in src
        assert "join(timeout=" in src, "父进程需 join(timeout) 等待子进程收尾"
        assert "GRAPE_STOP_TIMEOUT" in src

    def test_frontend_fail_reason_no_summary_fallback(self):
        """前端失败原因不再用 summary 兜底（源码断言：只取 data.result + 空态「暂无失败原因」）。"""
        src = open("src/api/static/js/pages/events.js", encoding="utf-8").read()
        i = src.find("function _evFailReasonText")
        assert i >= 0, "未找到 _evFailReasonText"
        j = src.find("\nfunction ", i)
        seg = src[i:j if j != -1 else i + 1400]
        assert ("暂无失败原因" in seg) or ("no_fail_reason" in seg), "失败事件应有空 result 的「暂无失败原因」态"
        assert "data.result" in seg
        assert "_evResultOf(" not in seg, "失败原因不得再走 result||summary 兜底"
        assert "summary" not in seg, "失败原因不得引用 summary（事件标题/A2A 交棒）"