"""S126 场景用例（D90 消费端批步 4）：credential_usage_log 审计表（P-5 硬前置）。

背景（契约决议 P-5；requirements 铁律 6/9）：
- P-3 并存形态的**准入门槛**：没有它 = "A 的凭据被 B 使用但审计只显示 A"
- executor/initiator 分列（业界硬要求）；credential_kind 值域 D100 收窄
  （upc kind 退役，存量行保留）
- 无外键级联（凭据删除保留审计历史，铁律 6）；审计行只增不改
- 高频审计写入走 Core insert（铁律 9 豁免场景）

UC↔用例映射（requirements UC30）：
- UC30① 表结构守卫：credential_kind/initiating_actor_id/executor_context 分列在场
- UC30② 无外键级联语义：凭据行删除后审计行保留（软引用）
- UC30③ 只增不改语义：模块无 update/delete API（AST 扫描）
- UC30④ 盖章迁移在册 + 幂等
- UC30⑤ Core 参数化写入 + 查询回环
"""
import ast
import os
import re
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

_TEST_DIR = Path(__file__).resolve().parent
_SRC = _TEST_DIR.parent / "src"

from src.persistence.credential_usage_log import (
    core_table, ensure_table, list_by_credential, log_usage)
from src.kernel.session_manager import db, db_lock


class TestCredentialUsageLog:
    def test_uc30_1_schema_columns(self):
        """executor/initiator 分列 + credential_kind 预留在场（D53 评审硬要求）。"""
        cols = {r[1] for r in db.execute(
            "PRAGMA table_info(grape_t_credential_usage_log)").fetchall()}
        for must in ("id", "credential_id", "credential_kind", "user_id",
                     "initiating_actor_id", "executor_context", "created_at"):
            assert must in cols, f"缺列 {must}"

    def test_uc30_2_no_fk_cascade_keeps_history(self):
        """凭据行删除后审计行保留（软引用语义——铁律 6）。"""
        ensure_table()
        rid = log_usage("s126_deleted_cred", 77, executor_context="task", flush=True)
        assert rid
        # 模拟凭据行删除（凭据源表无此行也无妨——软引用无 FK 约束）
        assert list_by_credential("s126_deleted_cred"), "审计行丢失"
        # 建表语句无 FOREIGN KEY
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='grape_t_credential_usage_log'").fetchone()[0]
        assert "FOREIGN KEY" not in ddl.upper(), "审计表出现外键（级联删除会毁审计历史）"

    def test_uc30_6_uuid_user_text_columns(self):
        """D105 第二批回归：UUIDv7 归属者/触发者以 TEXT 列写入审计表正常。

        user_id/initiating_actor_id 声明必须 TEXT（grape_accounts.id 已 UUIDv7）；
        UUID 写读回环 + 存量整型传参兼容（TEXT 亲和性）。
        注：模块 DDL（credential_usage_log._DDL）为准——测试库可能被
        db_setup 的旧声明先建表（另一批次收口），以源码 DDL 断言避免误报。
        """
        from src.id_gen import uuid7_str
        ensure_table()
        ddl = (_SRC / "persistence" / "credential_usage_log.py").read_text(encoding="utf-8")
        assert re.search(r"user_id\s+TEXT\s+NOT NULL", ddl), "user_id 列声明须 TEXT"
        assert re.search(r"initiating_actor_id\s+TEXT\s+DEFAULT", ddl), \
            "initiating_actor_id 列声明须 TEXT"
        uid, actor = uuid7_str(), uuid7_str()
        rid = log_usage(uuid7_str(), uid, credential_kind="user_credentials",
                        initiating_actor_id=actor, executor_context="manual", flush=True)
        row = db.execute(
            "SELECT user_id, initiating_actor_id FROM grape_t_credential_usage_log WHERE id=?",
            (rid,)).fetchone()
        assert row == (uid, actor), f"UUID 归属者/触发者写读不一致: {row}"
        # 存量整型传参兼容（TEXT 亲和性）
        rid2 = log_usage(uuid7_str(), 42, initiating_actor_id=0, executor_context="task", flush=True)
        row2 = db.execute(
            "SELECT user_id FROM grape_t_credential_usage_log WHERE id=?", (rid2,)).fetchone()
        assert row2 is not None and str(row2[0]) == "42"

    def test_uc30_3_append_only_no_update_api(self):
        """只增不改：模块 API 面禁 update/delete（AST 扫描，防审计篡改通道）。"""
        src = (_SRC / "persistence" / "credential_usage_log.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert not re.match(r"(update|delete)_", node.name), \
                    f"审计模块出现修改类 API: {node.name}"
        # Core 层面：模块内无 update()/delete() 语句构造
        assert ".update()" not in src and ".delete()" not in src

    def test_uc30_4_stamped_migration_registered(self):
        from src.api.db_setup import _MIGRATIONS
        mig = [m for m in _MIGRATIONS if m.name == "create-credential-usage-log-table"]
        assert mig, "盖章迁移未注册"
        assert mig[0].kind == "safe" and mig[0].version == "202611070002"

    def test_uc30_5_core_insert_and_query_roundtrip(self):
        """Core 参数化写入 + 查询回环（铁律 9 高频审计写入豁免通道）。"""
        cred = "s126_rt_" + uuid.uuid4().hex[:8]
        rid = log_usage(cred, 424242, credential_kind="user_credentials",
                        initiating_actor_id=1, executor_context="chat",
                        operation="token_fetch", flush=True)
        assert rid and len(rid) == 36
        rows = list_by_credential(cred)
        assert len(rows) == 1
        r = rows[0]
        # D105b：user_id/initiating_actor_id 列 TEXT 归一，读回恒 str（int 传参保值兼容）
        assert (r["credential_kind"], r["user_id"], r["initiating_actor_id"],
                r["executor_context"]) == ("user_credentials", "424242", "1", "chat")
        # 双写幂等无损（两条不同 operation 各成一行）——flush=True 同步直写（确定性断言）
        log_usage(cred, 424242, operation="second", flush=True)
        assert len(list_by_credential(cred)) == 2

    def test_uc30_7_flush_batch_mysql_dispatch_no_bare_sqlite(self, monkeypatch):
        """UC30⑦（BLK-1 / B1）：MySQL 后端下 _flush_batch 必须走统一 DB 抽象。

        裸连 sqlite3 在 MySQL 后端下 = 审计行写进本地不存在的 sqlite 文件，
        审计静默丢失。判据：backend=mysql 时 executemany/commit 走翻译层连接，
        全程零 sqlite3.connect 调用（调用即红）。
        """
        import sqlite3 as _sq
        import src.kernel.mysql_compat as _mc
        import src.kernel.session_manager as _sm
        from src.persistence.credential_usage_log import _flush_batch

        def _forbidden(*_a, **_kw):  # pragma: no cover - 命中即违规
            raise AssertionError(
                "BLK-1 violation: bare sqlite3.connect under mysql backend")
        monkeypatch.setattr(_sq, "connect", _forbidden)

        calls: dict = {}

        class _FakeCompatDB:
            def executemany(self, sql, rows):
                calls["sql"] = sql
                calls["rows"] = [tuple(r) for r in rows]
                return len(calls["rows"])

            def commit(self):
                calls["commit"] = True

        monkeypatch.setattr(_mc, "get_db_backend", lambda: "mysql")
        monkeypatch.setattr(_sm, "db", _FakeCompatDB())

        rid = str(uuid.uuid4())
        row = {"id": rid, "credential_id": "cred_x", "credential_kind":
               "user_credentials", "user_id": 7, "repo_key": "gitcode:org/repo",
               "initiating_actor_id": 1, "executor_context": "task",
               "operation": "token_fetch", "created_at": "2026-11-11T00:00:00Z"}
        _flush_batch([row])

        # 审计行经翻译层批量落库且提交——不静默丢行
        assert calls["sql"].startswith("INSERT INTO grape_t_credential_usage_log"), \
            calls.get("sql")
        assert len(calls["rows"]) == 1
        assert calls["rows"][0][0] == rid
        assert calls["rows"][0][1] == "cred_x"
        assert calls.get("commit") is True
