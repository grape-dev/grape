"""关系变更后的"项目列表刷新"行为守卫（P1-3，2026-11-14 补证后修复）。

缺陷：`hooks._kick_user_projects_refresh` 内部写死 `invalidate_user_repos(int(user_id))`，
而 grape_accounts 主键自 D105 起是 UUIDv7 字符串 → `int("01a1d…")` 必抛 ValueError，
被 `_do()` 的 except 吞成一条 warning。结果：OAuth 登录/绑定/PAT 变更后
`user_repos.synced_at` 从不置空，仓库列表要等 30 分钟 TTL 才更新。

守卫要求（用户明确）：**不 mock `_kick_user_projects_refresh`**、用真实 uuid7 id
走完整 hooks 入口，断言 `user_repos.synced_at` 被置 NULL。

原用例 `test_login_kicks_projects_refresh` monkeypatch 了该函数本身，只断言"被调用"，
函数体从未执行——本文件补上这一层。
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL  # noqa: E402

REPO_SQL = "gitee:yang-aiwu-new/livefire-tpl-verify-1010"


class _SqliteDB:
    """把 sqlite 连接包成 user/db.py 需要的 db 接口（cursor/commit）。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()


class _Passthrough:
    def __init__(self, session):
        self._s = session

    def __enter__(self):
        return self._s

    def __exit__(self, *a):
        return False


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """平台对象表（关系行）+ user_repos 缓存表，同一进程内可写。"""
    engine = create_engine("sqlite://")
    raw = engine.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.commit()
    s = Session(engine)
    s.execute(text(
        "INSERT INTO grape_t_repo_platforms (id,provider,name,enabled) VALUES "
        "('p1','gitee','Gitee',1),('p2','gitcode','GitCode',1)"
    ))
    s.execute(text(
        "INSERT INTO grape_t_channel_repo_link (id,oauth_provider,repo_provider,source) VALUES "
        "('l1','gitee','gitee','derived'),('l2','gitcode','gitcode','derived')"
    ))
    s.commit()

    # 刷新在 daemon 线程里执行 → 连接必须允许跨线程（SQLite 默认禁止，
    # 否则异常会被 _do() 的 except 吞成 warning，测试假绿）
    conn = sqlite3.connect(tmp_path / "user_repos.db", check_same_thread=False)
    conn.execute(
        "CREATE TABLE user_repos (user_id TEXT NOT NULL, repo_key TEXT NOT NULL,"
        " owner TEXT, name TEXT, description TEXT, role TEXT, is_admin INTEGER DEFAULT 0,"
        " is_join INTEGER DEFAULT 0, synced_at TEXT, created_at TEXT, PRIMARY KEY (user_id, repo_key))"
    )
    conn.commit()

    import src.domains.mgmt.user.db as udb

    # 2026-11-16 rebase 后该域一律走 _mg_db 域代理（基线归位），只 patch db 拦不住写入
    _fake = _SqliteDB(conn)
    monkeypatch.setattr(udb, "db", _fake, raising=False)
    monkeypatch.setattr(udb, "_mg_db", _fake, raising=False)
    monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=False)
    monkeypatch.setattr("src.kernel.persistence.uow.uow", lambda: _Passthrough(s))

    from src.domains.platform import hooks

    monkeypatch.setattr(hooks, "_ENABLED", True, raising=False)
    yield conn, s, hooks
    s.close()
    engine.dispose()
    conn.close()


def _seed_cached_repos(conn, uid: str) -> None:
    conn.execute(
        "INSERT INTO user_repos (user_id, repo_key, synced_at) VALUES (?,?,?)",
        (uid, REPO_SQL, "2026-11-14 12:00:00"),
    )
    conn.commit()


def _synced_at(conn, uid: str):
    return conn.execute(
        "SELECT synced_at FROM user_repos WHERE user_id=?", (uid,)
    ).fetchone()[0]


def _drain_refresh_threads(timeout: float = 5.0) -> None:
    for t in list(threading.enumerate()):
        if t.name.startswith("d109-projects-"):
            t.join(timeout)


def test_uuid_user_pat_saved_invalidates_projects_cache(env):
    """真实 uuid7 id（不 mock 刷新函数）：on_pat_saved 后 synced_at 必须被置 NULL。"""
    from src.kernel.id_gen import uuid7_str  # 与 grape_account_db 同源

    conn, _s, hooks = env
    uid = str(uuid7_str())
    assert "-" in uid and not uid.isdigit(), "必须是 UUIDv7 形态，否则本测试失去意义"
    _seed_cached_repos(conn, uid)
    assert _synced_at(conn, uid) is not None

    hooks.on_pat_saved(uid, "gitee")
    _drain_refresh_threads()

    assert _synced_at(conn, uid) is None, (
        "关系变更后 user_repos 未标记过期 → 项目列表要等 TTL 才更新"
        "（int(uuid) 抛 ValueError 被吞掉时的表现）"
    )


def test_uuid_user_oauth_login_invalidates_projects_cache(env):
    from src.kernel.id_gen import uuid7_str

    conn, _s, hooks = env
    uid = str(uuid7_str())
    _seed_cached_repos(conn, uid)

    hooks.on_oauth_login(uid, "gitee")
    _drain_refresh_threads()

    assert _synced_at(conn, uid) is None


def test_legacy_numeric_string_id_still_works(env):
    """存量整型 id 字符串化（如 '33'）不能被这次修复破坏。"""
    conn, _s, hooks = env
    uid = "33"
    _seed_cached_repos(conn, uid)

    hooks.on_pat_saved(uid, "gitee")
    _drain_refresh_threads()

    assert _synced_at(conn, uid) is None