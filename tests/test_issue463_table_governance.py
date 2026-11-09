"""Issue #463: agent_tasks 表体积治理测试（P0/P1/P2/P3）。

- P0: cleanup_old_agent_tasks_fields 清理 10 个字段、保留期 7 天
- P1: delete_old_agent_tasks 90 天终态分批整行删除（连带 task_messages）
- P2: enqueue_task URL 优先单写（有 OBS URL 的文件不再内联 MEDIUMTEXT）
- P3: agent_tasks 行数/体积指标 + 告警阈值规则
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

# 注意：session_manager 的 db/db_lock 一律在测试函数内导入（与 test_issue357_health.py 一致）——
# conftest 会话级 fixture 会重建 session_manager.db，模块级绑定会拿到失效连接（conftest.py 注释）。


def _align_db(*mod_names):
    """把指定模块的 db/db_lock 对齐到当前 session_manager 单例。

    混跑时其他测试模块（如 test_issue_337_list_handoff）会在收集期 reload(session_manager)
    重绑全局 db 且不恢复，收集期捕获引用的模块会与之分裂（写 A 库读 B 库）；
    先例见 test_issue_178_task_creator.py D43。
    """
    import importlib
    from src.session_manager import db as _sm_db, db_lock as _sm_lock
    for name in mod_names:
        mod = importlib.import_module(name)
        if hasattr(mod, "db"):
            mod.db = _sm_db
        if hasattr(mod, "db_lock"):
            mod.db_lock = _sm_lock

_OLD = "2020-01-01 00:00:00"
_ALL_FIELDS = {
    "prompt": "old prompt",
    "result_text": "old result",
    "skill_files": '{"sk": {"SKILL.md": "x"}}',
    "project_info_files": '{"project-graph.md": "BIG"}',
    "project_info_meta": '{"last_commit_sha": "abc"}',
    "knowledge_files": '{"agent/tips.md": "K"}',
    "env_extra": '{"GRAPE_X": "1"}',
    "errors": "err",
    "stderr": "std",
    "usage_json": '{"session_id": "s"}',
}
_COLS = ",".join(_ALL_FIELDS)
_PH = ",".join("?" * len(_ALL_FIELDS))


def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for tid in ("t463_old", "t463_recent", "t463_term_c", "t463_term_f", "t463_term_x",
                    "t463_running_old", "t463_recent_c"):
            c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
            c.execute("DELETE FROM task_messages WHERE task_id=?", (tid,))
        db.commit()


def _insert_task(tid, status, created_at, fields=None):
    from src.session_manager import db, db_lock
    vals = dict(_ALL_FIELDS)
    if fields is not None:
        vals.update(fields)
    with db_lock:
        c = db.cursor()
        c.execute(
            f"INSERT INTO agent_tasks (id, agent_session_id, runtime_id, status, created_at, {_COLS}) "
            f"VALUES (?,?,?,?,?,{_PH})",
            (tid, "sess-" + tid, 1, status, created_at, *[_ALL_FIELDS[k] for k in _ALL_FIELDS]),
        )
        db.commit()


def setup_function():
    _clean()


def teardown_function():
    _clean()


# ---------- P0 ----------

def test_p0_retention_is_7_days():
    from src.runtime.table_maintenance import _AGENT_TASKS_RETENTION_DAYS
    assert _AGENT_TASKS_RETENTION_DAYS == 7


def test_p0_cleanup_nullifies_all_10_fields_for_old_completed():
    from src.runtime.table_maintenance import cleanup_old_agent_tasks_fields
    _align_db("src.runtime.table_maintenance")
    _insert_task("t463_old", "completed", _OLD)
    from src.session_manager import db, db_lock
    updated = cleanup_old_agent_tasks_fields()
    assert updated >= 1
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT {_COLS} FROM agent_tasks WHERE id='t463_old'")
        row = dict(zip(_ALL_FIELDS, c.fetchone()))
    assert row["prompt"] == ""          # NOT NULL 列置空串
    assert row["result_text"] == ""
    for k in ("skill_files", "project_info_files", "project_info_meta", "knowledge_files",
              "env_extra", "errors", "stderr", "usage_json"):
        assert row[k] is None, f"字段 {k} 应被置 NULL，实际 {row[k]!r}"


def test_p0_cleanup_keeps_recent_completed_and_running():
    from src.runtime.table_maintenance import cleanup_old_agent_tasks_fields
    _align_db("src.runtime.table_maintenance")
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _insert_task("t463_recent", "completed", recent)
    _insert_task("t463_running_old", "running", _OLD)
    from src.session_manager import db, db_lock
    cleanup_old_agent_tasks_fields()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT result_text, skill_files FROM agent_tasks WHERE id='t463_recent'")
        assert c.fetchone() == ("old result", '{"sk": {"SKILL.md": "x"}}')
        c.execute("SELECT result_text FROM agent_tasks WHERE id='t463_running_old'")
        assert c.fetchone()[0] == "old result"


# ---------- P1 ----------

def test_p1_delete_old_terminal_tasks_and_messages():
    from src.runtime.table_maintenance import delete_old_agent_tasks
    from src.session_manager import db, db_lock
    _align_db("src.runtime.table_maintenance")
    for tid, st in (("t463_term_c", "completed"), ("t463_term_f", "failed"),
                    ("t463_term_x", "cancelled")):
        _insert_task(tid, st, _OLD)
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO task_messages (task_id, seq, type, content) VALUES (?,?,?,?)",
                      (tid, 1, "text", "m"))
            db.commit()
    # 保留：90 天内的终态 / 90 天前的非终态
    recent = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _insert_task("t463_recent_c", "completed", recent)
    _insert_task("t463_running_old", "running", _OLD)

    deleted = delete_old_agent_tasks()
    assert deleted >= 3
    with db_lock:
        c = db.cursor()
        for tid in ("t463_term_c", "t463_term_f", "t463_term_x"):
            c.execute("SELECT COUNT(*) FROM agent_tasks WHERE id=?", (tid,))
            assert c.fetchone()[0] == 0
            c.execute("SELECT COUNT(*) FROM task_messages WHERE task_id=?", (tid,))
            assert c.fetchone()[0] == 0, f"{tid} 的 task_messages 应连带删除"
        c.execute("SELECT COUNT(*) FROM agent_tasks WHERE id IN ('t463_recent_c','t463_running_old')")
        assert c.fetchone()[0] == 2


# ---------- P2 ----------

class _FakeStorage:
    def generate_file_urls(self, filtered_files):
        return {rel: {"url": "https://obs.example.com/" + rel, "storage_key": "knowledge/" + rel}
                for rel in filtered_files if rel == "project-graph.md"}


def test_p2_enqueue_url_wins_over_inline_content():
    from src.runtime.service import enqueue_task
    _align_db("src.runtime.service")
    collected = {
        "project_info": {"project-graph.md": {"content": "BIG"}, "member/extra.md": {"content": "M"}},
        "knowledge": {"agent/tips.md": {"content": "K"}},
        "meta": {"last_commit_sha": "abc"},
    }
    with patch("src.storage.get_storage", return_value=_FakeStorage()), \
         patch("src.runtime.service.collect_knowledge_files", return_value=collected):
        tid = enqueue_task(
            agent_session_id="sess-463", runtime_id=1, prompt="p",
            repo_key="gitcode:org/repo463", agent_type="coding",
            project_info_files={"project-graph.md": "BIG", "member/extra.md": "M"},
            project_info_meta={"last_commit_sha": "abc"},
            knowledge_files={"agent/tips.md": "K"},
        )
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT project_info_files, knowledge_files, project_info_file_urls, "
                      "knowledge_file_urls FROM agent_tasks WHERE id=?", (tid,))
            pif, kf, pifu, kfu = c.fetchone()
        # 有 URL 的键内联内容置空（URL 优先单写）；无 URL 的键保留内联兜底
        assert json.loads(pif)["project-graph.md"] == ""
        assert json.loads(pif)["member/extra.md"] == "M"
        assert json.loads(kf)["agent/tips.md"] == "K"
        assert "project-graph.md" in json.loads(pifu)
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
            db.commit()


# ---------- P3 ----------

def test_p3_seed_rules_cover_agent_tasks_metrics():
    from src.observability import alerts as al
    from src.session_manager import db, db_lock
    al.seed_rules()
    with db_lock:
        c = db.cursor()
        c.execute("SELECT metric, threshold, level FROM obs_alert_rules "
                  "WHERE metric IN ('agent_tasks_rows','agent_tasks_mb')")
        rules = {r[0]: (r[1], r[2]) for r in c.fetchall()}
    assert "agent_tasks_rows" in rules and rules["agent_tasks_rows"][1] == "platform"
    assert rules["agent_tasks_rows"][0] > 0
    assert "agent_tasks_mb" in rules and rules["agent_tasks_mb"][0] > 0


def test_p3_platform_metrics_expose_agent_tasks_rows():
    from src.observability import alerts as al
    vals = al._platform_metrics()
    assert "agent_tasks_rows" in vals
    assert vals["agent_tasks_rows"] >= 0
