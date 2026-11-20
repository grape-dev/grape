"""grape_t_engine_template_binding 三元组唯一约束守卫（spec B1 兑现 / 迁移 202611160001）。

背景（多模板共用引擎 spec docs/sandbox/platform/multi-template-per-engine-spec.md B1）：
- 建表 DDL 曾残留窄 UNIQUE(provider_id, engine_type)，与 provider-report handler 的
  三元组 upsert 不匹配——同 Provider 不同模板上报相同引擎必 500
  （E2E 实证 Duplicate entry '1-hermes'，2026-11-13 arch-4p）。
守卫断言：
- 源码建表 DDL 为三元组 UNIQUE(provider_id, engine_type, template_id)；
- 无窄 UNIQUE(provider_id, engine_type)（无 template_id 维度）残留；
- 迁移函数对 SQLite（无 information_schema）安全跳过不阻断。
"""
import sqlite3

import pytest


def _binding_ddl():
    from src.api import db_setup
    # 建表语句在模块内多个 (name, ddl) 清单中——全量收集核对
    for attr in dir(db_setup):
        val = getattr(db_setup, attr, None)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "grape_t_engine_template_binding":
                    return item[1]
    return None


def test_binding_ddl_is_3tuple_unique():
    ddl = _binding_ddl()
    assert ddl, "grape_t_engine_template_binding 建表 DDL 不在 _TABLES"
    assert "UNIQUE(provider_id, engine_type, template_id)" in ddl, \
        "spec B1：唯一约束必须是三元组 (provider_id, engine_type, template_id)"
    # 窄约束残留检测：UNIQUE(provider_id, engine_type) 不以 template_id 收尾
    import re
    narrow = re.search(r"UNIQUE\(provider_id,\s*engine_type\)(?!\s*,\s*template_id)", ddl)
    assert not narrow, "旧窄 UNIQUE(provider_id, engine_type) 残留——同引擎多模板仍会被 DB 拒绝"


def test_migration_sqlite_safe_skip(tmp_path):
    """SQLite（无 information_schema）下迁移安全跳过，不抛不阻断。"""
    from src.api import db_setup
    c = sqlite3.connect(str(tmp_path / "mig.db"))
    c.executescript("""
    CREATE TABLE grape_t_engine_template_binding (
        id INTEGER PRIMARY KEY, provider_id INTEGER NOT NULL,
        engine_type VARCHAR(64) NOT NULL, template_id INTEGER NOT NULL,
        status VARCHAR(32) DEFAULT '', engine_version VARCHAR(64) DEFAULT '',
        created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
        UNIQUE(provider_id, engine_type, template_id)
    );
    """)
    # db_setup.db 可能指向全局连接——这里只测 conn 显式传入路径
    rep = db_setup._migration_engine_template_binding_widen(conn=c)
    assert rep["action"] == "skipped", rep
    assert "information_schema" in rep["detail"] or "already" in rep["detail"], rep
    c.close()


def test_handler_upsert_matches_3tuple():
    """provider-report handler 仍按 (provider, engine, template) 三元组 upsert（回归锚点）。"""
    import inspect
    from src.domains.runtime.routes import runtime as rt
    src = inspect.getsource(rt.sandbox_provider_report)
    assert "SELECT id FROM grape_t_engine_template_binding WHERE provider_id=? AND engine_type=? AND template_id=?" in src
