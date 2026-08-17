"""Chat Agent backend → runtime_id 解析修复测试（chat-agent-backend-resolution-fix.md E01-E06）。"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, get_chat_config, set_chat_config

# 测试用 runtime id（避免与存量冲突）
R_WORKER_GLOBAL = 901   # 共享全局 worker（credential_id=9001，online）
R_LOCAL_DIRECT = 902    # 本机直连（credential_id=0）
R_USER_WORKER = 903     # 个人 scope=user worker（credential_id=9002）
C_WORKER_GLOBAL = 9001
C_USER_WORKER = 9002


def _seed_runtimes():
    with db_lock:
        c = db.cursor()
        # 凭据行（scope_of_runtime 读 runtime_credentials.scope）
        c.execute("DELETE FROM runtime_credentials WHERE id IN (?,?)", (C_WORKER_GLOBAL, C_USER_WORKER))
        c.execute("INSERT OR REPLACE INTO runtime_credentials (id, token_hash, name, device_name, scope, owner_id, owner_name, status) VALUES (?,?,?,?,?,?,?,?)",
                  (C_WORKER_GLOBAL, "h1", "cred-shared", "worker-123249", "global", "", "", "active"))
        c.execute("INSERT OR REPLACE INTO runtime_credentials (id, token_hash, name, device_name, scope, owner_id, owner_name, status) VALUES (?,?,?,?,?,?,?,?)",
                  (C_USER_WORKER, "h2", "cred-user-44", "worker-44", "user", "44", "younger_eric", "active"))
        for rid, cid, scope, status in [
            (R_WORKER_GLOBAL, C_WORKER_GLOBAL, "global", "online"),
            (R_LOCAL_DIRECT, 0, "global", "offline"),
            (R_USER_WORKER, C_USER_WORKER, "user", "online"),
        ]:
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
            c.execute(
                "INSERT OR REPLACE INTO agent_runtimes (id, credential_id, name, installed, healthy, status, scope, owner_id) VALUES (?,?,?,1,1,?,?,?)",
                (rid, cid, "opencode", status, scope, "44" if scope == "user" else ""))
        db.commit()


def _clean():
    with db_lock:
        c = db.cursor()
        for rid in (R_WORKER_GLOBAL, R_LOCAL_DIRECT, R_USER_WORKER):
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        for cid in (C_WORKER_GLOBAL, C_USER_WORKER):
            c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        c.execute("DELETE FROM chat_agent_session WHERE id LIKE 'sess-cfg-%'")
        db.commit()


def _cfg_runtime_id(user_id):
    with db_lock:
        row = db.cursor().execute(
            "SELECT runtime_id FROM chat_agent_session WHERE id=?", (f"sess-cfg-{user_id}",)).fetchone()
        return row[0] if row else None


def test_bare_engine_name_resolves_to_local_direct():
    """E01：裸引擎名 'opencode' 同时存在本机直连(902)与共享 worker(901) → 保存到 902"""
    _seed_runtimes()
    set_chat_config(44, backend="opencode")
    assert _cfg_runtime_id(44) == R_LOCAL_DIRECT
    _clean()


def test_instance_name_resolves_to_personal_worker():
    """E02：实例名 'opencode-worker-44'（scope=user）→ 保存到个人 worker(903)"""
    _seed_runtimes()
    set_chat_config(44, backend="opencode-worker-44")
    assert _cfg_runtime_id(44) == R_USER_WORKER
    _clean()


def test_unknown_backend_does_not_crash():
    """E03：未知名回落（不崩溃，不指向共享 worker）"""
    _seed_runtimes()
    set_chat_config(44, backend="unknown-xyz")
    rid = _cfg_runtime_id(44)
    assert rid in (0, None) or rid != R_WORKER_GLOBAL
    _clean()


def test_local_direct_is_direct_mode():
    """E04：本机直连 runtime 走 EXEC_MODE_DIRECT，不入队远端"""
    _seed_runtimes()
    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
    assert runtime_execution_kind(R_LOCAL_DIRECT) == EXEC_MODE_DIRECT
    _clean()


def test_dirty_worker_config_reverted_to_local_direct():
    """E05：共享 worker 配置回落本机直连（db_setup 启动清理）"""
    _seed_runtimes()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, runtime_id, created_at, last_active_at) VALUES ('sess-cfg-44', 44, '', ?, datetime('now'), datetime('now'))",
            (R_WORKER_GLOBAL,))
        db.commit()
    from src.api.db_setup import _cleanup_chat_shared_worker_runtime
    _cleanup_chat_shared_worker_runtime()
    assert _cfg_runtime_id(44) == R_LOCAL_DIRECT
    _clean()


def test_unconfigured_user_materializes_default():
    """E06：无配置用户首次读取自动物化默认（本机直连），无需 Save"""
    _seed_runtimes()
    cfg = get_chat_config(55)
    assert _cfg_runtime_id(55) == R_LOCAL_DIRECT
    assert cfg.get("runtime_id") == R_LOCAL_DIRECT
    _clean()
