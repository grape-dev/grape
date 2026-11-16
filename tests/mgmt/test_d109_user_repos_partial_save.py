"""user_repos 缓存的部分刷新守卫（2026-11-14 实锤：gitee 刷新把 gitcode/github 缓存抹掉）。

`save_user_repos` 原是「先删全部再写」；某个平台拉取失败（401/网络）时，它自己的缓存
会被其他平台的刷新连带删掉——用户明明有 81 个仓库，界面上只剩 7 个。

守卫：给定 providers 时只替换这些平台的行，其余平台原样保留；不给则保持全量覆盖语义。
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DDL = """
CREATE TABLE user_repos (
  user_id TEXT NOT NULL, repo_key TEXT NOT NULL, owner TEXT, name TEXT, description TEXT,
  role TEXT, is_admin INTEGER DEFAULT 0, is_join INTEGER DEFAULT 0, synced_at TEXT,
  created_at TEXT, PRIMARY KEY (user_id, repo_key)
)
"""


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()


def _setup(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "c.db")
    conn.execute(DDL)
    rows = [("33", "gitcode:a/1"), ("33", "gitcode:a/2"), ("33", "github:b/1"),
            ("33", "gitee:c/1"), ("33", "gitee:c/2")]
    conn.executemany("INSERT INTO user_repos (user_id, repo_key) VALUES (?,?)", rows)
    conn.commit()

    import src.domains.mgmt.user.db as udb

    _fake = _FakeDB(conn)
    monkeypatch.setattr(udb, "db", _fake, raising=False)
    # 2026-11-16 rebase 后写路径统一走 _mg_db 域代理（基线归位），必须一并替换
    monkeypatch.setattr(udb, "_mg_db", _fake, raising=False)
    monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=False)
    return conn, udb


def test_partial_save_keeps_other_platforms(tmp_path, monkeypatch):
    conn, udb = _setup(tmp_path, monkeypatch)
    # 只刷新 gitee：旧 2 条被替换，gitcode/github 3 条必须原样保留
    udb.save_user_repos("33", [{"repo_key": "gitee:c/1", "full_name": "c/1", "owner": "c", "name": "1"}],
                        providers=["gitee"])
    keys = {r[0] for r in conn.execute("SELECT repo_key FROM user_repos WHERE user_id='33'")}
    assert keys == {"gitcode:a/1", "gitcode:a/2", "github:b/1", "gitee:c/1"}, keys


def test_full_save_still_replaces_everything(tmp_path, monkeypatch):
    conn, udb = _setup(tmp_path, monkeypatch)
    udb.save_user_repos("33", [{"repo_key": "gitee:c/1"}])
    keys = {r[0] for r in conn.execute("SELECT repo_key FROM user_repos WHERE user_id='33'")}
    assert keys == {"gitee:c/1"}, keys

def test_underscore_provider_does_not_match_same_shape(tmp_path, monkeypatch):
    """P2-6：provider 名允许下划线，而 `_` 是 LIKE 单字符通配符。

    `LIKE 'a_b:%'` 会匹配 `aXb:…`（X 任意单字符）→ 误删别的平台缓存行。
    改为 substr 前缀相等后，只替换精确前缀。
    """
    conn, udb = _setup(tmp_path, monkeypatch)
    conn.executemany(
        "INSERT INTO user_repos (user_id, repo_key) VALUES (?,?)",
        [("33", "a_b:one/1"), ("33", "aXb:other/2")],
    )
    conn.commit()

    udb.save_user_repos("33", [{"repo_key": "a_b:one/1"}], providers=["a_b"])
    keys = {r[0] for r in conn.execute("SELECT repo_key FROM user_repos WHERE user_id='33'")}

    assert "a_b:one/1" in keys, "自身平台的行应被重写保留"
    assert "aXb:other/2" in keys, "同形平台（aXb）的行不得被 a_b 的前缀误删"
