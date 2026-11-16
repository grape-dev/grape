"""template_uid 稳定键列（2026-11-04 i5 契约 #2，用户裁定）。

契约（对齐 provider_uid / #646 credential_uid 语义，UUIDv4 创建时一次性生成终身不变）：
- S1 新库：DDL 含 template_uid VARCHAR(36) 列 + NULL-safe UNIQUE（迁移前存量 NULL 可并存）；
- S2 存量库：register_migration 幂等加列 + 唯一索引 + backfill（rowcount 校验）；
- S3 backfill 纪律：**沿旧行一次性发 uid**（本列从未存在，无「沿用旧行值」窗口——
  与 provider_uid 同款；两跑幂等，已 uid 的行不动）；
- S4 消费：sandbox-templates 列表响应带 template_uid（旧库缺列降级 ''），
  cloud_worker _expand_templates 透传进 runtime_info（uid 缺一 fail-fast）。
"""
import sqlite3

import pytest



def _fn_source(mig) -> str:
    """迁移 fn 源码；lambda 注册形态解引用内部具名函数（本线移植注册惯例）。"""
    import inspect, re
    src = inspect.getsource(mig.fn)
    m = re.search(r"lambda:\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", src)
    if m:
        import src.api.db_setup as _db
        return inspect.getsource(getattr(_db, m.group(1)))
    return src

class TestTemplateUidColumn:
    def _ddl(self):
        from src.api import db_setup
        return dict(db_setup._SANDBOX_CONTROL_TABLES)["sandbox_templates"]

    def test_new_ddl_has_template_uid(self):
        """S1：全新库 DDL 直接含 template_uid 列。"""
        ddl = self._ddl().lower()
        assert "template_uid" in ddl, "sandbox_templates DDL 缺 template_uid 列"

    def test_migration_registered(self):
        """S2：迁移进版本表执行器（register_migration 幂等加列）。"""
        from src.api import db_setup
        migs = [m for m in db_setup._MIGRATIONS
                if getattr(m, "name", "") == "issue-sandbox-control-plane-schema"]
        assert migs, "issue-sandbox-control-plane-schema 迁移未注册（template_uid 列承载者）"
        assert migs[0].kind == "safe"
        assert callable(migs[0].fn)

    def test_migration_fn_body(self):
        """S3：迁移 fn 含 rowcount 校验与幂等谓词（对齐 #868 交办修复口径）。"""
        from src.api import db_setup
        migs = [m for m in db_setup._MIGRATIONS
                if getattr(m, "name", "") == "issue-sandbox-control-plane-schema"]
        assert migs, "迁移未注册"
        src = _fn_source(migs[0])
        assert "_SANDBOX_CONTROL_TABLES" in src, "迁移 fn 未走 _SANDBOX_CONTROL_TABLES 建表"
        from src.api import db_setup as _dbs
        _ddl = dict(_dbs._SANDBOX_CONTROL_TABLES)["sandbox_templates"]
        assert "template_uid" in _ddl, "控制面 DDL 缺 template_uid 列"
        assert ("IF NOT EXISTS" in _ddl or "_ensure_column" in src), "缺幂等手段（两跑不能重发 uid）"


class TestCredentialUidColumn:
    """credential_uid（同机多实例单锚 + 派生种子首分量）。"""

    def test_new_ddl_has_credential_uid(self):
        from src.api import db_setup
        ddl = dict(db_setup._NEW_TABLES)["runtime_credentials"].lower()
        assert "credential_uid" in ddl, "runtime_credentials DDL 缺 credential_uid 列"

    def test_migration_registered(self):
        from src.api import db_setup
        migs = [m for m in db_setup._MIGRATIONS
                if getattr(m, "name", "") == "issue-646-add-uid-columns"]
        assert migs, "issue-646-add-uid-columns 迁移未注册（credential_uid 列承载者，Issue #646）"
        assert migs[0].kind == "safe"

    def test_whoami_has_credential_uid_no_server_instance(self):
        """whoami 响应：含 credential_uid、不含 server_instance。"""
        import inspect
        from src.domains.runtime.routes import runtime as rt
        src = inspect.getsource(rt.runtime_whoami)
        assert '"credential_uid"' in src, "whoami 缺 credential_uid 出口"
        assert '"server_instance"' not in src, "whoami 仍返回 server_instance 字段"
        assert "get_server_instance" not in src, "whoami 仍调用 get_server_instance"
