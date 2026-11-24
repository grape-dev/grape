# -*- coding: utf-8 -*-
"""autopilot_executions 读写必须走 runtime 域游标（手动触发 1146 守护）。

autopilot_executions 归 runtime 域（configs/table_ownership.json），但手动触发
（POST /autopilot/tasks/{id}/trigger）跑在 mgmt 进程：进程库（session_manager.db）
是 mgmt 库，表不在 → 1146 → execution 永远建不出来、页面点「立即触发」无任何
反馈（fire-and-forget 吞掉异常）。定时触发在 worker 进程不受影响。
"""
import inspect


def test_scheduler_imports_runtime_write_primitive():
    from src.domains.runtime.engine import scheduler as sch
    src = inspect.getsource(sch)
    assert 'from src.kernel.db_registry import cursor_for as _rt_cursor_for' in src


def test_create_execution_uses_domain_cursor():
    from src.domains.runtime.engine import scheduler as sch
    body = inspect.getsource(sch._create_execution)
    assert '_rt_cursor_for("runtime", write=True)' in body, (
        "_create_execution 必须走 cursor_for('runtime', write=True)——进程库"
        "(mgmt)里没有 autopilot_executions，手动触发会 1146")


def test_execute_task_result_read_uses_domain_cursor():
    from src.domains.runtime.engine import scheduler as sch
    # 上游把内联查询抽成 _read_exec_state（#3800 error_code）——守卫改锁 helper 本体
    body = inspect.getsource(sch._read_exec_state)
    assert 'FROM autopilot_executions' in body
    assert '_rt_cursor_for("runtime")' in body, (
        "执行结果查询(_read_exec_state)必须走 runtime 域游标（mgmt 进程 1146）")


def test_poll_dispatched_completion_uses_domain_cursor():
    from src.domains.runtime.engine import scheduler as sch
    body = inspect.getsource(sch._poll_dispatched_completion)
    assert '_rt_cursor_for("runtime")' in body, (
        "dispatched 轮询查 exec 行 + agent_tasks 都归 runtime——"
        "mgmt 进程手动触发路径下两表都不在进程库")


def test_update_execution_result_uses_domain_cursor():
    from src.domains.runtime.engine import scheduler as sch
    body = inspect.getsource(sch._update_execution_result)
    assert '_rt_cursor_for("runtime", write=True)' in body


# ── 架构守护 3.0 B1 整改：提交语义必须由 write 原语兜住 ──

def test_write_paths_use_write_primitive_not_manual_commit():
    """写路径 4 处必须 cursor_for(write=True)；块内不得再残留 db.commit()。

    域路由下 db.commit() 提交的是进程（mgmt）连接——撕裂其它线程的事务；
    本地路由下漏 commit 则 sqlite 写丢（审查实测复现）。两拓扑都错。
    """
    import re
    from src.domains.runtime.engine import scheduler as sch
    src = inspect.getsource(sch)

    writes = [
        (sch._create_execution, "INSERT INTO autopilot_executions"),
        (sch._update_execution_result, "UPDATE autopilot_executions SET"),
        (sch._notify_subscribers, "notified=1"),
    ]
    for fn, marker in writes:
        body = inspect.getsource(fn)
        assert f'_rt_cursor_for("runtime", write=True)' in body, (
            f"{fn.__name__} 写路径必须 cursor_for('runtime', write=True)")
        # 写函数体内不得有进程库手工提交（域路由下会提交错连接）
        assert "db.commit()" not in re.sub(r"#.*", "", body), (
            f"{fn.__name__} 内残留 db.commit()（B1 撕裂事务/写丢风险）")

    # agent_task_id 回写在 _execute_autopilot_remote 里,字符串级断言:
    assert 'with _rt_cursor_for("runtime", write=True) as _c:' in src


def test_local_topology_write_lands_on_disk(tmp_path, monkeypatch):
    """行为级验证（local/sqlite 路由）：_create_execution 写后必须真实落盘。

    用独立 sqlite 文件模拟进程库（route_of('runtime')='local'），
    预先建表，写 execution 后**另开连接**读取——读到即证明 write=True
    已提交；读不到即 B1 描述的『写事务挂在共享连接上』。
    """
    import sqlite3 as _s3
    import os as _os

    db_path = str(tmp_path / "rt.sqlite")
    # 预建 schema（列序与 _create_execution 的 INSERT 一致）
    conn = _s3.connect(db_path)
    conn.execute(
        "CREATE TABLE autopilot_executions ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, status TEXT,"
        " retry_count INTEGER, started_at TEXT, created_at TEXT,"
        " agent_type TEXT, params_json TEXT)")
    conn.commit(); conn.close()

    # 进程库切到这个文件：monkeypatch route_of 强制 local,session_manager 指向它
    monkeypatch.setattr("src.kernel.db_registry.route_of", lambda d: "local")
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", _FakeSqlite(db_path))

    from src.domains.runtime.engine import scheduler as sch
    sch._RUNNING_TASKS.discard(1)
    exec_id = sch._create_execution(1, "issue", "")
    assert exec_id, "insert failed"

    probe = _s3.connect(db_path)
    row = probe.execute(
        "SELECT task_id, status FROM autopilot_executions WHERE id=?", (exec_id,)).fetchone()
    probe.close()
    assert row, "B1: write did not land on disk (local/sqlite route, write=True must commit)"
    assert row[0] == 1 and row[1] == "running"


class _FakeSqlite:
    """最小 sqlite 连接替身：session_manager.db 的 cursor/commit/rollback/close。"""
    def __init__(self, path):
        import sqlite3 as _s3
        self._c = _s3.connect(path, check_same_thread=False)

    def cursor(self):
        return self._c.cursor()

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        self._c.close()
