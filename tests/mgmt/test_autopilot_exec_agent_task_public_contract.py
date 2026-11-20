"""mgmt autopilot 按 public 契约读 agent_task_id（PR #1699 审查 C2 回归）。

故障形态（修复前）：`list_executions` 调 runtime.public.exec_agent_task 后仍按行元组
取 `at_row[0]`，而 public 契约返回 **dict（列名→值）** → 恒抛 `KeyError: 0`，被
`except Exception` 吞成一条 warning，历史执行记录（agent_task_id 列为空、需按
event_seq + started_at 兜底反查）的关联**静默失效**：接口返回空串、日志只有一条
warning，主查询不回退任何错误。

覆盖：
- public 入口 `exec_agent_task` 的返回形态（dict / 键 id / 无命中 None / 元组下标必炸）；
- 端到端：agent_task_id 为空的历史执行记录经 agent_task_id 兜底反查后，接口正确
  带出关联任务 id；
- 直连列优先：agent_task_id 列非空时不得再触发反查。

全部走 sqlite 测试库（tests/conftest.py 初始化），不连生产库、不触发远端执行。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.mgmt.routes import autopilot as ap  # noqa: E402
from src.domains.runtime import public as rt  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402

RT_ID = 16991
RK = "gitcode:autopilot/pr1699"
# 高位执行 id：与 tests/runtime/test_autopilot_exec_agent_task_id.py 同口径，
# 确保落在分页 `ORDER BY e.id DESC LIMIT 50` 的首屏窗口内。
EXEC_BASE = 1905200000
ATID = "t_pr1699_bootstrap"


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _clean():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM autopilot_executions WHERE id >= ? AND id < ?",
                      (EXEC_BASE, EXEC_BASE + 1000))
        except Exception:
            pass
        try:
            c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_pr1699%' OR repo_key=?", (RK,))
        except Exception:
            pass
        db.commit()


def _seed_task(tid, seq, event_type="autopilot_event", created_at=None):
    _created = created_at or _iso(_utcnow())
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, "
            "agent_type, event_type, status, prompt, cwd, skill_files, env_extra, event_seq, "
            "skill, started_at, result_text, created_at) "
            "VALUES (?,?,?,?,?, 'vod', ?, 'running', 'do work', '', '{}', '{}', ?, '', ?, ?, ?)",
            (tid, "sess-" + tid, RT_ID, RK, "#1699", event_type, seq,
             _iso(_utcnow()), "", _created))
        db.commit()


def _seed_exec(exec_id, task_id=1699, agent_task_id=None, started_at=None):
    _started = started_at or _iso(_utcnow() - timedelta(hours=1))
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (exec_id,))
        c.execute(
            "INSERT INTO autopilot_executions "
            "(id, task_id, status, retry_count, result, error, a2a_seq, agent_task_id, "
            " started_at, created_at) "
            "VALUES (?,?,'success',0,'ok','',?,?,?,?)",
            (exec_id, task_id, exec_id, agent_task_id, _started, _started))
        db.commit()


def _find(result, exec_id):
    for row in result["executions"]:
        if row["id"] == exec_id:
            return row
    raise AssertionError(f"seeded execution {exec_id} not returned")


@pytest.fixture(autouse=True)
def _setup():
    # 测试隔离：src.kernel.session_manager 在 **import 时** 就按当时的 DSN 环境变量
    # 建好连接并缓存。若同一 pytest 进程里先跑过 tests/scripts/test_smoke_chat_submit.py
    # （它 monkeypatch.setenv("DEVOPS_DB_HOST", "127.0.0.1") 并加载客户端脚本），
    # 之后的用例会拿一个连到 127.0.0.1 的连接，于是本文件里 seed 到进程库的
    # autopilot_executions 行查不出来 —— 单独跑全绿、合跑 2 红就是这个原因。
    # 这里在 fixture 里显式确认模块已按当前进程配置加载（幂等，已加载则不重复建连）。
    import src.kernel.session_manager  # noqa: F401
    _clean()
    yield
    _clean()


# ── public 入口契约 ────────────────────────────────────────────────────────

def test_public_exec_agent_task_returns_dict_keyed_by_column():
    """public 契约：返回 dict（列名→值），id 可按键取到。"""
    _seed_task(ATID, EXEC_BASE + 1)
    row = rt.exec_agent_task(EXEC_BASE + 1, ATID)
    assert isinstance(row, dict), f"public 契约必须是 dict，实测 {type(row).__name__}"
    assert row.get("id") == ATID


def test_public_exec_agent_task_tuple_index_would_raise():
    """回归锚点：dict 上没有 0 号下标 —— 旧实现 `at_row[0]` 必抛 KeyError。"""
    _seed_task(ATID + "_idx", EXEC_BASE + 2)
    row = rt.exec_agent_task(EXEC_BASE + 2, ATID + "_idx")
    with pytest.raises(KeyError):
        row[0]


def test_public_exec_agent_task_none_when_no_match():
    """兜底也无命中时返回 None（调用方跳过，不得退化成裸 event_seq 查询）。"""
    _seed_exec(EXEC_BASE + 3)
    assert rt.exec_agent_task(EXEC_BASE + 3, "", _iso(_utcnow()), _iso(_utcnow())) is None


# ── 路由端到端：历史执行记录的 agent_task_id 关联 ────────────────────────────

def test_executions_resolves_legacy_agent_task_id_via_public_dict():
    """agent_task_id 列为空的历史执行记录：兜底反查命中后接口必须带出关联 id。

    修复前 `at_row[0]` 抛 KeyError 被 except 吞掉 → 这里恒为空串。
    """
    exec_id = EXEC_BASE + 10
    _seed_task(ATID, exec_id)                       # event_seq=exec_id 的 autopilot_event 任务
    _seed_exec(exec_id, agent_task_id=None)

    out = ap.list_executions(user={}, task_id=None, status=None, limit=50, offset=0)

    assert _find(out, exec_id)["agent_task_id"] == ATID


def test_executions_prefers_direct_agent_task_id_column():
    """agent_task_id 列非空 → 直连列优先，不因兜底反查改动结果。"""
    exec_id = EXEC_BASE + 11
    _seed_task(ATID + "_other", exec_id)            # 兜底若被误用会得到这个 id
    _seed_exec(exec_id, agent_task_id=ATID)

    out = ap.list_executions(user={}, task_id=None, status=None, limit=50, offset=0)

    assert _find(out, exec_id)["agent_task_id"] == ATID
