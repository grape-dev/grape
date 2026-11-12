"""UC42：D100 凭据类型注册表 + grape_t_system_credentials 门面（批 1）。

场景↔用例映射（方案稿 docs/weekly-09/d100-credential-type-schema-design.md §九）：
- UC42① 注册表守卫：未登记 provider key 写入被拒+告警（RED 先行语义：
  守卫先行于消费接线，本批 sc 侧硬拒、uc 侧 warning 期由 set_credential
  接线时落——本用例锁 sc 硬拒 + 注册表判定函数行为）
- UC42② PAT 经注册表写入读出等价（改造前后行为等价——registry lookup
  对三平台 PAT/动态 PAT/云裸名的判定正确性）
- UC42③ OAuth 快照清退：W1 双写消灭 / R1 改道（独立用例文件覆盖 oauth
  域——本文件仅锁 registry deprecated 判定，行为清退见 test_uc42b）
- UC42④ upc 退役（同上，见 test_uc42b）
- UC42⑤ rc/uc/sc 分域互斥：一个 key 只允许出现在一个 scope 的值域
- UC42⑥ sc 门面批 1 全链：CRUD/admin 闸门/加密落库/use 审计三断言/
  resolve 回落/唯一约束/注册表未登记拒绝（冒烟十项的固化形态）
"""
import os

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import json
import uuid

import pytest

from src.kernel.session_manager import db, db_lock

# 探针 admin（挂真实 admin_accounts 行，防孤儿清理误删真实账号——
# 测试夹具清理与断言隔离纪律）
_PROBE_ADMIN_ID = 990001


def _mk_probe_admin():
    with db_lock:
        db.execute(
            "INSERT OR REPLACE INTO admin_accounts (id, username, password_hash, token_version) "
            "VALUES (?,?,?,?)", (_PROBE_ADMIN_ID, "uc42_probe_admin", "x", 1))
        db.commit()


def _cleanup_probe_admin():
    with db_lock:
        db.execute("DELETE FROM admin_accounts WHERE id=?", (_PROBE_ADMIN_ID,))
        db.commit()


# ──────────────── UC42① 注册表守卫 ────────────────

def test_uc42_1_registry_lookup_and_reject():
    """注册表判定：已登记命中 / 未登记 None / sc 门面对未登记 key 硬拒。"""
    from src.domains.mgmt.user.credential_registry import lookup
    from src.domains.mgmt.user import system_credentials as sc

    # 已登记（三段各抽一）
    assert lookup("huawei_cloud") is not None
    assert lookup("gitcode_pat") is not None
    assert lookup("bot") is not None
    assert lookup("oauth_client") is not None
    # 未登记
    assert lookup("nonexistent_key") is None
    assert lookup("") is None
    # sc 门面硬拒（sc 无存量负担，无 warning 期——方案稿 §7.3 消费点 3）
    _mk_probe_admin()
    try:
        with pytest.raises(ValueError, match="unknown system credential type"):
            sc.create("bogus_type", "v", operator_id=_PROBE_ADMIN_ID)
    finally:
        _cleanup_probe_admin()


def test_uc42_1b_dynamic_pat_via_platform_card():
    """动态 PAT 规则匹配：卡在册平台 <p>_pat 自动合法（新平台接入零注册表改动）。"""
    from src.domains.mgmt.user.credential_registry import lookup, is_deprecated
    # 内置三平台显式登记
    assert lookup("gitcode_pat") is not None
    # 动态匹配路径：平台卡在册即合法（gitee 也在内置清单；此处验证规则函数本身）
    from src.domains.mgmt.user.credential_registry import _is_dynamic_pat
    assert _is_dynamic_pat("gitcode_pat") is True
    assert _is_dynamic_pat("nonexistent_platform_pat") is False
    assert _is_dynamic_pat("huawei_cloud") is False  # 非 _pat 形态不匹配
    # deprecated 判定：<p>_oauth 清退值域
    assert is_deprecated("gitcode_oauth") is True
    assert is_deprecated("gitcode_pat") is False


def test_uc42_1c_sensitive_fields_and_storage_mode():
    """注册表精化（业界对标收口）：sensitive 标记路由 + storage_mode 形态位。

    - sensitive 标记驱动脱敏（不靠字段名猜）：oauth_client 的 client_id
      非敏感可明文记录 / client_secret 敏感只可指纹
    - storage_mode 三形态：现全量 encrypted（出示型）；hashed/external_ref
      为形态位预留（验证型/闸门③），零消费方零迁移
    """
    from src.domains.mgmt.user.credential_registry import (
        sensitive_fields, storage_mode_of, lookup)
    # sensitive 路由：client_id 非敏感 / client_secret 敏感
    assert sensitive_fields("oauth_client") == ("client_secret",)
    # 全敏感型：huawei_cloud ak/sk 均敏感
    assert sensitive_fields("huawei_cloud") == ("ak", "sk")
    # 单值类型/未登记：空（调用方按全敏感兜底）
    assert sensitive_fields("vlm_api_key") == ()
    assert sensitive_fields("nonexistent") == ()
    # storage_mode：现全量出示型 encrypted
    for key in ("gitcode_pat", "huawei_cloud", "oauth_client", "obs_aksk"):
        assert storage_mode_of(key) == "encrypted", key
    # spec 字段面：storage_mode 在册（形态位已在类型契约中）
    spec = lookup("gitcode_pat")
    assert spec.storage_mode == "encrypted"
    assert spec.fields == (("token", True, True),)  # 三元组 (name, required, sensitive)


# ──────────────── UC42② PAT 注册表等价 ────────────────

def test_uc42_2_pat_registry_equivalence():
    """PAT 三平台经注册表 fields 约定校验等价（token 必填，多余字段不拒）。"""
    from src.domains.mgmt.user.credential_registry import validate_fields, lookup
    for p in ("gitcode", "github", "gitee"):
        spec = lookup(f"{p}_pat")
        assert spec is not None and spec.scope == "user"
        ok, missing = validate_fields(f"{p}_pat", {"token": "tok"})
        assert ok and not missing
        ok2, missing2 = validate_fields(f"{p}_pat", {})
        assert not ok2 and missing2 == ("token",)
        # 多余字段不拒（config 开放 dict）
        ok3, _ = validate_fields(f"{p}_pat", {"token": "t", "note": "x"})
        assert ok3


# ──────────────── UC42⑤ 分域互斥 ────────────────

def test_uc42_5_scope_mutual_exclusion():
    """一个 key 只允许出现在一个 scope 值域（rc bot/webhook ≠ uc provider ≠ sc cred_type）。"""
    from src.domains.mgmt.user.credential_registry import SPECS, lookup
    keys = [s.key for s in SPECS]
    # 无重复 key
    assert len(keys) == len(set(keys)), f"registry duplicate keys: {keys}"
    # sc 门面拒绝非 system 段 key（跨域写入在 sc 入口被分域校验拦下）
    from src.domains.mgmt.user import system_credentials as sc
    _mk_probe_admin()
    try:
        with pytest.raises(ValueError, match="belongs to scope"):
            sc.create("huawei_cloud", {"ak": "a", "sk": "b"}, operator_id=_PROBE_ADMIN_ID)
        with pytest.raises(ValueError, match="belongs to scope"):
            sc.create("bot", "x", operator_id=_PROBE_ADMIN_ID)
    finally:
        _cleanup_probe_admin()


# ──────────────── UC42⑥ sc 门面批 1 全链 ────────────────

@pytest.fixture()
def probe_admin():
    _mk_probe_admin()
    yield
    # 凭据行清理（探针类型 test_probe 专属，不触碰其他数据）
    with db_lock:
        db.execute("DELETE FROM grape_t_system_credentials WHERE cred_type='test_probe'")
        db.execute("DELETE FROM audit_logs WHERE event_type LIKE 'system_credential%' "
                   "AND operator_id=?", (_PROBE_ADMIN_ID,))
        db.commit()
        _cleanup_probe_admin()


def test_uc42_6_crud_admin_gate_and_encrypt_at_rest(probe_admin):
    """CRUD 贯穿 + admin 闸门（非 admin PermissionError）+ 库内行非明文。"""
    from src.domains.mgmt.user import system_credentials as sc
    # 非 admin 拒绝（fail-closed）
    with pytest.raises(PermissionError):
        sc.create("test_probe", "sec_val", operator_id=999999)
    with pytest.raises(PermissionError):
        sc.update("test_probe", "sec_val", operator_id=0)
    # create → 库内行非明文（密文落库断言）
    rid = sc.create("test_probe", "sec_val", operator_id=_PROBE_ADMIN_ID)
    with db_lock:
        raw = db.execute("SELECT value, created_at, updated_at FROM grape_t_system_credentials "
                         "WHERE id=?", (rid,)).fetchone()
    assert raw is not None
    assert raw[0] != "sec_val" and ":" in raw[0], f"库内非密文: {raw[0][:20]}"
    # 时间列门面赋值（NOT NULL 无 DEFAULT 的行为锁）
    assert raw[1] and raw[2], "created_at/updated_at 未被门面赋值"
    # get 解密回读
    dto = sc.get("test_probe")
    assert dto.value == "sec_val" and dto.id == rid


def test_uc42_6_create_update_semantics(probe_admin):
    """create/update 分离语义：duplicate create 拒 / missing update 拒 /
    轮换后 id/created_at 稳定（审计锚点）。"""
    from src.domains.mgmt.user import system_credentials as sc
    rid = sc.create("test_probe", "v1", operator_id=_PROBE_ADMIN_ID)
    with pytest.raises(ValueError, match="already exists"):
        sc.create("test_probe", "v2", operator_id=_PROBE_ADMIN_ID)
    # 未登记 key：先撞注册表守卫（分域校验在先）；用已登记但未建行的 key
    # 验证 update 的 not found 分支
    with pytest.raises(ValueError, match="unknown system credential type"):
        sc.update("never_created_probe", "v", operator_id=_PROBE_ADMIN_ID)
    with pytest.raises(ValueError, match="not found"):
        sc.update("oauth_client", "v", operator_id=_PROBE_ADMIN_ID)
    with db_lock:
        created_at = db.execute("SELECT created_at FROM grape_t_system_credentials WHERE id=?",
                                (rid,)).fetchone()[0]
    sc.update("test_probe", "v2_rotated", operator_id=_PROBE_ADMIN_ID)
    dto = sc.get("test_probe")
    assert dto.value == "v2_rotated"
    assert dto.id == rid and dto.created_at == created_at  # 审计锚点稳定


def test_uc42_6_use_audit_triple(probe_admin):
    """use 入口审计三断言：解密等值 + auth_audit 变更链 + usage_log 消费行。"""
    import time
    from src.domains.mgmt.user import system_credentials as sc
    rid = sc.create("test_probe", "audited_val", operator_id=_PROBE_ADMIN_ID)
    sc.update("test_probe", "audited_val_v2", operator_id=_PROBE_ADMIN_ID)
    dto = sc.use("test_probe", actor_id=42, executor_context="uc42")
    # ① 解密等值
    assert dto.value == "audited_val_v2" and dto.id == rid
    # ② auth_audit 变更链（create+update 各一条，operator 可归因）
    with db_lock:
        evs = db.execute(
            "SELECT event_type, COUNT(*) FROM audit_logs "
            "WHERE event_type LIKE 'system_credential%' AND operator_id=? "
            "GROUP BY event_type", (_PROBE_ADMIN_ID,)).fetchall()
    ev_map = dict(evs)
    assert ev_map.get("system_credential_create") == 1
    assert ev_map.get("system_credential_update") == 1
    # detail 不落明文（值指纹形态——尾4位）
    with db_lock:
        detail = db.execute(
            "SELECT detail FROM audit_logs WHERE event_type='system_credential_update' "
            "AND operator_id=? ORDER BY id DESC LIMIT 1", (_PROBE_ADMIN_ID,)).fetchone()[0]
    assert "audited_val_v2" not in detail, "审计 detail 泄露明文"
    # ③ usage_log 消费行（异步队列最终一致，轮询 5s）
    deadline = time.time() + 5
    urows = []
    while time.time() < deadline:
        with db_lock:
            urows = db.execute(
                "SELECT credential_kind, operation, initiating_actor_id, executor_context "
                "FROM grape_t_credential_usage_log WHERE credential_kind='system_credentials' "
                "AND credential_id=?", (rid,)).fetchall()
        if urows:
            break
        time.sleep(0.1)
    assert urows, "usage_log 消费审计未落库（5s 内）"
    assert urows[0][0] == "system_credentials"  # credential_kind 值域（D100 标签，不随表名改）
    assert urows[0][1] == "system_cred_use:test_probe"
    # D105 第二批 TEXT 化后 initiating_actor_id 落库/回读恒 str 形态（'42'）——
    # 断言随迁移口径对齐（原 int 比较是 TEXT 迁移漏网，评审 D105-16 审计发现）
    assert urows[0][2] == "42" and urows[0][3] == "uc42"


def test_uc42_6_resolve_fallback_and_delete(probe_admin):
    """resolve 双路：DB 命中即权威；DB 空走 env fallback；delete 后自动回 env
    （回滚=删行零代码变更的机制锁）。"""
    import os as _os
    from src.domains.mgmt.user import system_credentials as sc
    _os.environ["VLM_API_KEY"] = "env_val_uc42"
    try:
        # DB 空 → env 回落
        assert sc.resolve("vlm_api_key", actor_id=42) == "env_val_uc42"
        # DB 命中 → 权威（env 被遮蔽）
        rid = sc.create("vlm_api_key", "db_authoritative_val",
                        operator_id=_PROBE_ADMIN_ID)
        assert sc.resolve("vlm_api_key", actor_id=42) == "db_authoritative_val"
        # 删行 → 自动回 env（回滚语义）
        assert sc.delete("vlm_api_key", operator_id=_PROBE_ADMIN_ID) is True
        assert sc.resolve("vlm_api_key", actor_id=42) == "env_val_uc42"
        # delete 幂等（不存在=False）
        assert sc.delete("vlm_api_key", operator_id=_PROBE_ADMIN_ID) is False
    finally:
        _os.environ.pop("VLM_API_KEY", None)
        with db_lock:
            db.execute("DELETE FROM grape_t_system_credentials WHERE cred_type='vlm_api_key'")
            db.commit()


def test_uc42_6_fields_validation():
    """dict 型 key 的 fields 约定：缺必填拒 / 满足过 / 单值类型恒过。"""
    from src.domains.mgmt.user import system_credentials as sc
    _mk_probe_admin()
    try:
        # obs_aksk fields=(ak,sk)：缺 sk 拒
        with pytest.raises(ValueError, match="missing required fields"):
            sc.create("obs_aksk", {"ak": "only_ak"}, operator_id=_PROBE_ADMIN_ID)
        # 满足则建行成功（dict 整体序列化加密，同 uc config_json 先例）
        rid = sc.create("obs_aksk", {"ak": "ak1", "sk": "sk1"},
                        operator_id=_PROBE_ADMIN_ID)
        dto = sc.get("obs_aksk")
        assert dto.value == {"ak": "ak1", "sk": "sk1"}  # dict 还原
        # 单值类型（test_probe）恒过 fields 校验
        rid2 = sc.create("test_probe", "plain_str_val", operator_id=_PROBE_ADMIN_ID)
        assert sc.get("test_probe").value == "plain_str_val"
    finally:
        with db_lock:
            db.execute("DELETE FROM grape_t_system_credentials WHERE cred_type IN "
                       "('obs_aksk','test_probe')")
            db.commit()
            _cleanup_probe_admin()


def test_uc42_6_unique_constraint_scope_id_empty():
    """UNIQUE(scope_type, scope_id, cred_type) 在 scope_id='' 下真正生效
    （MySQL UNIQUE 对 NULL 互不相等——'' 保证约束生效，方案稿 §1.3）。"""
    from src.domains.mgmt.user import system_credentials as sc
    _mk_probe_admin()
    try:
        rid = sc.create("test_probe", "v1", operator_id=_PROBE_ADMIN_ID)
        with db_lock:
            row = db.execute("SELECT scope_type, scope_id FROM grape_t_system_credentials "
                             "WHERE id=?", (rid,)).fetchone()
        assert row == ("system", ""), "scope 形态偏离（应为 system/''）"
        # 同 (system, '', test_probe) 二次 INSERT（绕过门面直插）被 UNIQUE 拒
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            with db_lock:
                db.execute(
                    "INSERT INTO grape_t_system_credentials (id, scope_type, scope_id, "
                    "cred_type, value, created_at, updated_at) "
                    "VALUES (?, 'system', '', 'test_probe', 'x', 't', 't')",
                    (str(uuid.uuid4()),))
                db.commit()
    finally:
        with db_lock:
            db.execute("DELETE FROM grape_t_system_credentials WHERE cred_type='test_probe'")
            db.commit()
            _cleanup_probe_admin()
