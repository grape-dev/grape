"""Issue #2423 M1-b：agent_tasks.env_extra 落库加密（C3 闭环）。

Seam（公开接口）：
- enqueue_task(env_extra) → agent_tasks.env_extra 列原文（enc:v1 信封，DB 查无明文）；
- get_task(task_id) 回读 → 解密还原 env dict（claim/worker 路径透明）；
- 存量明文行兼容（迁移期降级）+ 启动迁移 backfill 加密盖章。
"""
import json
import os
import sys
import uuid

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import service
from src.domains.runtime.engine.env_extra_security import (
    ENVELOPE_PREFIX, encrypt_env_extra, decrypt_env_extra,
)

REPO = "gitcode:owner/test-2423-envextra"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM session_api_tokens WHERE repo_key=?", (REPO,))
        db.commit()


def setup_function():
    _clean()


def teardown_function():
    _clean()


def _enqueue(env_extra=None, user_id=0):
    return service.enqueue_task(
        agent_session_id=f"ses-test-{uuid.uuid4().hex[:6]}", runtime_id=0,
        pool_agent_type="coding",
        repo_key=REPO, agent_type="coding", skill="coding-cli",
        prompt="test prompt", env_extra=env_extra or {}, user_id=user_id,
    )


def _raw_env_extra(task_id: str) -> str:
    with db_lock:
        row = db.cursor().execute(
            "SELECT env_extra FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    return row[0] if row else ""


# ---------- 信封单元（纯逻辑） ----------

def test_encrypt_decrypt_roundtrip():
    env = {"GRAPE_SESSION_TOKEN": "tk_xxx", "MY_API_KEY": "sek", "N": "1"}
    sealed = encrypt_env_extra(env)
    assert sealed.startswith(ENVELOPE_PREFIX)
    assert "tk_xxx" not in sealed and "sek" not in sealed
    assert decrypt_env_extra(sealed) == env


def test_decrypt_legacy_plaintext_compatible():
    legacy = json.dumps({"OLD": "v"})
    assert decrypt_env_extra(legacy) == {"OLD": "v"}
    assert decrypt_env_extra("") == {}
    assert decrypt_env_extra("not-json{") == {}
    assert decrypt_env_extra({"A": "1"}) == {"A": "1"}


# ---------- enqueue 落库 / get_task 回读 ----------

def test_enqueue_writes_encrypted_env_extra():
    tid = _enqueue({"MY_API_KEY": "sek-2423", "GRAPE_SESSION_TOKEN": "tk_2423"})
    raw = _raw_env_extra(tid)
    assert raw.startswith(ENVELOPE_PREFIX), "env_extra 落库必须是 enc 信封"
    assert "sek-2423" not in raw and "tk_2423" not in raw, "DB 原文不得含明文凭据"
    task = service.get_task(tid)
    assert task["env_extra"]
    env = task["env_extra"] if isinstance(task["env_extra"], dict) else task["env_extra"] if isinstance(task["env_extra"], dict) else json.loads(task["env_extra"])  # arch11：get_task 出口已解密
    assert env.get("MY_API_KEY") == "sek-2423"
    assert env.get("GRAPE_SESSION_TOKEN", "").startswith("tk_")


def test_get_task_returns_decrypted_env():
    tid = _enqueue({"K": "v"})
    task = service.get_task(tid)
    assert not str(task["env_extra"]).startswith(ENVELOPE_PREFIX)
    assert task["env_extra"] if isinstance(task["env_extra"], dict) else json.loads(task["env_extra"]).get("K") == "v"


# ---------- 启动迁移（存量明文 → 信封） ----------

def test_env_extra_migration_registered():
    from src.api import db_setup
    migs = {m.version: m for m in db_setup._MIGRATIONS}
    assert "202611200003" in migs
    assert migs["202611200003"].kind == "backfill"
    assert callable(migs["202611200003"].fn)
    assert migs["202611200003"].name == "issue2423-env-extra-encrypt-backfill"


def test_env_extra_migration_encrypts_existing_plaintext_rows():
    """存量明文行执行迁移 fn → enc 信封；已加密行不重复处理（幂等）。"""
    from src.api import db_setup
    legacy_id = f"t_legacy{uuid.uuid4().hex[:6]}"
    sealed_id = f"t_sealed{uuid.uuid4().hex[:6]}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, skill, prompt, env_extra, status) "
            "VALUES (?,?,?,?,?,?,?, ?, 'completed')",
            (legacy_id, f"ses-{legacy_id}", 0, REPO, "coding", "coding-cli", "p",
             json.dumps({"SECRET_AK": "legacy-ak"})))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, skill, prompt, env_extra, status) "
            "VALUES (?,?,?,?,?,?,?, ?, 'completed')",
            (sealed_id, f"ses-{sealed_id}", 0, REPO, "coding", "coding-cli", "p",
             encrypt_env_extra({"SECRET_AK": "sealed-ak"})))
        db.commit()
    try:
        db_setup._fn_migrate_env_extra_encryption_2423()  # arch11 注册名带 _2423 后缀
        raw_legacy = _raw_env_extra(legacy_id)
        assert raw_legacy.startswith(ENVELOPE_PREFIX)
        assert "legacy-ak" not in raw_legacy
        assert decrypt_env_extra(raw_legacy) == {"SECRET_AK": "legacy-ak"}
        raw_sealed = _raw_env_extra(sealed_id)
        assert raw_sealed.startswith(ENVELOPE_PREFIX)
        assert decrypt_env_extra(raw_sealed) == {"SECRET_AK": "sealed-ak"}
    finally:
        _clean()
