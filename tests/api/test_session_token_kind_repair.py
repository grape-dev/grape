"""2026-11-13 事故回归：session_api_tokens.kind 静默漏列 → 会话凭据全 401。

事故链（线上证据：`grape-logs/{insight,worker}.log` 2026-11-13T01:23:06Z）：

1. `_fn_takeover_schema_v1` 调 `_ensure_column("session_api_tokens", "kind", ...)`
   时未传 strict；
2. 库按域拆分后，runtime/insight 里该表是**跨库视图**，`ALTER ... ADD COLUMN`
   报 1347 "'xxx' is not BASE TABLE"，`_ensure_column` 只记 MANUAL REVIEW 后
   **正常返回**；
3. 迁移执行器据此盖章 202611160001 → 列永不创建、下次启动不再重试；
4. `verify_session_token()` 的 SQL 带 `COALESCE(kind,'worker')` → MySQL 1054
   → 异常被 `except` 吞掉返回 None → 所有 `tk_` 会话凭据 401，Agent 任务卡在
   「前置检查」但 `exit_code=0`，流水线自动停摆且无告警。

本文件锁定修复契约：
- 非 owner 库（视图）缺列 → 抛 `_NonOwnerObject`，执行器**跳过不盖章**；
- owner 库（基表）缺列 → 正常 ALTER 补列；
- 两个 repair 迁移（202611160006 / 202611160007）已注册且按 owner 分域；
- MySQL 上 `session_key` 是 TEXT，索引 DDL 必须带前缀长度（否则 1170）。
"""
import pytest

from tests.mock_db import MockDB


def _view_mock(monkeypatch, db_setup, table_type="VIEW", cols=("id", "session_key")):
    """构造一个「当前库中该表是视图且缺列」的 mock 连接。"""
    mdb = MockDB()
    mdb.register("PRAGMA table_info", [(i, c) for i, c in enumerate(cols)])
    mdb.register("information_schema.tables", [(table_type,)])
    monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
    return mdb.cursor()


class TestEnsureColumnViewGuard:
    def _m(self):
        from src.api import db_setup
        return db_setup

    def test_non_owner_view_raises_and_does_not_alter(self, monkeypatch):
        m = self._m()
        c = _view_mock(monkeypatch, m, "VIEW")
        with pytest.raises(m._NonOwnerObject) as ei:
            m._ensure_column(c, "session_api_tokens", "kind",
                             "VARCHAR(16) NOT NULL DEFAULT 'worker'", strict=True)
        assert "视图" in str(ei.value)
        # 关键：绝不能盲发 ALTER（否则 1347 被吞 → 迁移盖章 → 列永不创建）
        assert not any("ALTER TABLE" in sql for sql, _ in c.queries), c.queries

    def test_base_table_still_alters(self, monkeypatch):
        m = self._m()
        c = _view_mock(monkeypatch, m, "BASE TABLE")
        m._ensure_column(c, "session_api_tokens", "kind",
                         "VARCHAR(16) NOT NULL DEFAULT 'worker'", strict=True)
        alters = [sql for sql, _ in c.queries if "ALTER TABLE" in sql]
        assert alters and "ADD COLUMN `kind`" in alters[0]
        assert "ALGORITHM=INSTANT" in alters[0]

    def test_column_already_present_is_idempotent(self, monkeypatch):
        m = self._m()
        c = _view_mock(monkeypatch, m, "VIEW", cols=("id", "session_key", "kind"))
        m._ensure_column(c, "session_api_tokens", "kind",
                         "VARCHAR(16) NOT NULL DEFAULT 'worker'", strict=True)
        assert not any("ALTER TABLE" in sql for sql, _ in c.queries)


class TestRepairMigrationsRegistered:
    def _registry(self):
        from src.api import db_setup
        return {m.version: m for m in db_setup._MIGRATIONS}

    def test_session_token_kind_repair_registered(self):
        reg = self._registry()
        assert "202611160006" in reg, "缺 repair-session-token-kind-v1（历史库已盖章不会重试）"
        assert reg["202611160006"].name == "repair-session-token-kind-v1"
        assert reg["202611160006"].fn is not None

    def test_task_messages_actor_repair_registered(self):
        reg = self._registry()
        assert "202611160007" in reg
        assert reg["202611160007"].name == "repair-task-messages-actor-v1"

    def test_takeover_migration_uses_strict(self):
        """`_fn_takeover_schema_v1` 必须 strict，否则失败也盖章（事故根因 2）。"""
        import inspect
        from src.api import db_setup
        src = inspect.getsource(db_setup._fn_takeover_schema_v1)
        assert src.count("strict=True") >= 5, "takeover 补列必须全部 strict=True"

    def test_index_ddl_has_text_prefix_on_mysql(self):
        """session_key 是 TEXT：MySQL 索引需前缀长度，否则 1170 静默丢索引。"""
        import inspect
        from src.api import db_setup
        src = inspect.getsource(db_setup._fn_takeover_schema_v1)
        assert "session_key(64)" in src