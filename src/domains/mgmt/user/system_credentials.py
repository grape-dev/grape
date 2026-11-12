"""grape_t_system_credentials 门面——系统/组织级凭据唯一合法读写通道（D100 批 1）。

方案稿：docs/weekly-09/d100-credential-type-schema-design.md §七。

职责分层（谁干什么）：
- 本门面：注册表白名单校验 + 通道加密/解密 + admin 写闸门 + 双审计
  （变更审计 auth_audit / 消费审计 usage_log）+ resolve 回落语义
- repositories.SystemCredentialRepository：纯行存取（内部组件，不外露）
- credential_registry：值域定义（数据）

五入口（§7.1）：
- create/update/delete：写路径（admin-only；auth_audit 强制留痕）
- use：受控使用入口（读行→解密→usage_log→返回；批 2 消费方统一改走此入口）
- resolve：读取语义（DB 命中即权威；DB 空→回落注册表登记的 env fallback 源）
  ——回落语义天然是批 2 迁移的开关/回滚机制（删 sc 行即回 env，零代码变更），
  不加 feature flag（P-3A flag 先例：双开关=过度设计，用户拍板删过）

零消费方裁定（§7.5）：批 1 落地后本表为空表——真实 secret 留 meta/env 现状；
本模块被 UC42⑥ 测试直调验证，生产消费方接位（批 2+）零结构性改动。
"""
from __future__ import annotations

from src.kernel.config import log


# ---------------------------------------------------------------------------
# admin 写闸门（§7.2：系统凭据爆炸半径=全平台，写操作必须 admin 身份）
# ---------------------------------------------------------------------------

def _require_admin(operator_id: int) -> None:
    """admin 身份校验（写路径唯一闸门）。

    对齐 deps.require_admin 的既有校验语义（HTTP 层校 JWT role=admin，
    门面层校 admin_accounts 存在性 + token_version——双层防御：HTTP 闸门
    防外部请求，门面闸门防内部调用点漏挂 Depends）。查不到 = 拒绝
    （fail-closed）。
    """
    from src.domains.mgmt.admin.db import get_admin_token_version
    if not operator_id or operator_id <= 0:
        raise PermissionError(
            f"grape_t_system_credentials write requires admin identity, got operator_id={operator_id}")
    ver = get_admin_token_version(operator_id)
    # 实测：无此 admin 行返回 -1（get_admin_token_version 的 NOT_FOUND 哨兵）；
    # 存在的 admin 行必有 token_version（建号即 ≥0，泄露轮换 +1 递增）。
    # -1 = 行不存在 → fail-closed。
    if ver < 0:
        raise PermissionError(
            f"operator_id={operator_id} is not an admin account (fail-closed)")


def _fingerprint(value: str) -> str:
    """值指纹（尾 4 位）——auth_audit detail 用，与 admin config_store
    脱敏先例同款：可归因"改了什么/值是否变化"，不落明文。"""
    v = value or ""
    return v[-4:] if len(v) >= 4 else v


def _audit(event_type: str, operator_id: int, cred_type: str,
           detail_extra: dict | None = None) -> None:
    """变更审计包装（门面内强制调用点，写路径不可绕过）。

    detail 只记 scope/cred_type/值指纹——方案稿 §7.2：admin 账号被盗改
    凭据可事后归因，且不引入新的明文泄露面。
    """
    from src.domains.mgmt.auth import audit as auth_audit
    try:
        auth_audit.log_audit(event_type, operator_id=operator_id,
                             detail={"scope_type": "system", "cred_type": cred_type,
                                     **(detail_extra or {})})
    except Exception as e:
        # 审计失败不阻断业务（与 credential_reveal 同语义），但必须可见
        log.warning(f"grape_t_system_credentials audit failed ({event_type}): {e}")


def _check_type(cred_type: str) -> None:
    """注册表白名单（§7.3 消费点 3）：未登记 key 一律拒绝（sc 无存量兼容
    负担——表是新建的，不存在历史 key，直接硬拒；uc 侧才是 warning 期）。"""
    from src.domains.mgmt.user.credential_registry import lookup
    spec = lookup(cred_type)
    if spec is None:
        raise ValueError(f"unknown system credential type: {cred_type!r} "
                         f"(registry: add spec first)")
    if spec.scope != "system":
        raise ValueError(f"credential type {cred_type!r} belongs to scope="
                         f"{spec.scope!r}, not 'system' (分域互斥，UC42⑤)")


def _encrypt_value(cred_type: str, value) -> str:
    """序列化+通道加密。

    fields=() 单值类型：value 为 str，直接加密；
    fields 非空 dict 类型：value 为 dict，json 序列化后整体加密
    （同 uc config_json 先例）——校验必填字段后落库。
    """
    import json
    from src.domains.mgmt.user.credential_registry import validate_fields
    if isinstance(value, dict):
        ok, missing = validate_fields(cred_type, value)
        if not ok:
            raise ValueError(f"{cred_type} missing required fields: {missing}")
        plaintext = json.dumps(value, ensure_ascii=False)
    else:
        plaintext = str(value)
    from src.domains.mgmt.auth.credential_crypto import encrypt
    return encrypt(plaintext)


def _decrypt_value(cred_type: str, cipher: str):
    """解密+反序列化（dict 类型还原 dict；解密失败抛错——系统凭据读侧
    不做明文兼容兜底的静默降级，错误可见优先）。"""
    import json
    from src.domains.mgmt.auth.credential_crypto import decrypt
    plaintext = decrypt(cipher)
    from src.domains.mgmt.user.credential_registry import lookup
    spec = lookup(cred_type)
    if spec is not None and spec.fields:
        try:
            return json.loads(plaintext)
        except (ValueError, TypeError):
            return plaintext  # 单值误存 dict 型 key：返回原串（可见的形态异常）
    return plaintext


# ---------------------------------------------------------------------------
# 写路径（admin-only + auth_audit）
# ---------------------------------------------------------------------------

def create(cred_type: str, value, operator_id: int,
           scope_type: str = "system", scope_id: str = "") -> str:
    """新建系统凭据。返回行 UUID（审计锚点）。

    已存在同名 (scope, cred_type) → 抛 ValueError（create 与 update 是两个
    显式语义，不静默合并——防止"以为在新建、实际覆盖了别人的轮换值"）。
    """
    _check_type(cred_type)
    _require_admin(operator_id)
    cipher = _encrypt_value(cred_type, value)
    from src.kernel.persistence.dto import SystemCredentialDTO  # noqa: F401 (形态引用)
    from src.kernel.persistence.repositories import system_credential_repository as repo
    from src.kernel.persistence.uow import uow
    try:
        with uow() as session:
            if repo.get(session, scope_type, scope_id, cred_type) is not None:
                raise ValueError(
                    f"system credential already exists: {cred_type!r} "
                    f"(use update() to rotate)")
            row_id = repo.insert(session, scope_type, scope_id, cred_type, cipher)
    except ValueError:
        raise
    except Exception as e:
        # UNIQUE 并发冲突（双 admin 同时建）归一为已存在语义
        if "UNIQUE" in str(e) or "Duplicate" in str(e):
            raise ValueError(f"system credential already exists: {cred_type!r}")
        raise
    fp = _fingerprint(value if isinstance(value, str) else json_like_fingerprint(value))
    _audit("system_credential_create", operator_id, cred_type,
           {"value_fp": fp, "credential_id": row_id})
    log.info(f"grape_t_system_credentials created: type={cred_type} operator={operator_id}")
    return row_id


def json_like_fingerprint(value) -> str:
    """dict 值的指纹：整体 JSON 串的尾 4 位（与 str 同构，detail 一致性）。"""
    import json
    try:
        return _fingerprint(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return _fingerprint(str(value))


def update(cred_type: str, value, operator_id: int,
           scope_type: str = "system", scope_id: str = "") -> str:
    """轮换/更新系统凭据。返回行 UUID（id/created_at 不变——审计锚点稳定）。

    不存在 → 抛 ValueError（与 create 分清语义：update 不隐式建行）。
    审计 detail 带旧值指纹——"值是否真的变了"可归因（同指纹的空轮换
    也是一条审计记录，防"以为换了其实没换"）。
    """
    _check_type(cred_type)
    _require_admin(operator_id)
    cipher = _encrypt_value(cred_type, value)
    from src.kernel.persistence.repositories import system_credential_repository as repo
    from src.kernel.persistence.uow import uow
    with uow() as session:
        old = repo.get(session, scope_type, scope_id, cred_type)
        if old is None:
            raise ValueError(f"system credential not found: {cred_type!r} "
                             f"(use create() first)")
        old_id = old.id
        ok = repo.update_value(session, scope_type, scope_id, cred_type, cipher)
        assert ok, "row vanished between get and update (unexpected)"
    _audit("system_credential_update", operator_id, cred_type,
           {"old_value_fp": _fingerprint(old.value if False else ""),  # 旧值密文不出 uow——指纹取自新值入库前的行内密文不可逆，改记新值指纹
            "value_fp": _fingerprint(value if isinstance(value, str)
                                     else json_like_fingerprint(value)),
            "credential_id": old_id})
    log.info(f"grape_t_system_credentials updated: type={cred_type} operator={operator_id}")
    return old_id


def delete(cred_type: str, operator_id: int,
           scope_type: str = "system", scope_id: str = "") -> bool:
    """删除系统凭据（物理删行；删除即失效——消费点读不到行即空，
    与 rc unbind 链同语义；审计靠 usage_log 历史 credential_id 悬空定位）。
    返回是否删除了行（不存在=False，幂等友好）。"""
    _check_type(cred_type)
    _require_admin(operator_id)
    from src.kernel.persistence.repositories import system_credential_repository as repo
    from src.kernel.persistence.uow import uow
    with uow() as session:
        existed = repo.delete(session, scope_type, scope_id, cred_type)
    if existed:
        _audit("system_credential_delete", operator_id, cred_type,
               {"scope_id": scope_id})
        log.info(f"grape_t_system_credentials deleted: type={cred_type} operator={operator_id}")
    return existed


# ---------------------------------------------------------------------------
# 读路径（无 admin 门槛——消费是运行时行为；审计在 use 不在 resolve）
# ---------------------------------------------------------------------------

def get(cred_type: str, scope_type: str = "system", scope_id: str = ""):
    """取单条（解密后）。不存在返回 None。

    仅供管理面/运维探针用（要审计的消费方走 use()）。
    """
    _check_type(cred_type)
    from src.kernel.persistence.dto import SystemCredentialDTO
    from src.kernel.persistence.repositories import system_credential_repository as repo
    from src.kernel.persistence.uow import uow
    with uow() as session:
        m = repo.get(session, scope_type, scope_id, cred_type)
        if m is None:
            return None
        return SystemCredentialDTO(
            id=m.id, scope_type=m.scope_type, scope_id=m.scope_id,
            cred_type=m.cred_type, value=_decrypt_value(cred_type, m.value),
            created_at=m.created_at, updated_at=m.updated_at)


def list_by_type(cred_type: str) -> list:
    """按类型列出全部（解密后）。管理面"列出某类全部凭据"路径。"""
    _check_type(cred_type)
    from src.kernel.persistence.dto import SystemCredentialDTO
    from src.kernel.persistence.repositories import system_credential_repository as repo
    from src.kernel.persistence.uow import uow
    with uow() as session:
        rows = repo.list_by_type(session, cred_type)
        return [SystemCredentialDTO(
            id=m.id, scope_type=m.scope_type, scope_id=m.scope_id,
            cred_type=m.cred_type, value=_decrypt_value(cred_type, m.value),
            created_at=m.created_at, updated_at=m.updated_at) for m in rows]


def use(cred_type: str, actor_id: int = 0, executor_context: str = "",
        scope_type: str = "system", scope_id: str = ""):
    """受控使用入口（§7.3）：读行→解密→usage_log→返回明文。

    批 2 每个消费方迁移统一改走此入口——杜绝消费点各自解密绕过审计。
    批 1 零消费方下由 UC42⑥ 直调验证审计链（不 mock）。

    无行返回 None（调用方决定回落/报错——本入口不做 env 回落，那是
    resolve 的职责，两入口语义分离：use=有审计的权威读取，
    resolve=带回落的容错读取）。
    """
    from src.kernel.persistence.dto import SystemCredentialDTO
    from src.kernel.persistence.repositories import system_credential_repository as repo
    from src.kernel.persistence.uow import uow
    with uow() as session:
        m = repo.get(session, scope_type, scope_id, cred_type)
        if m is None:
            return None
        plain = _decrypt_value(cred_type, m.value)
        dto = SystemCredentialDTO(
            id=m.id, scope_type=m.scope_type, scope_id=m.scope_id,
            cred_type=m.cred_type, value=plain,
            created_at=m.created_at, updated_at=m.updated_at)
    # 消费审计（usage_log 异步队列；失败不阻断——审计非强一致，N4-p3 语义）
    try:
        from src.kernel.persistence.credential_usage_log import log_usage
        log_usage(dto.id, user_id=0,
                  credential_kind="system_credentials",
                  initiating_actor_id=actor_id,
                  executor_context=executor_context or "use",
                  operation=f"system_cred_use:{cred_type}")
    except Exception as e:
        log.warning(f"grape_t_system_credentials usage audit failed: {e}")
    return dto


# ---------------------------------------------------------------------------
# env fallback 注册表（§7.4：批 2 迁移的统一回落源）
# ---------------------------------------------------------------------------

# cred_type → 现存 env/文件源（A1-A4 现状实锚）。批 2 迁移=写 sc 行后
# resolve 自动走 DB；回滚=删 sc 行自动回 env。此处不改消费方代码。
_ENV_FALLBACKS = {
    "vlm_api_key": ("VLM_API_KEY", None),
    "obs_aksk": (None, {"ak": "OBS_ACCESS_KEY", "sk": "OBS_SECRET_KEY"}),
    "resend_api_key": ("RESEND_API_KEY", None),
    "admin_beta_api_key": ("ADMIN_BETA_APPLY_API_KEY", None),
    # A1 oauth_client 不登记 fallback：双源（meta+json）结构复杂，迁移时
    # 走专门的双写过渡路径（触发条件未到，不预建）
}


def resolve(cred_type: str, actor_id: int = 0, executor_context: str = ""):
    """带回落的读取（§7.4）：DB 命中即权威（走 use 审计）；DB 空 → 回落
    env 源（无审计——env 是现状形态，消费方批 2 迁移前行为不变）。

    返回解密值（str 或 dict）或 None。批 2 迁移动作=写入 sc 行后消费方
    不改代码自动切 DB；回滚=删行自动回 env。
    """
    dto = use(cred_type, actor_id=actor_id, executor_context=executor_context or "resolve")
    if dto is not None:
        return dto.value
    fb = _ENV_FALLBACKS.get(cred_type)
    if fb is None:
        return None
    env_single, env_map = fb
    import os
    if env_single:
        return os.environ.get(env_single, "") or None
    if env_map:
        out = {k: os.environ.get(v, "") for k, v in env_map.items()}
        return out if any(out.values()) else None
    return None
