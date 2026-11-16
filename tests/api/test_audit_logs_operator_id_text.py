"""audit_logs.operator_id 列型守卫（2026-11-16 8070 E2E 实锤缺陷）。

缺陷：uuid-pk 线（202611120003）把账号 id 变 UUID 字符串，但 widen 清单漏了
audit_logs.operator_id（INTEGER 未改）。log_audit 传 uuid 进 INT 列 → MySQL
1265 Data truncated，被 except 吞成 WARNING，scene_pin / issue_admin_confirmed
等用户操作审计整条静默丢失。

修复面（三处，缺一即回归）：
1. DDL 源：_AUDIT_LOGS_DDL.operator_id 必须为 TEXT（fresh 实例）；
2. 迁移 202611170042 已注册（存量库 ALTER 幂等 + G-01 自检不盖章 raise）；
3. log_audit 端到端：uuid 字符串 operator_id 落库成功、行可读回（整链冒烟）。
"""
import re
import sqlite3

import src.api.db_setup as db_setup
import src.domains.mgmt.schema_ddl as mgmt_ddl
from src.domains.mgmt.auth import audit as audit_mod


def test_ddl_operator_id_is_text():
    """DDL 源锚：operator_id 不得回退 INTEGER（回退则 fresh 实例即复发 1265）。"""
    ddl = mgmt_ddl._AUDIT_LOGS_DDL
    m = re.search(r"operator_id\s+(\w+)", ddl)
    assert m, "operator_id column missing from _AUDIT_LOGS_DDL"
    assert m.group(1).upper() == "TEXT", (
        f"audit_logs.operator_id must be TEXT (uuid operator ids), got {m.group(1)}")


def test_migration_registered():
    """202611170042 必须在迁移注册表（含幂等自检语义：非 int 否则 raise 不盖章）。"""
    versions = {getattr(e, "version", None) or e.get("version")
                for e in getattr(db_setup, "MIGRATIONS", [])
                } if hasattr(db_setup, "MIGRATIONS") else None
    if versions is None:
        # 注册表形态不同——退化为源码 grep 锚
        src = open(db_setup.__file__, encoding="utf-8").read()
        assert '"202611170042"' in src, "migration 202611170042 not registered"
        assert 'name="audit-logs-operator-id-text"' in src
        assert "refuse to stamp" in src, "G-01 幂等自检（不盖章 raise）不得删"
        return
    assert "202611170042" in versions


def test_log_audit_uuid_operator_id_end_to_end(tmp_path, monkeypatch):
    """端到端冒烟：uuid operator_id 过 log_audit 落库可读回（不被 except 吞）。"""
    conn = sqlite3.connect(str(tmp_path / "audit.db"))
    conn.executescript(mgmt_ddl._AUDIT_LOGS_DDL)
    monkeypatch.setattr(audit_mod, "db", conn, raising=False)
    import threading
    monkeypatch.setattr(audit_mod, "db_lock", threading.RLock(), raising=False)

    audit_mod.log_audit(
        "scene_pin", operator_id="89f6c325-ca67-4490-ab6e-06754f66e6c7",
        detail={"scene_id": "s1", "pinned": True})

    rows = conn.execute(
        "SELECT event_type, operator_id, detail FROM audit_logs "
        "WHERE event_type='scene_pin'").fetchall()
    assert len(rows) == 1, "scene_pin audit row silently dropped (log_audit except swallow)"
    assert rows[0][1] == "89f6c325-ca67-4490-ab6e-06754f66e6c7"
