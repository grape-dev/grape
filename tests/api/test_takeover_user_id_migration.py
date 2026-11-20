"""takeover_sessions.user_id 放宽迁移（202611200001）的守卫。

锁三件事，防回归：
1. 含 MODIFY COLUMN 的迁移必须按 cleanup 语义注册（Issue #536 D4-2：破坏性 DDL
   要过 min_code_version 门槛，蓝绿共存期旧实例不得先执行）；
2. 实现必须走 `_destructive_ddl(..., kind_hint="cleanup")`，不能裸 ALTER；
3. `takeover_sessions` 归 runtime 域：三库拓扑下进程库的同名历史表不是接管接口
   真正读写的那份，实现要额外覆盖 runtime 域；被拦下时抛错（不盖章，下次重试）。
"""

import inspect

from src.api import db_setup


def _mig():
    return next(m for m in db_setup._MIGRATIONS if m.version == "202611200001")


def test_registered_as_cleanup_semantics():
    m = _mig()
    assert m.name == "takeover-sessions-user-id-uuid-v1"
    assert m.kind == "cleanup", "含 MODIFY COLUMN 的迁移必须走 cleanup（min_code_version 门槛）"


def test_uses_destructive_gate_and_covers_runtime_domain():
    src = inspect.getsource(db_setup._fn_takeover_sessions_user_id_uuid_v1)
    assert "_destructive_ddl(" in src, "MODIFY 必须经 _destructive_ddl 收口，禁止裸 ALTER"
    assert 'kind_hint="cleanup"' in src
    assert 'uses_own_dsn("runtime")' in src, "runtime 域库要单独覆盖（进程库同名表不是权威）"
    assert "raise RuntimeError" in src, "被门槛拦下必须抛错（否则迁移盖章而 DDL 未执行）"