"""Issue #4066：存量 custom_fields 清理迁移（202611240001）。

契约：
1. `autopilot_tasks.custom_fields` 与 `devops_pipeline_scene.advanced_options`
   的非法条目（空名 / 非大写 / 仍保留）被剔除；
2. 合法条目（含白名单 `GRAPE_*`）保留，场景其它 advanced_options 键不动；
3. 全部剔除时 custom_fields 落空串（与写侧空值口径一致）；
4. 幂等（两跑结果一致，无非法项时不做任何写）；
5. `post_check` 残留非法项则抛错（不盖章），表缺失视为收敛。
"""
import json
import sqlite3

import pytest

from src.domains.mgmt.migrations.autopilot_custom_fields_cleanup import (
    cleanup_invalid_custom_fields,
    verify_custom_fields_clean,
)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
    CREATE TABLE autopilot_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        custom_fields TEXT DEFAULT ''
    );
    CREATE TABLE devops_pipeline_scene (
        scene_id TEXT,
        repo_key TEXT,
        advanced_options TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1
    );
    """)
    c.commit()
    return c


def _task_fields(conn, task_id):
    row = conn.execute("SELECT custom_fields FROM autopilot_tasks WHERE id=?", (task_id,)).fetchone()
    return json.loads(row[0] or "[]")


def _scene_opts(conn, scene_id):
    row = conn.execute("SELECT advanced_options FROM devops_pipeline_scene WHERE scene_id=?",
                       (scene_id,)).fetchone()
    return json.loads(row[0] or "{}")


def test_autopilot_invalid_entries_removed_valid_kept(conn):
    stale = [
        {"name": "TEAM_FLAG", "value": "keep"},
        {"name": "GRAPE_ORG_ID", "value": "42"},        # 白名单：合法
        {"name": "GRAPE_API_TOKEN", "value": "leak"},
        {"name": "team_lower", "value": "x"},
        {"name": "", "value": "empty"},
        {"value": "no-name"},
    ]
    conn.execute("INSERT INTO autopilot_tasks (id, name, custom_fields) VALUES (1, 't', ?)",
                 (json.dumps(stale, ensure_ascii=False),))
    conn.commit()

    cleanup_invalid_custom_fields(conn=conn)

    assert _task_fields(conn, 1) == [
        {"name": "TEAM_FLAG", "value": "keep"},
        {"name": "GRAPE_ORG_ID", "value": "42"},
    ]


def test_autopilot_all_invalid_becomes_empty_string(conn):
    conn.execute("INSERT INTO autopilot_tasks (id, name, custom_fields) VALUES (2, 't', ?)",
                 (json.dumps([{"name": "GRAPE_SESSION_TOKEN", "value": "tk"}]),))
    conn.commit()

    cleanup_invalid_custom_fields(conn=conn)

    row = conn.execute("SELECT custom_fields FROM autopilot_tasks WHERE id=2").fetchone()
    assert row[0] == "", "全部剔除后应写空串（与写侧空值口径一致）"


def test_scene_advanced_options_pruned_other_keys_kept(conn):
    opts = {
        "work_branch": "dev",
        "custom_fields": [
            {"name": "OK_FLAG", "value": "1"},
            {"name": "GRAPE_SESSION_TOKEN", "value": "tk"},
            {"name": "bad_lower", "value": "x"},
            {"name": "GRAPE_ANALYSIS_MODE", "value": "all"},   # 白名单：合法
        ],
    }
    conn.execute(
        "INSERT INTO devops_pipeline_scene (scene_id, repo_key, advanced_options) VALUES ('s1', 'r', ?)",
        (json.dumps(opts, ensure_ascii=False),))
    conn.commit()

    cleanup_invalid_custom_fields(conn=conn)

    cleaned = _scene_opts(conn, "s1")
    assert cleaned["work_branch"] == "dev"
    assert cleaned["custom_fields"] == [
        {"name": "OK_FLAG", "value": "1"},
        {"name": "GRAPE_ANALYSIS_MODE", "value": "all"},
    ]


def test_idempotent_second_run_no_change(conn):
    fields = [{"name": "TEAM_FLAG", "value": "1"}]
    conn.execute("INSERT INTO autopilot_tasks (id, name, custom_fields) VALUES (3, 't', ?)",
                 (json.dumps(fields),))
    conn.commit()

    cleanup_invalid_custom_fields(conn=conn)
    first = _task_fields(conn, 3)
    cleanup_invalid_custom_fields(conn=conn)
    assert _task_fields(conn, 3) == first == fields
    verify_custom_fields_clean(conn=conn)


def test_post_check_passes_when_clean_and_raises_on_residue(conn):
    conn.execute("INSERT INTO autopilot_tasks (id, name, custom_fields) VALUES (4, 't', ?)",
                 (json.dumps([{"name": "GRAPE_API_TOKEN", "value": "x"}]),))
    conn.commit()

    with pytest.raises(RuntimeError):
        verify_custom_fields_clean(conn=conn)

    cleanup_invalid_custom_fields(conn=conn)
    verify_custom_fields_clean(conn=conn)   # 不抛 = 收敛


def test_missing_tables_are_skipped():
    c = sqlite3.connect(":memory:")
    cleanup_invalid_custom_fields(conn=c)
    verify_custom_fields_clean(conn=c)     # 表不存在视为收敛，不阻断启动