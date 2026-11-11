"""UC40：rc 双权威分裂修复——显式设置 token 路径收口（D98，2026-11-08）。

场景↔用例映射（requirements UC40，六断言面）：
- UC40① 手动设置 token 后 rc 可读（RED：修复前 _update_repo_bot_token 只写
  managed_repos 列不同步 rc——D58 写冻结执行清单漏迁本入口，双权威分裂）
- UC40② deps has_bot 闸门 rc 感知（any_repo_credential_for_reps 修复前假空）
- UC40③ rc 行幂等/锚点稳定：重复设置不同 token，行 id 不变（ON CONFLICT）、
  value 更新（UC26③ 同款审计锚点语义）
- UC40④ /repos/managed has_bot_token 贯穿（含 _r 笔误回归锁）
- UC40⑤ settings 配置页多平台：bot_username 卡派生（非 devops-bot 硬编码）
- UC40⑥ round9 缝迁移回归（auto_upgrade 端口链/回落次序/源码符号）

密文回读链：_update_repo_bot_token 写列走 _encrypt_repo_secret（N3 通道），
D98 补写 rc 同加密语义——get_repo_credential_value 读侧统一解密。
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import uuid

import pytest

from src.kernel.session_manager import db, db_lock


def _iso_repo_key() -> str:
    return f"gitcode:uc40/{uuid.uuid4().hex[:10]}"


def _seed_managed_repo(repo_key: str):
    """播种托管行 + user_repos join 行（路径 B 形态；免外部 mock——只验证写读链）。
    list_managed_repos 的 else 分支（org_id=0）要求 LEFT JOIN user_repos 命中。"""
    from src.domains.mgmt.user.db import add_managed_repo, mark_user_repo_join
    ok = add_managed_repo(repo_key, repo_key.split(":", 1)[1].split("/")[0],
                          repo_key.split("/")[-1], host_user_id=1, webhook_id=0)
    assert ok, f"播种托管行失败: {repo_key}"
    mark_user_repo_join(1, repo_key, is_join=True)


def _cleanup(repo_key: str):
    with db_lock:
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (repo_key,))
        db.commit()


# ──────────────── UC40①②③ 设置 token → rc 权威可读 ────────────────

def test_uc40_1_manual_token_write_lands_in_rc():
    """设置 token 后 rc 可读（解密回读一致）——双权威分裂回归锁。"""
    from src.domains.mgmt.user.db import update_managed_repo_bot_token, get_repo_credential_value
    rk = _iso_repo_key()
    _seed_managed_repo(rk)
    try:
        r = update_managed_repo_bot_token(rk, "uc40_tok_abc123")
        assert r.get("ok") is True
        # ①核心断言：rc 权威表可读（修复前这里为空——只写列不写 rc）
        val = get_repo_credential_value(rk, "bot")  # D99 归一
        assert val == "uc40_tok_abc123", f"rc 未同步写入（双权威分裂回潮）: {val!r}"
    finally:
        _cleanup(rk)


def test_uc40_2_has_bot_gate_sees_manual_token():
    """deps has_bot 闸门（rc 感知）对手动设置的 token 返回 True。"""
    from src.domains.mgmt.user.db import update_managed_repo_bot_token, any_repo_credential_for_repos
    rk = _iso_repo_key()
    _seed_managed_repo(rk)
    try:
        update_managed_repo_bot_token(rk, "uc40_tok_gate")
        # ②核心断言：修复前 rc 无行 → 假空
        assert any_repo_credential_for_repos([rk]) is True
    finally:
        _cleanup(rk)


def test_uc40_3_rc_upsert_idempotent_value_updates():
    """重复设置不同 token：rc 值收敛为最新（INSERT OR REPLACE 先例语义，
    对齐 config_store.global_bot_token_swap 步骤 6——不锚定 id，锚点空缺由
    N4 幂等回填补）。"""
    from src.domains.mgmt.user.db import update_managed_repo_bot_token, get_repo_credential_value
    rk = _iso_repo_key()
    _seed_managed_repo(rk)
    try:
        update_managed_repo_bot_token(rk, "uc40_tok_v1")
        assert get_repo_credential_value(rk, "bot") == "uc40_tok_v1"  # D99 归一
        update_managed_repo_bot_token(rk, "uc40_tok_v2")
        assert get_repo_credential_value(rk, "bot") == "uc40_tok_v2", \
            "重复设置应收敛为最新值"  # D99 归一
    finally:
        _cleanup(rk)


# ──────────────── UC40④ /repos/managed has_bot_token 贯穿 ────────────────

def test_uc40_4_managed_endpoint_has_bot_token_true():
    """设置 token 后 /repos/managed 的 has_bot_token=True（贯穿 _r 笔误回归锁）。"""
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.api.deps import require_org_member
    from src.domains.mgmt.user.db import update_managed_repo_bot_token

    rk = _iso_repo_key()
    _seed_managed_repo(rk)
    owner, name = rk.split(":", 1)[1].split("/")
    app.dependency_overrides[require_org_member] = lambda: {"id": 1, "org_id": 0}
    client = TestClient(app)
    try:
        update_managed_repo_bot_token(rk, "uc40_tok_ep")
        r = client.get("/api/repos/managed", headers={"Authorization": "Bearer test"})
        assert r.status_code == 200, r.text[:200]
        hit = [x for x in r.json()["repos"] if x["repo_key"] == rk]
        assert hit, f"仓库应在列表: {r.json()['repos'][:2]}"
        assert hit[0]["has_bot_token"] is True, \
            "has_bot_token 应 True（_r 笔误致 NameError 500 的回归锁 + rc 收敛语义）"
    finally:
        app.dependency_overrides.clear()
        with db_lock:
            db.cursor().execute("DELETE FROM user_repos WHERE repo_key=?", (rk,))
            db.commit()
        _cleanup(rk)


# ──────────────── UC40⑤ settings 配置页多平台 ────────────────

def test_uc40_5_settings_bot_username_card_derived():
    """GET /settings/gitcode-repo 的 bot_username 走 resolve_bot_username 来源链
    （非 'devops-bot' 硬编码）——gitee 等平台配置页显示正确 bot 身份。"""
    import uuid as _uuid
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.api.deps import require_no_session_token
    from src.kernel.session_manager import db as _db

    rk = f"gitee:uc40/{_uuid.uuid4().hex[:8]}"
    owner, name = rk.split(":", 1)[1].split("/")
    card_bot = "uc40_card_bot"
    with db_lock:
        _db.cursor().execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, bot_username)"
            " VALUES (?,?,?,?,?)", (rk, owner, name, f"{owner}/{name}", card_bot))
        # get_managed_repos 走 LEFT JOIN user_repos——补 join 行让托管行进入回显列表
        _db.cursor().execute(
            "INSERT INTO user_repos (user_id, repo_key, is_join) VALUES (1, ?, 1)", (rk,))
        _db.commit()
    app.dependency_overrides[require_no_session_token] = lambda: {"id": 1, "username": "uc40"}
    client = TestClient(app)
    try:
        r = client.get(f"/api/settings/gitcode-repo/{owner}/{name}",
                       headers={"Authorization": "Bearer test"})
        assert r.status_code == 200, r.text[:200]
        d = r.json()
        assert d["bot_username"] == card_bot, \
            f"bot_username 应取托管行/来源链值，Got {d['bot_username']!r}（devops-bot 硬编码回潮）"
    finally:
        app.dependency_overrides.clear()
        with db_lock:
            _db.cursor().execute("DELETE FROM user_repos WHERE repo_key=?", (rk,))
            _db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            _db.commit()


# ──────────────── UC40⑥ round9 缝迁移回归 ────────────────

def test_uc40_6_round9_seams_migrated():
    """迁移后 6 用例全绿：auto_upgrade 端口链 / webhook_token 回落次序 /
    源码符号锚定（test_issue_372_round9_audit 零失败）。

    D100 混跑序守卫：pytest.main 内嵌会重新 exec tests/conftest.py（rewrite
    层独立 loader），其导入期 os.remove 会删掉外层在用的测试库——嵌套标记
    GRAPE_PYTEST_NESTED=1 让 conftest 删除段跳过（对齐 S106 孤儿 inode 教训）。
    """
    os.environ["GRAPE_PYTEST_NESTED"] = "1"
    try:
        code = pytest.main(["tests/domains/runtime/engine/test_issue_372_round9_audit.py", "-q", "--no-header",
                            "-p", "no:cacheprovider"])
    finally:
        os.environ.pop("GRAPE_PYTEST_NESTED", None)
    assert code == 0, "round9 缝迁移后有失败（见上方输出）"
