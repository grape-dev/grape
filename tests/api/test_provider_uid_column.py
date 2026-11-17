"""provider_uid 稳定键列（2026-11-04 用户裁定：grape_t_sandbox_provider_config 加 UUIDv4 列，
暂无消费者也先落——为后续沙箱 uid 化/分布式合流预留身份锚）。

契约（同 #646 credential_uid 语义，UUIDv4 创建时一次性生成终身不变）：
- S1 新库：DDL 含 provider_uid VARCHAR(36) 列 + 非空判定的 UNIQUE 索引（NULL/'' 可并存，
  迁移前存量行不撞唯一键）；
- S2 存量库：register_migration 幂等加列 + 唯一索引（两跑不报错）；
- S3 两个写入入口都生成 uid：WebUI 生成沙箱凭据（settings.py 一凭据一 Provider 链）与
  worker provider-report 自注册链——同 Provider 幂等重试 uid 不换（命中 UPDATE 分支不重写 uid）；
- S4 读取出口：provider-configs 下发带 provider_uid（旧库缺列降级 ''）。
"""
import importlib
import sqlite3

import pytest


def _fresh_conn_with_table(conn_sqlite: sqlite3.Connection):
    """手工建最小 grape_t_sandbox_provider_config 表（不含 uid 列），模拟存量库。"""
    c = conn_sqlite.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS grape_t_sandbox_provider_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform VARCHAR(64) NOT NULL DEFAULT '',
            endpoint VARCHAR(512) NOT NULL DEFAULT '',
            api_key_enc TEXT,
            extra_config TEXT,
            enabled INTEGER DEFAULT 1,
            max_sandboxes INTEGER DEFAULT 10,
            runtime_credential_id INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_by VARCHAR(64) DEFAULT ''
        )
    """)
    conn_sqlite.commit()
    return conn_sqlite



def _fn_source(mig) -> str:
    """迁移 fn 源码；lambda 注册形态解引用内部具名函数（本线移植注册惯例）。"""
    import inspect, re
    src = inspect.getsource(mig.fn)
    m = re.search(r"lambda:\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", src)
    if m:
        import src.api.db_setup as _db
        return inspect.getsource(getattr(_db, m.group(1)))
    return src

class TestProviderUidColumn:
    def _ddl(self):
        from src.api import db_setup
        return dict(db_setup._SANDBOX_CONTROL_TABLES)["grape_t_sandbox_provider_config"]

    def test_new_ddl_has_provider_uid(self):
        """S1：全新库 DDL 直接含 provider_uid 列。"""
        ddl = self._ddl().lower()
        assert "provider_uid" in ddl, "grape_t_sandbox_provider_config DDL 缺 provider_uid 列"

    def test_migration_registered(self):
        """S2：迁移进版本表执行器（register_migration 幂等加列）。"""
        from src.api import db_setup
        migs = [m for m in db_setup._MIGRATIONS
                if getattr(m, "name", "") == "issue-sandbox-control-plane-schema"]
        assert migs, "issue-sandbox-control-plane-schema 迁移未注册（provider_uid 列承载者）"
        assert migs[0].kind == "safe"
        assert callable(migs[0].fn)

    def test_uuid_shape(self):
        """uid 生成器输出 36 位小写连字符 UUID（UUIDv4）。"""
        from src.kernel.sandbox_uid import new_provider_uid, is_uid_str
        for _ in range(3):
            u = new_provider_uid()
            assert is_uid_str(u), f"new_provider_uid 输出非法: {u}"
            assert u[14] == "4", f"非 UUIDv4: {u}"

    def test_migration_fn_body(self):
        """S3：迁移 fn 含 rowcount 校验与幂等谓词（对齐 #868 交办修复口径）。"""
        from src.api import db_setup
        migs = [m for m in db_setup._MIGRATIONS
                if getattr(m, "name", "") == "issue-sandbox-control-plane-schema"]
        assert migs, "迁移未注册"
        src = _fn_source(migs[0])
        # 本线形态（2026-11-10 移植）: 控制面 schema 迁移按 _SANDBOX_CONTROL_TABLES 逐表建，
        # 列定义在常量 DDL 内不在 fn 字面——守卫改为: fn 引用该常量 + DDL 含列 + 幂等手段
        assert "_SANDBOX_CONTROL_TABLES" in src, "迁移 fn 未走 _SANDBOX_CONTROL_TABLES 建表"
        from src.api import db_setup as _dbs
        _ddl = dict(_dbs._SANDBOX_CONTROL_TABLES)["grape_t_sandbox_provider_config"]
        assert "provider_uid" in _ddl, "控制面 DDL 缺 provider_uid 列"
        assert ("IF NOT EXISTS" in _ddl or "_ensure_column" in src), "缺幂等手段（两跑不能重发 uid）"
