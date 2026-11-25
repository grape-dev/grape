"""Issue #4045 契约测试：引擎 revoke 后 chat 配置 runtime_id 悬空。

覆盖 Spec 验收标准的可测子集（真实 SQLite 测试库，不连 MySQL）：

- S1 删除侧：`purge_runtime_credential` 同事务摘绑 `chat_agent_session`（`runtime_id=0`）且
  **保留 `engine_uid`**，报告含 chat 摘绑行数；
- S2 读路径：悬空行 `engine_uid` 可解析 → 重解析到可用引擎并**回写 `runtime_id`**（DB 可见）；
- S3 出参三态：悬空且 uid 不可解析 → `bound=False, reason=engine_missing`；
  真未配置（`runtime_id=0` 且无 uid）→ `bound=False, reason=unbound`；
- S4 WARNING 收敛：同一 `runtime_id` 仅首次 `warning`，其后 `debug`；
- S5 迁移 009：dry-run 不改、apply 置 0 并保留 uid、二次执行 0 变更（幂等）。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
import sys

from src.kernel.session_manager import db, db_lock, get_chat_config

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

R_LIVE = 9501          # 现存可用引擎
R_LIVE2 = 9504         # 另一台现存引擎（uid 重解析目标）
R_GONE = 9599          # 已删引擎 id（悬空引用）
CID = 9503             # 待吊销凭据（与引擎行同批删除）
UID = 94045            # 隔离用 user_id
UID_UID_RESOLVE = 94046
UID_DANGLING = 94047
UID_UNBOUND = 94048

UID_A = "uid-4045-engine-a"
UID_B = "uid-4045-engine-b"


# ----------------------------------------------------------------- 基础设施
def _exec(sql: str, params=()):
    with db_lock:
        c = db.cursor()
        c.execute(sql, params)
        db.commit()


def _query(sql: str, params=()):
    with db_lock:
        c = db.cursor()
        c.execute(sql, params)
        return c.fetchall()


def _reset_engine_cache():
    try:
        from src.domains.runtime.engine import engine_select
        engine_select._resolve_local_cache.clear()
    except Exception:  # noqa: BLE001  # 缓存清理失败不影响断言（用例自种引擎）
        pass


def _seed_engine(rid: int, name: str, engine_uid: str = "", credential_id: int = 0):
    _exec("INSERT OR REPLACE INTO agent_runtimes "
          "(id, credential_id, name, installed, healthy, status, scope, owner_id, engine_uid, last_seen_at) "
          "VALUES (?,?,?,1,1,'online','global','',?,datetime('now'))",
          (rid, credential_id, name, engine_uid or None))


def _seed_row(row_id: str, user_id: int, runtime_id: int = 0, engine_uid: str = "",
              repo_key: str = ""):
    _exec("INSERT OR REPLACE INTO chat_agent_session "
          "(id, user_id, repo_key, runtime_id, engine_uid, skill, skill_id, created_at, last_active_at) "
          "VALUES (?,?,?,?,?, 'chat', 0, datetime('now'), datetime('now'))",
          (row_id, user_id, repo_key, runtime_id, engine_uid or None))


def _clean(uid: int):
    _exec("DELETE FROM chat_agent_session WHERE user_id=?", (uid,))


def _clean_engines(*rids: int):
    for rid in rids:
        _exec("DELETE FROM agent_runtimes WHERE id=?", (rid,))


# --------------------------------------------------------- S1 删除侧同事务摘绑
def test_revoke_detaches_chat_config_and_keeps_engine_uid():
    """吊销凭据删除引擎行时，chat 配置引用同事务置 0，且 engine_uid 保留（可重绑定线索）。"""
    from src.domains.runtime.public import purge_runtime_credential

    _clean(UID)
    _clean_engines(CID, R_GONE)
    _exec("INSERT OR REPLACE INTO runtime_credentials (id, token_hash, name, scope, status) "
          "VALUES (?, 'hash', 'issue4045-cred', 'user', 'active')", (CID,))
    _seed_engine(9601, "hermes", UID_A, credential_id=CID)
    _seed_row(f"sess-cfg-{UID}", UID, runtime_id=9601, engine_uid=UID_A)
    _exec("INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, engine_uid) "
          "VALUES ('devops-4045', 'o/r', 'coding', ?, ?)", (9601, UID_A))
    try:
        report = purge_runtime_credential(str(CID))
        assert report["engines"] == 1, f"引擎行应被删除，实际 {report}"
        assert report.get("chat_sessions") == 1, f"chat 配置引用应被摘绑并计数，实际 {report}"
        row = _query("SELECT runtime_id, engine_uid FROM chat_agent_session WHERE id=?",
                     (f"sess-cfg-{UID}",))
        assert row and row[0][0] == 0, f"chat runtime_id 应置 0，实际 {row}"
        assert row[0][1] == UID_A, f"engine_uid 必须保留（不可清空），实际 {row}"
        dev = _query("SELECT runtime_id FROM devops_agent_session WHERE id='devops-4045'")
        assert dev and dev[0][0] == 0, f"devops 会话摘绑口径不应回退，实际 {dev}"
        assert not _query("SELECT id FROM agent_runtimes WHERE id=9601"), "引擎行应已物理删除"
    finally:
        _exec("DELETE FROM devops_agent_session WHERE id='devops-4045'")
        _clean(UID)
        _clean_engines(9601, CID)
        _exec("DELETE FROM runtime_credentials WHERE id=?", (CID,))


# ------------------------------------------------------- S2 uid 重解析 + 回写
def test_uid_reresolve_backfills_runtime_id():
    """悬空 runtime_id + 有效 engine_uid → 按 uid 重解析到现存引擎，并把 runtime_id 回写。"""
    _clean(UID_UID_RESOLVE)
    _clean_engines(R_LIVE2)
    _seed_engine(R_LIVE2, "hermes", UID_B)
    _seed_row(f"sess-cfg-{UID_UID_RESOLVE}", UID_UID_RESOLVE, runtime_id=R_GONE, engine_uid=UID_B)
    try:
        cfg = get_chat_config(UID_UID_RESOLVE)
        assert cfg["runtime_id"] == R_LIVE2, f"应按 uid 重解析到现存引擎，实际 {cfg}"
        assert cfg["backend"] == "hermes", f"backend 应为重解析引擎名，实际 {cfg}"
        assert cfg.get("bound") is True, f"重解析成功应 bound=True，实际 {cfg}"
        row = _query("SELECT runtime_id FROM chat_agent_session WHERE id=?",
                     (f"sess-cfg-{UID_UID_RESOLVE}",))
        assert row and row[0][0] == R_LIVE2, f"重解析结果必须回写 DB，实际 {row}"
    finally:
        _clean(UID_UID_RESOLVE)
        _clean_engines(R_LIVE2)
        _reset_engine_cache()


# ----------------------------------------------------------- S3 出参三态
def test_dangling_without_uid_is_engine_missing():
    """悬空且无 engine_uid 可重解析 → bound=False / reason=engine_missing（不再静默）。"""
    _clean(UID_DANGLING)
    _seed_row(f"sess-cfg-{UID_DANGLING}", UID_DANGLING, runtime_id=R_GONE, engine_uid="")
    try:
        cfg = get_chat_config(UID_DANGLING)
        assert cfg.get("bound") is False, f"悬空配置应 bound=False，实际 {cfg}"
        assert cfg.get("reason") == "engine_missing", f"reason 应为 engine_missing，实际 {cfg}"
        assert cfg["runtime_id"] != R_GONE, f"不得把幽灵引擎当配置返回，实际 {cfg}"
    finally:
        _clean(UID_DANGLING)
        _reset_engine_cache()


def test_unbound_row_reason():
    """真未配置（runtime_id=0 且无 uid）→ bound=False / reason=unbound，与 engine_missing 可区分。"""
    _clean(UID_UNBOUND)
    _seed_row(f"sess-cfg-{UID_UNBOUND}", UID_UNBOUND, runtime_id=0, engine_uid="")
    try:
        cfg = get_chat_config(UID_UNBOUND)
        assert cfg.get("bound") is False, f"未绑定应 bound=False，实际 {cfg}"
        assert cfg.get("reason") == "unbound", f"reason 应为 unbound，实际 {cfg}"
    finally:
        _clean(UID_UNBOUND)
        _reset_engine_cache()


# ------------------------------------------------------- S4 WARNING 去重
def test_dangling_warning_deduped(caplog):
    """同一 runtime_id 只首条 WARNING，其后 debug（避免每请求刷屏）。"""
    from src.kernel import session_manager

    getattr(session_manager, "_DANGLING_WARN_SEEN", {}).clear()
    with caplog.at_level(logging.DEBUG):
        session_manager._chat_config_row_engine(R_GONE, "")
        session_manager._chat_config_row_engine(R_GONE, "")
        session_manager._chat_config_row_engine(R_GONE, "")
    hits = [r for r in caplog.records if "不存在（引擎行已删除）" in r.getMessage()]
    assert hits, "应至少记录一条悬空告警（不得静默）"
    warn = [r for r in hits if r.levelno == logging.WARNING]
    debug = [r for r in hits if r.levelno == logging.DEBUG]
    assert len(warn) == 1, f"同一 runtime_id 只应首条 WARNING，实际 {[(r.levelname, r.getMessage()) for r in hits]}"
    assert len(debug) == 2, f"其后应降级为 debug，实际 {[(r.levelname, r.getMessage()) for r in hits]}"


# ------------------------------------------------------- S5 迁移 009 幂等
def _load_migration():
    path = os.path.join(ROOT, "scripts", "migrations", "009_chat_config_dangling_runtime.py")
    spec = importlib.util.spec_from_file_location("mig009_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mig_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "mig009.db"))
    conn.execute("CREATE TABLE agent_runtimes (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE chat_agent_session ("
                 "id TEXT PRIMARY KEY, user_id TEXT, repo_key TEXT, runtime_id INTEGER, engine_uid TEXT)")
    conn.execute("INSERT INTO agent_runtimes (id, name) VALUES (1, 'hermes')")
    conn.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid) "
                 "VALUES ('sess-cfg-1', 1, '', 1080, 'uid-1080')")
    conn.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid) "
                 "VALUES ('sess-cfg-2', 2, '', 1091, NULL)")
    conn.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid) "
                 "VALUES ('sess-cfg-3', 3, '', 1202, '')")
    conn.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid) "
                 "VALUES ('sess-cfg-4', 4, '', 1, 'uid-1')")      # 正常行：不动
    conn.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid) "
                 "VALUES ('sess-cfg-5', 5, '', 0, '')")          # 已置 0：不动
    conn.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid) "
                 "VALUES ('repo-sess-6', 6, 'o/r', 1302, 'uid-1302')")   # repo 级会话悬空行
    conn.commit()
    return conn


def test_migration_009_idempotent(tmp_path):
    """dry-run 不改数据；apply 置 0 且保留 uid；二次 apply 0 变更。"""
    mod = _load_migration()
    conn = _mig_db(tmp_path)
    try:
        plan = mod.scan(conn)
        assert plan["dangling"] == 4, f"应扫出 4 行悬空（3 config + 1 repo 会话），实际 {plan}"
        assert plan["by_scope"] == {"user_config": 3, "repo_session": 1}, f"应按行类型分列，实际 {plan}"
        assert plan["ids"] == [1080, 1091, 1202, 1302], f"悬空 id 清单应准确，实际 {plan}"
        assert plan["with_uid"] == 2, f"应统计可重绑定的 uid 行，实际 {plan}"
        assert plan["has_uid"], f"应识别到 engine_uid 列，实际 {plan}"
        assert _query_mig(conn, "SELECT runtime_id FROM chat_agent_session WHERE id='sess-cfg-1'") == 1080, \
            "dry-run 不得改数据"

        mod.backup(conn, plan)
        changed = mod.apply_plan(conn, plan)
        assert changed == 4, f"应清扫 4 行，实际 {changed}"
        rows = conn.execute("SELECT id, runtime_id, engine_uid FROM chat_agent_session ORDER BY id").fetchall()
        by_id = {r[0]: (r[1], r[2]) for r in rows}
        assert by_id["sess-cfg-1"] == (0, "uid-1080"), f"悬空行置 0 且保留 uid，实际 {by_id}"
        assert by_id["sess-cfg-2"] == (0, None), f"无 uid 悬空行置 0，实际 {by_id}"
        assert by_id["sess-cfg-4"] == (1, "uid-1"), "正常行不得被改"
        assert by_id["sess-cfg-5"] == (0, ""), "已置 0 行不得被改"

        again = mod.scan(conn)
        assert again["dangling"] == 0, f"迁移后应 0 悬空，实际 {again}"
        assert mod.apply_plan(conn, again) == 0, "二次 apply 必须 0 变更（幂等）"
    finally:
        conn.close()
        _reset_engine_cache()


def _query_mig(conn, sql: str):
    row = conn.execute(sql).fetchone()
    return row[0] if row else None