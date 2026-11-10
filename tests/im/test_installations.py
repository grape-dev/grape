"""安装枚举测试（Issue #760 P1——§5.3 第 2 步切换）。

SqliteInstallationProvider：grape_t_channel_installation 活跃行枚举（status 过滤）。
（A2 Phase B 已删 reconcile_installations：legacy im_* 列 DROP 后无对账对象。）
"""
import asyncio
import importlib.util
import os
import sqlite3
import sys

import pytest

from src.im.engine.installations import SqliteInstallationProvider

_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "migrations", "001_channel_tables.py")
_spec = importlib.util.spec_from_file_location("migrations_001", _MIG)
migrations_001 = importlib.util.module_from_spec(_spec)
sys.modules["migrations_001"] = migrations_001
_spec.loader.exec_module(migrations_001)
ensure_channel_tables = migrations_001.ensure_channel_tables
backfill_installations = migrations_001.backfill_installations


def _legacy_tables(conn):
    conn.executescript(
        """
        CREATE TABLE chat_agent_session (
            id INTEGER PRIMARY KEY, user_id INTEGER, repo_key TEXT,
            im_app_id TEXT, im_app_secret TEXT, im_provider TEXT
        );
        CREATE TABLE devops_agent_session (
            id INTEGER PRIMARY KEY, repo_key TEXT, agent_type TEXT,
            im_app_id TEXT, im_app_secret TEXT, im_provider TEXT
        );
        """
    )


def _seed(conn):
    c = conn.cursor()
    # user 轨：两个用户共享 app_u1（去重语义）+ 独立 app_u2
    c.execute("INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) VALUES (1, '', 'app_u1', 's1')")
    c.execute("INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) VALUES (2, '', 'app_u1', 's1')")
    c.execute("INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) VALUES (3, '', 'app_u2', 's2')")
    # repo 轨：app_r1 跨两个仓库共享
    c.execute("INSERT INTO devops_agent_session (repo_key, agent_type, im_app_id, im_app_secret) VALUES ('a/b', 'vod', 'app_r1', 's3')")
    c.execute("INSERT INTO devops_agent_session (repo_key, agent_type, im_app_id, im_app_secret) VALUES ('c/d', 'vod', 'app_r1', 's3')")
    conn.commit()


def _db_path(tmp_path):
    path = tmp_path / "grape.db"
    conn = sqlite3.connect(path)
    _legacy_tables(conn)
    ensure_channel_tables(conn)
    _seed(conn)
    backfill_installations(conn)
    conn.close()
    return path


def _open(path):
    conn = sqlite3.connect(path)
    conn.row_factory = None
    return conn


async def _provider_enumerates_impl(tmp_path):
    """P1 枚举：user 2 条（app 去重）+ repo 2 条（app 共享）= 4 活跃安装。"""
    provider = SqliteInstallationProvider(lambda: _open(_db_path(tmp_path)))
    rows = await provider()
    assert len(rows) == 4
    scopes = sorted(r["scope"] for r in rows)
    assert scopes == ["repo", "repo", "user", "user"]
    for r in rows:
        assert r["status"] == "active"
        assert r["channel_type"] == "feishu"
        assert isinstance(r["config"], dict) and r["config"].get("app_id")


async def _provider_filters_impl(tmp_path):
    """status != active（revoked/disabled）的安装不进入枚举。"""
    path = _db_path(tmp_path)
    conn = _open(path)
    conn.execute("UPDATE grape_t_channel_installation SET status='revoked' WHERE scope='user'")
    conn.commit()
    conn.close()
    provider = SqliteInstallationProvider(lambda: _open(path))
    rows = await provider()
    assert all(r["scope"] == "repo" for r in rows)
    assert len(rows) == 2


def test_provider_enumerates_active_installations(tmp_path):
    asyncio.run(_provider_enumerates_impl(tmp_path))


def test_provider_filters_non_active(tmp_path):
    asyncio.run(_provider_filters_impl(tmp_path))
