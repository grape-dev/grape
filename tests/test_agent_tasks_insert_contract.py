"""看护 agent_tasks 直连创建 INSERT 契约（防 39 列 / 37 值漂移事故）。

   事故：docs/new-version3/agent-tasks-direct-create-binding-fix-design.md
   _create_direct_task / _create_failed_task 曾因缺失 pool_tag/pool_engine_ids
   值导致 INSERT 绑定数不匹配，事件卡 running 被 sweeper 置 failed（Seq #1991/#1992）。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock

REPO = "gitcode:owner/test-agenttasks-insert"


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


def _row(seq):
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT id, event_seq, status, runtime_id, skill, pool_tag, pool_engine_ids "
            "FROM agent_tasks WHERE repo_key=? AND event_seq=?",
            (REPO, seq),
        ).fetchone()


# ---------- 契约（纯逻辑，无 DB） ----------

from src.api import event_pipeline as ep  # noqa: E402
from src.runtime.service import _TASK_COLUMNS_LIST  # noqa: E402


def test_task_insert_row_fully_covers_columns():
    """_task_insert_row 组装的值数恒等于列清单列数（防 39/37 漂移）。"""
    kw = {c: (0 if c in ("skill_id", "runtime_id", "user_id", "attempts",
                         "affinity_runtime_id", "event_seq") else "")
          for c in _TASK_COLUMNS_LIST}
    row = ep._task_insert_row(_TASK_COLUMNS_LIST, **kw)
    assert len(row) == len(_TASK_COLUMNS_LIST)
    # 关键列必须被显式覆盖（历史缺失列）
    for key in ("pool_tag", "pool_engine_ids", "id", "status"):
        assert key in kw and (kw[key] is not None), f"列 {key} 必须提供占位"


def test_task_insert_row_missing_column_raises():
    """缺任一词条应抛 ValueError（不允许静默少绑定）。"""
    kw = {c: "" for c in _TASK_COLUMNS_LIST}
    kw.pop("pool_engine_ids")
    import pytest

    with pytest.raises(ValueError):
        ep._task_insert_row(_TASK_COLUMNS_LIST, **kw)


# ---------- DB 回归：direct / failed 创建 ----------

def test_create_direct_task_inserts_row():
    """直连创建成功：agent_tasks 落一行 queued，pool_tag/pool_engine_ids 存在。"""
    seq = 9001
    tid = ep._create_direct_task(REPO, seq, "vod", "opencode", "vod",
                                 skill_path="skills/vod/SKILL.md", engine_id=0,
                                 evt={"issue_id": "#1"})
    assert tid, "create_direct_task 应返回 task_id（不允许 bindings 异常）"
    row = _row(seq)
    assert row, "agent_tasks 应有该事件行"
    assert row[1] == seq and row[2] == "queued"
    assert row[4] == "vod"
    assert row[5] == "" and row[6] == ""  # pool_tag / pool_engine_ids 占位非 None


def test_create_failed_task_inserts_failed_snapshot():
    """engine_unavailable 场景：落 failed 快照，重派可复用。"""
    seq = 9002
    ep._create_failed_task(REPO, seq, "vod", "opencode", "vod", skill_path="",
                           skill_id=0, evt={"type": "issue_opened", "issue_id": "#2"},
                           issue_id="#2", runtime_id=0)
    row = _row(seq)
    assert row, "agent_tasks 应有 failed 快照行"
    assert row[1] == seq and row[2] == "failed"
    with db_lock:
        c = db.cursor()
        errs = c.execute("SELECT errors FROM agent_tasks WHERE repo_key=? AND event_seq=?",
                         (REPO, seq)).fetchone()[0]
    assert "engine_unavailable" in (errs or "")


def test_create_direct_task_no_bindings_error_in_db():
    """关键回归：直接对库执行 INSERT 不抛绑定数异常（旧 39/37 会抛）。"""
    from datetime import datetime, timezone

    seq = 9003
    now = datetime.now(timezone.utc).isoformat()
    # 由列清单驱动构造（避免手抄清单再次漂移——2026-10-19 缺 pool_engine_source 就是手抄漏列）
    defaults = {"skill_id": 0, "runtime_id": 0, "user_id": 0, "attempts": 0,
                "affinity_runtime_id": 0, "event_seq": seq, "exit_code": None,
                "completed_at": None}
    kw = {c: defaults.get(c, "") for c in _TASK_COLUMNS_LIST}
    kw.update({
        "id": "t_ct_contract", "agent_session_id": "s_any",
        "issue_id": "#3", "repo_key": REPO,
        "agent_type": "vod", "skill": "vod",
        "event_type": "issue_opened",
        "account": "owner/test-agenttasks-insert",
        "env_extra": "{}", "skill_files": "{}",
        "project_info_files": "{}", "project_info_meta": "{}", "knowledge_files": "{}",
        "project_info_file_urls": "{}", "knowledge_file_urls": "{}",
        "pool_agent_type": "", "pool_tag": "", "pool_engine_ids": "", "pool_engine_source": "",
        "status": "queued", "result_text": "", "errors": "", "stderr": "",
        "usage_json": "{}", "started_at": now, "created_at": now,
    })
    row = ep._task_insert_row(_TASK_COLUMNS_LIST, **kw)
    assert len(row) == len(_TASK_COLUMNS_LIST), "列数必须一致"
    with db_lock:
        c = db.cursor()
        c.execute(
            f"INSERT INTO agent_tasks ({','.join(_TASK_COLUMNS_LIST)}) VALUES "
            f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})", row)
        db.commit()
    assert _row(seq) is not None

# ---------- 静态防复发：INSERT 调用点必须覆盖全部列 ----------

def test_insert_call_sites_cover_all_columns_statically():
    """两个 INSERT 调用点的源码必须显式覆盖 _TASK_COLUMNS_LIST 的每一列。

    2026-10-19 事故：_TASK_COLUMNS_LIST 新增 `pool_engine_source`（local-claimer 设计）时，
    `_create_direct_task` / `_create_failed_task` 未同步该列 → `_task_insert_row` 抛
    ValueError 被 except 吞掉 → 任务行/失败快照都不落库（排障断链）。
    本用例是**静态守卫**：以后任何新列进清单，只要这两个调用点没跟上，立刻红。
    """
    import inspect
    import re as _re

    def _missing(fn):
        src = inspect.getsource(fn)
        return [c for c in _TASK_COLUMNS_LIST
                if not _re.search(rf"(^|[\s,(]){_re.escape(c)}\s*=", src, _re.M)]

    assert not _missing(ep._create_direct_task), (
        f"_create_direct_task 未覆盖列: {_missing(ep._create_direct_task)}")
    assert not _missing(ep._create_failed_task), (
        f"_create_failed_task 未覆盖列: {_missing(ep._create_failed_task)}")


def test_failed_snapshot_records_pool_engine_source_column():
    """失败快照必须能写 pool_engine_source（本次事故的直接回归）。"""
    seq = 9004
    ep._create_failed_task(REPO, seq, "sre", "opencode", "sre", skill_path="", skill_id=0,
                           evt={"type": "issue_opened", "issue_id": "#4"}, issue_id="#4", runtime_id=0)
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT status, pool_engine_source FROM agent_tasks WHERE repo_key=? AND event_seq=?",
            (REPO, seq)).fetchone()
    assert row, "失败快照应落库（缺列时会整条丢弃）"
    assert row[0] == "failed" and (row[1] or "") == ""
