"""Issue #1495 回归测试：task_messages 活跃度判定从字节比较改为 epoch 口径。

病根（Issue #1495 / 对齐 health_scorer.py 两次实测踩坑）：
- `SELECT 1 FROM task_messages WHERE task_id=? AND created_at > ?` 是 TEXT 列逐字节比较，
  cutoff 由 Python isoformat() 生成（T 分隔、带微秒、+00:00 后缀）。
  1. 表默认值行（SQLite datetime('now') / MySQL now() → 'YYYY-MM-DD HH:MM:SS' 空格分隔）：
     同日内第 11 字节 ' '(0x20) < 'T'(0x54) → 恒早于 cutoff → 活跃任务被误判死任务
     （误杀旧进程 + attempts+1 → 三次后强制 failed）。
  2. 无微秒 ISO 行（'...T08:00:00+00:00'）：与同秒 cutoff（'...T08:00:00.123456+00:00'）
     比较时第 19 字节 '+'(0x2B) < '.'(0x2E) → 同秒窗口塌陷为不活跃。
- 修复：SQL 仅按 task_id（idx_task_messages_task）取 created_at，Python 侧宽容解析为
  epoch（复用 health.parse_dt 双后端口径）后与 now-window 比较；解析失败行不计活跃
  （保守：宁可误回收也不留死任务）；窗口常量 600s/1200s 单点收敛到 task_activity.py。

用例矩阵（AC#1，两调用点 × {空格格式, 无微秒 ISO}）：
- recover_orphans：空格格式 → 不判孤儿；无微秒 ISO → 不判孤儿；不可解析行 → 不计活跃（真孤儿仍回炉）
- claim 兜底：空格格式 → 不回收；无微秒 ISO → 不回收；真死任务（仅窗口外消息）→ 仍回收（AC#4 兜底）
"""
import datetime as dt
import sqlite3
import threading
import time as _time

import pytest

ROOT = None  # pytest.ini pythonpath=src；conftest 已引导 ROOT


@pytest.fixture()
def setup_db(monkeypatch):
    """内存 SQLite 双表最小环境：替换 svc/rt/session_manager 的 db/db_lock。"""
    from src.runtime import service as svc
    import src.api.routes.runtime as rt
    # event_store 必须同批替换：claim 回收的 force-fail 分支与 recover-orphans 判死
    # 回写都会函数内 from src.api.event_store import update_event_complete——该模块
    # 全局 db 是首次 import 时绑定的连接对象，若不替换会在真测试库上回写（组合运行
    # 时还会因模块级绑定与 monkeypatch undo 时序产生"closed database"串扰）。
    import src.api.event_store as _es

    test_db = sqlite3.connect(":memory:", check_same_thread=False)
    test_db.executescript("""
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY, agent_session_id TEXT DEFAULT '', runtime_id INTEGER DEFAULT 0,
        event_seq INTEGER, issue_id TEXT DEFAULT '', repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT '', skill TEXT DEFAULT '', skill_id INTEGER DEFAULT 0,
        prompt TEXT DEFAULT '', cwd TEXT DEFAULT '', runtime_session_id TEXT DEFAULT '',
        event_type TEXT DEFAULT '', account TEXT DEFAULT '', user_id INTEGER DEFAULT 0,
        repo_gitcode_token TEXT DEFAULT '', env_extra TEXT DEFAULT '{}',
        skill_files TEXT DEFAULT '{}', server_url TEXT DEFAULT '',
        issue_session_id TEXT DEFAULT '',
        attempts INTEGER DEFAULT 0, pool_agent_type TEXT DEFAULT '',
        affinity_runtime_id INTEGER DEFAULT 0, pool_tag TEXT DEFAULT '',
        pool_engine_ids TEXT DEFAULT '',
        status TEXT DEFAULT 'queued', stream_ended INTEGER DEFAULT 0,
        exit_code INTEGER, result_text TEXT DEFAULT '', errors TEXT DEFAULT '',
        started_at TEXT, completed_at TEXT, created_at TEXT
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0, busy_slots INTEGER DEFAULT 0, max_concurrent INTEGER DEFAULT 4
    );
    CREATE TABLE task_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, created_at TEXT, content TEXT
    );
    """)
    monkeypatch.setattr(svc, "db", test_db)
    monkeypatch.setattr(svc, "db_lock", threading.RLock())
    monkeypatch.setattr(rt, "db", test_db)
    monkeypatch.setattr(rt, "db_lock", svc.db_lock)
    import src.session_manager as _sm
    monkeypatch.setattr(_sm, "db", test_db)
    monkeypatch.setattr(_sm, "db_lock", svc.db_lock)
    monkeypatch.setattr(_es, "db", test_db)
    monkeypatch.setattr(_es, "db_lock", svc.db_lock)
    yield test_db
    test_db.close()


def _seed_task(c, tid, *, runtime_id=501, status="running", attempts=0,
               started_seconds_ago=700, agent_type="coding"):
    """插入 dispatched/running 任务；started_at 默认 700s 前（超过 claim 600s 兜底窗口与 recover 300s 宽限期）。"""
    now = dt.datetime.now(dt.timezone.utc)
    started = (now - dt.timedelta(seconds=started_seconds_ago)).isoformat()
    created = (now - dt.timedelta(seconds=started_seconds_ago + 10)).isoformat()
    c.execute(
        "INSERT INTO agent_tasks (id, status, runtime_id, attempts, agent_type, "
        "pool_agent_type, agent_session_id, created_at, started_at) "
        "VALUES (?,?,?,?,?,'',?,?,?)",
        (tid, status, runtime_id, attempts, agent_type, "s-" + tid, created, started))


def _insert_message_raw(c, tid, created_at_raw, content="alive"):
    """按字面原样写入 created_at（不归一化——被测对象正是异形格式行）。"""
    c.execute("INSERT INTO task_messages (task_id, created_at, content) VALUES (?,?,?)",
              (tid, created_at_raw, content))


def _fmt_utc(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


# ===== recover_orphans 调用点（src/api/routes/runtime.py）=====

class TestRecoverOrphansFormat:
    def test_space_format_message_within_window_not_orphaned(self, setup_db):
        """表默认值形态（datetime('now') 空格分隔）窗口内消息 → 活跃，不判孤儿。

        旧字节比较：同日 ' '(0x20) < 'T'(0x54) → 恒判孤儿 → attempts+1 回炉（本用例红）。
        """
        from src.api.routes.runtime import runtime_recover_orphans
        c = setup_db
        c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
                  "VALUES (601, 'codex', 'online', 1, 1, 4)")
        _seed_task(c, "t_1495_space", runtime_id=601)
        _insert_message_raw(c, "t_1495_space", _fmt_utc(dt.datetime.now(dt.timezone.utc)))
        setup_db.commit()

        resp = runtime_recover_orphans(601, credential_id=0)

        assert "t_1495_space" in resp["skipped_active"], \
            f"空格格式活跃行应跳过回收，实际 {resp}"
        row = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_1495_space'").fetchone()
        assert row == ("running", 0), f"活跃任务状态/attempts 不应变，实际 {row}"

    def test_no_microsecond_iso_message_within_window_not_orphaned(self, setup_db):
        """无微秒 ISO 行（'...T...+00:00'）与同秒带微秒 cutoff → 活跃，不判孤儿。

        旧字节比较：同秒第 19 字节 '+'(0x2B) < '.'(0x2E) → 同秒窗口塌陷（本用例红）。
        """
        from src.api.routes.runtime import runtime_recover_orphans
        c = setup_db
        c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
                  "VALUES (602, 'codex', 'online', 1, 1, 4)")
        _seed_task(c, "t_1495_iso", runtime_id=602)
        _insert_message_raw(c, "t_1495_iso", dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
        setup_db.commit()

        resp = runtime_recover_orphans(602, credential_id=0)

        assert "t_1495_iso" in resp["skipped_active"], \
            f"无微秒 ISO 活跃行应跳过回收，实际 {resp}"
        row = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_1495_iso'").fetchone()
        assert row == ("running", 0), f"活跃任务状态/attempts 不应变，实际 {row}"

    def test_unparseable_message_not_active_orphan_recovered(self, setup_db):
        """仅含不可解析 created_at 的消息行 → 不计活跃（保守取舍），真孤儿仍回炉。"""
        from src.api.routes.runtime import runtime_recover_orphans
        c = setup_db
        c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
                  "VALUES (603, 'codex', 'online', 1, 1, 4)")
        _seed_task(c, "t_1495_bad", runtime_id=603)
        _insert_message_raw(c, "t_1495_bad", "not-a-timestamp")
        setup_db.commit()

        resp = runtime_recover_orphans(603, credential_id=0)

        assert "t_1495_bad" in resp["recovered"], \
            f"不可解析行不计活跃，孤儿应回炉，实际 {resp}"
        row = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_1495_bad'").fetchone()
        assert row == ("queued", 1), f"孤儿应回炉 attempts+1，实际 {row}"


# ===== claim 兜底回收调用点（src/runtime/service.py）=====

class TestClaimBackstopFormat:
    def test_space_format_message_blocks_claim_backstop(self, setup_db):
        """dispatched 超时但空格格式消息在 600s 窗口内 → 不回收、不杀旧进程。

        旧字节比较下该任务会被回炉 attempts+1（本用例红）。
        """
        from src.runtime import service
        c = setup_db
        c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
                  "VALUES (701, 'codex', 'online', 1, 0, 4)")
        _seed_task(c, "t_1495_cl_sp", runtime_id=701, status="dispatched")
        _insert_message_raw(c, "t_1495_cl_sp", _fmt_utc(dt.datetime.now(dt.timezone.utc)))
        setup_db.commit()

        service.claim(701)

        row = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_1495_cl_sp'").fetchone()
        assert row == ("dispatched", 0), \
            f"空格格式活跃行应跳过 claim 兜底回收，实际 {row}"

    def test_no_microsecond_iso_message_blocks_claim_backstop(self, setup_db):
        """dispatched 超时但无微秒 ISO 消息在 600s 窗口内 → 不回收（同秒窗口塌陷修复）。"""
        from src.runtime import service
        c = setup_db
        c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
                  "VALUES (702, 'codex', 'online', 1, 0, 4)")
        _seed_task(c, "t_1495_cl_iso", runtime_id=702, status="dispatched")
        _insert_message_raw(c, "t_1495_cl_iso",
                            dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
        setup_db.commit()

        service.claim(702)

        row = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_1495_cl_iso'").fetchone()
        assert row == ("dispatched", 0), \
            f"无微秒 ISO 活跃行应跳过 claim 兜底回收，实际 {row}"

    def test_dead_task_with_only_stale_messages_recovered_on_claim(self, setup_db):
        """真死任务：消息存在但全部在 600s 窗外 → claim 照常回收（不因 helper 漏回收）。"""
        from src.runtime import service
        c = setup_db
        c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
                  "VALUES (703, 'codex', 'online', 1, 0, 4)")
        _seed_task(c, "t_1495_dead", runtime_id=703, status="dispatched", attempts=0)
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1200)).isoformat()
        _insert_message_raw(c, "t_1495_dead", stale, "last breath")
        setup_db.commit()

        service.claim(703)

        row = c.execute("SELECT status, attempts, runtime_id FROM agent_tasks WHERE id='t_1495_dead'").fetchone()
        # 回收语义：attempts+1 且 runtime_id 归零回池（runtime.py 同款）；
        # 回池后可能被同一次 claim 的后续广播认领（status 变 running），二者均合法。
        assert row[1] == 1, f"真死任务应被兜底回收 attempts+1，实际 {row}"
        assert row[0] in ("queued", "running", "dispatched") and (
                row[0] != "running" or row[2] == 0 or row[2] == 703), \
            f"窗口外消息的真死任务应被兜底回收，实际 {row}"


# ===== 窗口常量单点定义（AC#3 护栏）=====

class TestWindowConstants:
    def test_constants_single_source(self):
        """600/1200 收敛为 task_activity 具名常量，两调用点引用同一来源。"""
        from src.runtime import task_activity
        assert task_activity.CLAIM_ACTIVITY_WINDOW_SECONDS == 600
        assert task_activity.RECOVER_ORPHANS_ACTIVITY_WINDOW_SECONDS == 1200

    def test_recover_orphans_uses_task_activity_window(self, setup_db, monkeypatch):
        """recover_orphans 活跃窗口随 task_activity 常量联动（不再各自拼字面量）。"""
        from src.api.routes.runtime import runtime_recover_orphans
        from src.runtime import task_activity
        c = setup_db
        c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
                  "VALUES (604, 'codex', 'online', 1, 1, 4)")
        # 消息在 1200s 窗口内、但在 300s 窗口外：随常量生效则判活跃跳过
        _seed_task(c, "t_1495_win", runtime_id=604)
        mid = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=task_activity.RECOVER_ORPHANS_ACTIVITY_WINDOW_SECONDS - 60)
        _insert_message_raw(c, "t_1495_win", mid.isoformat())
        setup_db.commit()

        resp = runtime_recover_orphans(604, credential_id=0)
        assert "t_1495_win" in resp["skipped_active"], \
            f"1200s 窗口内消息应跳过回收，实际 {resp}"


# ---------------------------------------------------------------------------
# AC#B 项：task_messages.created_at 表默认值口径（Issue #1495）
# ---------------------------------------------------------------------------

def test_sqlite_default_created_at_parses_as_utc_now():
    """SQLite 默认 datetime('now') 是 UTC，parse_dt 的 sqlite 分支按 UTC 解析 → 无歧义。

    这是「默认值只有 MySQL 侧需要改」的依据：若不成立，SQLite 新库同样会误判。
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE m (id INTEGER PRIMARY KEY, created_at TEXT DEFAULT (datetime('now')))")
    conn.execute("INSERT INTO m (id) VALUES (1)")
    ts = conn.execute("SELECT created_at FROM m WHERE id=1").fetchone()[0]
    conn.close()
    from src.runtime.health import parse_dt
    epoch = parse_dt(ts)
    assert epoch is not None, f"SQLite 默认值不可解析: {ts!r}"
    assert abs(_time.time() - epoch) < 60, f"SQLite 默认值与当前时间偏差过大: {ts!r}"


def test_created_at_default_migration_is_noop_on_sqlite():
    """迁移在 SQLite 后端必须是 no-op（SQLite 不支持 ALTER 改默认值，且默认已正确）。"""
    from src.api.db_setup import _fn_normalize_task_messages_created_at_default as mig
    assert mig() == 0
    assert mig() == 0  # 幂等


def test_created_at_default_migration_expression_avoids_percent_escape():
    """MySQL ALTER 的默认值表达式不得包含 `%`。

    mysql_compat 参数化会把 `%` 转义成 `%%`，DATE_FORMAT('%Y-...') 会退化为常量串
    （实测：插入得到字面量 %Y-%m-%dT...）。因此用 CONCAT/REPLACE 拼装。
    """
    import inspect
    from src.api import db_setup as ds
    src = inspect.getsource(ds._fn_normalize_task_messages_created_at_default)
    alter_line = [l for l in src.splitlines() if "ALTER TABLE task_messages" in l]
    assert alter_line, "迁移内未找到 ALTER 语句"
    joined = " ".join(alter_line)
    assert "%" not in joined, f"ALTER 表达式含 %，会被 mysql_compat 转义: {joined}"
    assert "+00:00" in src and "UTC_TIMESTAMP" in src
