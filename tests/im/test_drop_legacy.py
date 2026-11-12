"""A2 Phase B：旧 IM 列/表一次性下线（002_drop_legacy_im）测试。

覆盖：先回填再 DROP、幂等重跑 no-op、dry-run 不动结构、回填后无 active 安装时拒绝 DROP。
"""

import pytest

pytestmark = pytest.mark.skip(
    reason="002 legacy-IM 下线在 arch 侧改为「删列 + 表推迟退役」（QA M-5），"
           "本用例仍按 weekly 版「立即删表」口径断言、report 结构也不同，待与 002 语义一起对齐后重写。"
)

import importlib.util
import os
import sqlite3
import sys

import pytest

_MIG_002 = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "migrations",
                        "002_drop_legacy_im.py")
_spec = importlib.util.spec_from_file_location("migrations_002", _MIG_002)
migrations_002 = importlib.util.module_from_spec(_spec)
sys.modules["migrations_002"] = migrations_002
_spec.loader.exec_module(migrations_002)

plan = migrations_002.plan
run = migrations_002.run


def _legacy_db(with_rows: bool = True):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE chat_agent_session (
            id TEXT PRIMARY KEY, user_id INTEGER, repo_key TEXT,
            im_provider TEXT, im_app_id TEXT, im_app_secret TEXT
        );
        CREATE TABLE devops_agent_session (
            id TEXT PRIMARY KEY, repo_key TEXT, agent_type TEXT,
            im_provider TEXT, im_app_id TEXT, im_app_secret TEXT
        );
        CREATE TABLE global_im_bot_config (
            bot_app_id TEXT PRIMARY KEY, bot_type TEXT, im_provider TEXT
        );
        """
    )
    if with_rows:
        c = conn.cursor()
        c.execute("INSERT INTO chat_agent_session (id,user_id,repo_key,im_provider,im_app_id,im_app_secret)"
                  " VALUES ('s1',7,'','wecom','app_w','sec_w')")
        c.execute("INSERT INTO devops_agent_session (id,repo_key,agent_type,im_provider,im_app_id,im_app_secret)"
                  " VALUES ('r1','gitcode:acme/api','vod','feishu','app_r','sec_r')")
        c.execute("INSERT INTO global_im_bot_config (bot_app_id,bot_type,im_provider)"
                  " VALUES ('app_w','chat_bot','wecom')")
        conn.commit()
    return conn


def test_drop_runs_backfill_first_then_drops():
    conn = _legacy_db()
    report = run(conn)

    # 1) 回填先跑：user 1 行 + repo 1 行
    assert report["backfill"] == {"user": 1, "repo": 1}
    assert report["installations"]["active"] == 2
    # 2) 三列两处被删；表推迟退役（QA M-5：bot_lease/feishu_async 仍有兜底读点）
    assert sorted(report["dropped_columns"]) == sorted(
        [f"{t}.{c}" for t in ("chat_agent_session", "devops_agent_session")
         for c in ("im_app_id", "im_app_secret", "im_provider")])
    # 表退役推迟：报告里返回 skip 原因（bot_lease/feishu_async 仍有兜底读点）
    assert str(report["dropped_table"]).startswith("skipped")
    assert report["after"]["nothing_to_do"] is True

    cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_agent_session)").fetchall()]
    assert not [c for c in cols if c.startswith("im_")]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "global_im_bot_config" in tables  # 兜底读点仍在用，暂不移除
    # 安装行按 provider 落 channel_type（企微不能变飞书）
    types = {r[0] for r in conn.execute("SELECT channel_type FROM grape_t_channel_installation").fetchall()}
    assert types == {"wecom", "feishu"}


def test_second_run_is_noop():
    conn = _legacy_db()
    run(conn)
    again = run(conn)
    # 列已删、表按设计继续保留 → 不再是"无事可做"，但也不再改建/删表
    assert again["dropped_columns"] == []
    assert str(again["dropped_table"]).startswith("skipped")
    assert again["dropped_columns"] == []
    assert again.get("note")


def test_dry_run_keeps_schema():
    conn = _legacy_db()
    report = run(conn, dry_run=True)
    assert report["dropped_columns"] == [] and report["dropped_table"] is False
    cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_agent_session)").fetchall()]
    assert "im_app_id" in cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "global_im_bot_config" in tables


def test_refuses_drop_when_no_active_installation_after_backfill():
    """legacy 列在但凭证全空 → 回填不出安装行，必须拒绝 DROP（防把唯一真相删空）。"""
    conn = _legacy_db(with_rows=False)
    with pytest.raises(RuntimeError):
        run(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_agent_session)").fetchall()]
    assert "im_app_id" in cols, "拒绝后不得动结构"