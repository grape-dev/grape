"""runtime.public 会话/任务写原语单测（P3-5g）：账号级联、渠道会话、VOD、会话摘绑、trace 回填。"""
import contextlib

from src.domains.runtime import public as rt
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rowcounts=None, fetch_pages=None):
        self.executed = []
        self.batches = []
        self.rowcounts = list(rowcounts or [])
        self.fetch_pages = list(fetch_pages or [])

    def execute(self, sql, args=None):
        self.executed.append((" ".join(sql.split()), args))
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 1
        return self.rowcount

    def executemany(self, sql, rows):
        self.batches.append((" ".join(sql.split()), [tuple(r) for r in rows]))
        return len(self.batches[-1][1])

    def fetchall(self):
        if not self.fetch_pages:
            return []
        return [(v,) for v in self.fetch_pages.pop(0)]

    def fetchone(self):
        return None

    def close(self):
        pass


def install(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        assert domain == "runtime"
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def install_own(monkeypatch, cur):
    """自有 DSN 拓扑（`_any_cursor` 走 db_registry.cursor，autocommit 不提交）。"""
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda _d: True)
    return install(monkeypatch, cur)


def install_tx(monkeypatch, cur):
    """自有 DSN 拓扑：`_any_tx()` 走 db_registry.transaction。"""

    @contextlib.contextmanager
    def _tx(domain):
        assert domain == "runtime"
        yield cur

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda _d: True)
    monkeypatch.setattr(db_registry, "transaction", _tx)
    return cur


def test_purge_user_rows_deletes_four_tables(monkeypatch):
    cur = install_tx(monkeypatch, FakeCursor(rowcounts=[3, 1, 5, 0]))
    out = rt.purge_user_rows("u1")
    assert out == {"agent_tasks": 3, "chat_agent_session": 1, "chat_conversations": 5, "issue": 0}
    assert [e[0] for e in cur.executed] == [
        "DELETE FROM agent_tasks WHERE user_id=?",
        "DELETE FROM chat_agent_session WHERE user_id=?",
        "DELETE FROM chat_conversations WHERE user_id=?",
        "DELETE FROM issue WHERE user_id=?",
    ]
    assert all(e[1] == ("u1",) for e in cur.executed)


def test_purge_user_rows_reuses_caller_cursor_in_single_db(monkeypatch):
    """单库拓扑：复用调用方游标（不另开连接、不自行提交）。"""
    cur = FakeCursor(rowcounts=[2, 0, 4, 1])
    called = {"cursor": 0}

    @contextlib.contextmanager
    def _no_cursor(**_kw):
        called["cursor"] += 1
        yield FakeCursor()

    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda _d: False)
    monkeypatch.setattr(db_registry, "cursor", _no_cursor)

    out = rt.purge_user_rows("u9", cursor=cur)
    assert out == {"agent_tasks": 2, "chat_agent_session": 0, "chat_conversations": 4, "issue": 1}
    assert called["cursor"] == 0
    assert len(cur.executed) == 4


def test_delete_chat_conversations_by_channel(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcounts=[4]))
    assert rt.delete_chat_conversations("cli_a") == 4
    assert cur.executed[0] == ("DELETE FROM chat_conversations WHERE im_channel_id=%s", ("cli_a",))


def test_delete_chat_conversations_empty_is_noop(monkeypatch):
    cur = install(monkeypatch, FakeCursor())
    assert rt.delete_chat_conversations("") == 0
    assert cur.executed == []


def test_delete_vod_conversations_by_ids(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcounts=[2]))
    assert rt.delete_vod_conversations(agent_session_ids=["s1", "s2"]) == 2
    sql, args = cur.executed[0]
    assert sql == "DELETE FROM vod_conversations WHERE agent_session_id IN (%s,%s)"
    assert args == ("s1", "s2")


def test_delete_vod_conversations_by_repo(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcounts=[7]))
    assert rt.delete_vod_conversations(repo_key="gitcode:o/r") == 7
    sql, args = cur.executed[0]
    assert "SELECT id FROM devops_agent_session WHERE repo_key=%s AND agent_type='vod'" in sql
    assert args == ("gitcode:o/r",)


def test_delete_vod_conversations_requires_scope(monkeypatch):
    cur = install(monkeypatch, FakeCursor())
    assert rt.delete_vod_conversations() == 0
    assert cur.executed == []


def test_detach_agent_sessions(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcounts=[2]))
    assert rt.detach_agent_sessions([5, 7]) == 2
    sql, args = cur.executed[0]
    assert sql == "UPDATE devops_agent_session SET runtime_id=0 WHERE runtime_id IN (%s,%s)"
    assert args == (5, 7)


def test_detach_agent_sessions_empty_is_noop(monkeypatch):
    cur = install(monkeypatch, FakeCursor())
    assert rt.detach_agent_sessions([]) == 0
    assert cur.executed == []


def test_clear_agent_session_runtime(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcounts=[1]))
    assert rt.clear_agent_session_runtime("gitcode:o/r", "vod") == 1
    sql, args = cur.executed[0]
    assert sql == ("UPDATE devops_agent_session SET runtime_session_id='' "
                   "WHERE repo_key=%s AND agent_type=%s")
    assert args == ("gitcode:o/r", "vod")


def test_ensure_agent_session_is_insert_ignore(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcounts=[1]))
    assert rt.ensure_agent_session("sid-1", "gitcode:o/r", "vod") == 1
    sql, args = cur.executed[0]
    assert sql.startswith("INSERT IGNORE INTO devops_agent_session")
    assert args == ("sid-1", "gitcode:o/r", "vod", "idle")


def test_stamp_task_traces_batch(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcounts=[1, 0]))
    n = rt.stamp_task_traces([("t1", "tr1", "p1"), ("t2", "tr2", "p2")])
    assert n == 1
    assert len(cur.executed) == 2
    assert cur.executed[0] == (
        "UPDATE agent_tasks SET trace_id=%s, pipeline_trace_id=%s WHERE id=%s",
        ("tr1", "p1", "t1"))


def test_stamp_task_traces_empty_is_noop(monkeypatch):
    cur = install(monkeypatch, FakeCursor())
    assert rt.stamp_task_traces([]) == 0
    assert cur.executed == []


# ---- 凭据使用审计原语（c53729dc0 把 SQL 从 kernel 下沉到 runtime 域）----

_AUDIT_INSERT_SQL = (
    "INSERT INTO grape_t_credential_usage_log (id, credential_id, credential_kind, "
    "user_id, repo_key, initiating_actor_id, executor_context, operation, created_at) "
    "VALUES (?,?,?,?,?,?,?,?,?)"
)


def test_credential_usage_log_insert_many_empty_is_noop(monkeypatch):
    cur = install_own(monkeypatch, FakeCursor())
    rt.credential_usage_log_insert_many([])
    assert cur.batches == []
    assert cur.executed == []


def test_credential_usage_log_insert_many_keeps_column_order(monkeypatch):
    """批量写入保持调用方的九列行序（kernel 侧攒批不重排列）。"""
    cur = install_own(monkeypatch, FakeCursor())
    rows = [("id-1", "cred-1", "user_credentials", "7", "gitcode:o/r",
             "1", "task", "token_fetch", "2026-11-11 00:00:00"),
            ("id-2", "cred-2", "repo_credentials", "8", "",
             "", "chat", "", "2026-11-11 00:00:01")]
    rt.credential_usage_log_insert_many(rows)
    assert cur.batches == [(_AUDIT_INSERT_SQL, rows)]


def test_credential_usage_log_prune_two_step_per_page(monkeypatch):
    """两步查删（SELECT id LIMIT → DELETE IN 列表）：MySQL 禁 IN 子查询带 LIMIT（1235）。"""
    # rowcount 序列对齐 execute：SELECT(忽略) / DELETE(2) / SELECT(忽略) / DELETE(1)
    cur = install_own(monkeypatch, FakeCursor(rowcounts=[0, 2, 0, 1],
                                              fetch_pages=[["a", "b"], ["c"]]))
    assert rt.credential_usage_log_prune("2026-01-01 00:00:00", batch=2) == 3
    assert [e[0] for e in cur.executed] == [
        "SELECT id FROM grape_t_credential_usage_log WHERE created_at < ? LIMIT ?",
        "DELETE FROM grape_t_credential_usage_log WHERE id IN (?,?)",
        "SELECT id FROM grape_t_credential_usage_log WHERE created_at < ? LIMIT ?",
        "DELETE FROM grape_t_credential_usage_log WHERE id IN (?)",
    ]
    assert cur.executed[0][1] == ("2026-01-01 00:00:00", 2)
    assert cur.executed[1][1] == ["a", "b"]
    assert cur.executed[3][1] == ["c"]


def test_credential_usage_log_prune_no_rows_is_noop(monkeypatch):
    cur = install_own(monkeypatch, FakeCursor(fetch_pages=[[]]))
    assert rt.credential_usage_log_prune("2026-01-01 00:00:00", batch=100) == 0
    assert len(cur.executed) == 1, cur.executed
