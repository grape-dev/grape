"""[M1 E2E 引擎 30 空壳根因守卫] 注册 INSERT 分支必须首插即写云元数据。

根因（2026-11-15 M1 E2E 实证，引擎 30）：register 的 INSERT 分支（新引擎行）
原来不含 runtime_info/provider/runtime_type 三列 → 行落列默认 local/'{}'/''；
worker 侧 _refresh_templates 的 hash-diff 下一轮可能判定「未变化」跳过注册，
空壳永不回填 → 认领时 build_context 读 provider='' 误判本地引擎 →
"dispatch_execution only for cloud sandbox, got BwrapSandbox"。

守卫：模拟 cloud_worker 首次注册（全新凭据+全新引擎名），
断言首插后行内 runtime_type=cloud / provider=凭据名 / runtime_info 非空。
"""
import hashlib
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi.testclient import TestClient  # noqa: E402


def _client():
    os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")
    from src.api.grape_worker_server import app
    return TestClient(app)


def _insert_credential(cursor, lock, uid, token):
    with lock:
        cursor.execute("DELETE FROM agent_runtimes WHERE credential_id IN "
                       "(SELECT id FROM runtime_credentials WHERE credential_uid=?)", (uid,))
        cursor.execute("DELETE FROM runtime_credentials WHERE credential_uid=?", (uid,))
        cursor.execute(
            "INSERT INTO runtime_credentials (token_hash, name, scope, owner_name, status, "
            "credential_uid, cred_type, platform) VALUES (?,?,?,?, 'active', ?, 'sandbox', 'cubesandbox')",
            (hashlib.sha256(token.encode()).hexdigest(), f"sbx-{uid[:8]}", uid, "user", "tester"))
        cid = cursor.lastrowid
    return cid


def test_register_insert_branch_writes_cloud_metadata():
    c = _client()
    from src.kernel.session_manager import db, db_lock
    uid = f"repro-guard-{uuid.uuid4().hex[:8]}"
    token = f"rta_{uuid.uuid4().hex}"
    with db_lock:
        d = db.cursor()
        cred_id = _insert_credential(d, db_lock, uid, token)
        db.commit()

    prov_name = f"sbx-{uid[:8]}"
    engine_name = f"opencode:tpl-x:{prov_name}:2"
    ri = json.dumps({"engine_type": "opencode", "platform": "cubesandbox",
                     "provider_id": 2, "provider_name": prov_name,
                     "template_id": "tpl-x", "template_db_id": 6})
    payload = {"device_name": "guard-dev", "cli_version": "cw", "runtimes": [{
        "name": engine_name, "version": "", "healthy": True, "installed": True,
        "cli_path": "", "provider": prov_name, "runtime_type": "cloud",
        "runtime_info": ri, "agent_tags": []}]}
    try:
        r = c.post("/api/runtime/register", json=payload,
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text[:200]
        with db_lock:
            d = db.cursor()
            d.execute("SELECT runtime_type, provider, runtime_info FROM agent_runtimes "
                       "WHERE credential_id=? AND name=?", (cred_id, engine_name))
            row = d.fetchone()
        assert row, "首插引擎行应存在"
        assert row[0] == "cloud", f"runtime_type 应首插即 cloud，实际 {row[0]!r}（空壳回归）"
        assert row[1] == prov_name, f"provider 应为 {prov_name!r}，实际 {row[1]!r}"
        assert json.loads(row[2] or "{}").get("platform") == "cubesandbox", \
            f"runtime_info 应含平台元数据，实际 {row[2][:80]!r}"
    finally:
        with db_lock:
            d = db.cursor()
            d.execute("DELETE FROM agent_runtimes WHERE credential_id=?", (cred_id,))
            d.execute("DELETE FROM runtime_credentials WHERE id=?", (cred_id,))
            db.commit()
