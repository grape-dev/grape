"""Issue #372 §18: _create_direct_task / _create_failed_task pool_engine_source 修复测试

E2E 实测发现 direct 模式下 task 记录未落库——_task_insert_row 缺 pool_engine_source
列值抛 ValueError，内部 try/except 捕获返回空字符串，调用方不检查直接 start_direct_agent，
agent 内存执行但 DB 无记录。

测试覆盖：
- T8-01: _create_direct_task 成功落库，pool_engine_source=""
- T8-02: _create_failed_task 成功落库，pool_engine_source=""
- T8-03: _create_direct_task 返回非空 task_id
- T8-04: _create_direct_task 落库的 task 行 status=queued + event_seq 正确
- T8-05: _create_failed_task 落库的 task 行 status=failed + errors 非空
- T8-06: _task_insert_row 包含 pool_engine_source 列（防列清单漂移回归）
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

# 从 event_pipeline 导入 db（而非 session_manager）——确保与 _create_direct_task
# 使用同一个 db 连接对象。全量 collection 时其他测试模块的 os.environ 改写
# 不会影响已导入的 db 引用，但可能导致 session_manager.db 被重绑为新连接，
# 如果测试从 session_manager 导入 db 会拿到新连接，与 event_pipeline 的旧连接不一致。
from src.api.event_pipeline import db, db_lock

REPO = "gitcode:owner/test-issue372-pool-engine-source"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (REPO,))
        db.commit()


def setup_function():
    _clean()


def teardown_function():
    _clean()


def _get_task_row(seq):
    """查 agent_tasks 行，含 pool_engine_source 列。"""
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT id, event_seq, status, pool_engine_source, errors "
            "FROM agent_tasks WHERE repo_key=? AND event_seq=?",
            (REPO, seq),
        ).fetchone()


# ===========================================================================
# T8-06: 列清单防漂移回归
# ===========================================================================

def test_task_columns_list_contains_pool_engine_source():
    """T8-06: _TASK_COLUMNS_LIST 必须包含 pool_engine_source（防列清单漂移）。"""
    from src.runtime.service import _TASK_COLUMNS_LIST
    assert "pool_engine_source" in _TASK_COLUMNS_LIST, (
        "pool_engine_source 必须在 _TASK_COLUMNS_LIST 中——"
        "若移除该列需同步更新 _create_direct_task / _create_failed_task 的 _task_insert_row 调用"
    )


# ===========================================================================
# T8-01 / T8-03 / T8-04: _create_direct_task 成功落库
# ===========================================================================

def test_create_direct_task_inserts_row_with_pool_engine_source():
    """T8-01 + T8-03 + T8-04: _create_direct_task 成功落库。

    修复前：_task_insert_row 缺 pool_engine_source → ValueError → 返回空字符串。
    修复后：追加 pool_engine_source="" → INSERT 成功 → 返回非空 task_id。
    """
    from src.api import event_pipeline as ep

    seq = 9101
    tid = ep._create_direct_task(
        REPO, seq, "vod", "opencode", "vod",
        skill_path="skills/vod/SKILL.md", engine_id=0,
        evt={"issue_id": "#101"},
    )

    # T8-03: 返回非空 task_id
    assert tid, (
        "_create_direct_task 应返回非空 task_id——"
        "空字符串说明 _task_insert_row 抛 ValueError 被内部 try/except 吞掉"
    )
    assert tid.startswith("t_"), f"task_id 应以 t_ 开头，实际: {tid}"

    # T8-01: 落库行存在 + pool_engine_source 为空字符串
    row = _get_task_row(seq)
    assert row is not None, "agent_tasks 应有该事件行"
    assert row[0] == tid, f"DB 中 task_id 应与返回值一致: {row[0]} vs {tid}"
    assert row[3] == "", f"pool_engine_source 应为空字符串，实际: {row[3]!r}"

    # T8-04: status=queued + event_seq 正确
    assert row[1] == seq, f"event_seq 应为 {seq}，实际: {row[1]}"
    assert row[2] == "queued", f"status 应为 queued，实际: {row[2]}"


# ===========================================================================
# T8-02 / T8-05: _create_failed_task 成功落库
# ===========================================================================

def test_create_failed_task_inserts_failed_snapshot_with_pool_engine_source():
    """T8-02 + T8-05: _create_failed_task 成功落库。

    修复前：同 _create_direct_task，缺 pool_engine_source → ValueError → 落库失败。
    修复后：追加 pool_engine_source="" → INSERT 成功 → failed 快照落库。
    """
    from src.api import event_pipeline as ep

    seq = 9102
    ep._create_failed_task(
        REPO, seq, "vod", "opencode", "vod",
        skill_path="", skill_id=0,
        evt={"type": "issue_opened", "issue_id": "#102"},
        issue_id="#102", runtime_id=0,
    )

    # T8-02: 落库行存在 + pool_engine_source 为空字符串
    row = _get_task_row(seq)
    assert row is not None, "agent_tasks 应有 failed 快照行"
    assert row[3] == "", f"pool_engine_source 应为空字符串，实际: {row[3]!r}"

    # T8-05: status=failed + errors 非空
    assert row[1] == seq, f"event_seq 应为 {seq}，实际: {row[1]}"
    assert row[2] == "failed", f"status 应为 failed，实际: {row[2]}"
    assert row[4], "errors 应非空（含 engine_unavailable 原因）"
