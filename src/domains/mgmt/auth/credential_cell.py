"""OV 凭据格子（Issue #2423 M2）—— user_id×org 维度的管理端凭据配置。

设计约束（对齐联邦 v2 / OvGate 写路径门禁；公开口径见
`help/api/16-auth-and-misc.md` 的「组织 OV 凭据格子」节）：
- 明文只落 grape_t_plugin_credential_vault（全系统唯一持明文处，真本单存）；
  格子行（grape_t_ov_credential_cells）只存 vault_ref 句柄，不含明文；
- 吊销 = 格子行 + vault 行同事务删除 + 吊销台账（grape_t_ov_credential_cell_revocations）
  落一笔——exec 每次实时解析（无缓存副本），吊销即失效、不复活：存量自配
  user_credentials 的兼容回退在吊销后被台账阻断，重新配格子方可恢复；
- 凭据按 user_id 隔离（resolve_cell(user_id)）：两用户任务 OV 落账身份可区分；
- 管理端 API 仅组织管理员可配/撤任意成员（路由层 require_path_org_admin）。

表结构单一事实源在本模块（QA 200711627 Standards #4）：db_setup 的
_NEW_TABLES / safe 迁移从本模块导入 DDL，双源定义已收敛。
"""
from __future__ import annotations

from src.kernel.config import log
from src.kernel.id_gen import uuid7_str
from src.kernel.session_manager import db, db_lock

OV_PLUGIN_ID = "openviking"
OV_CRED_KEY = "ov_api_key"

# 格子托管凭据注册表（cred_key → plugin_id）：exec 端点按 manifest 凭据标记 /
# 此注册表分派格子解析，通用端点不硬编码具体插件凭据键特例（Standards #5）。
_CELL_MANAGED_CREDENTIALS = {OV_CRED_KEY: OV_PLUGIN_ID}

# manifest cred_spec 声明格子托管的标记值（"managed" 字段；"user"/"none" 等
# 显式反标记可让注册表键位插件强制走用户自配路径）
_CELL_MANAGED_MARKERS = ("org_cell", "cell", "vault_cell", "true")

OV_CREDENTIAL_CELLS_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_ov_credential_cells (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    org_id     INTEGER NOT NULL DEFAULT 0,
    base_url   TEXT DEFAULT '',
    vault_ref  TEXT NOT NULL,                -- vault://cred/<ref> 的 ref（不含明文）
    created_by INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, org_id)
)
"""

# 吊销台账（QA 200711627 (a)3）：吊销后阻断存量自配 user_credentials 兜底回退——
# 管理员吊销语义对全部解析序成立（吊销不复活），重配格子即恢复。
OV_CREDENTIAL_REVOCATIONS_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_ov_credential_cell_revocations (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    org_id     INTEGER NOT NULL DEFAULT 0,
    revoked_by INTEGER DEFAULT 0,
    revoked_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, org_id)
)
"""


def _cell_name(org_id: int) -> str:
    """vault 内凭据名（user_id 维度由 owner_id 承载，org 维度编码进 name）。"""
    return f"{OV_CRED_KEY}@org{org_id or 0}"


def _ensure_table() -> None:
    with db_lock:
        db.execute(OV_CREDENTIAL_CELLS_DDL)
        db.execute(OV_CREDENTIAL_REVOCATIONS_DDL)
        db.commit()


_ensure_table()


def is_cell_managed_credential(cred_key: str, cred_spec: dict | None = None) -> bool:
    """格子托管判定（exec 端点凭据解析分派入口，Standards #5）。

    manifest cred_spec 显式声明 managed 标记 → 以标记为准（org_cell 等值走格子；
    user/none 等反标记强制走用户自配）；未声明标记的存量插件按注册表
    （ov_api_key）兜底——市场 hd-openviking 无标记亦正确分派。
    """
    if cred_spec:
        marker = str(cred_spec.get("managed") or "").strip().lower()
        if marker:
            return marker in _CELL_MANAGED_MARKERS
    return (cred_key or "") in _CELL_MANAGED_CREDENTIALS


def put_cell(admin_user_id: int, target_user_id: int, org_id: int,
             ov_api_key: str, base_url: str = "") -> dict:
    """配置/重置一格（user_id×org）：明文入 vault，格子行存 ref。

    同格重配：vault ref 稳定（同 plugin+name+owner 更新明文不改 ref），
    格子行只刷新 base_url/created_by/updated_at。
    """
    from src.domains.mgmt.auth.vault import CredentialVault  # arch11 落位
    key = (ov_api_key or "").strip()
    if not key:
        raise ValueError("ov_api_key 不能为空")
    handle = CredentialVault().put_secret(
        plugin_id=OV_PLUGIN_ID, name=_cell_name(org_id),
        secret=key, owner_id=str(target_user_id))
    ref = handle.rsplit("/", 1)[-1]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO grape_t_ov_credential_cells (id, user_id, org_id, base_url, vault_ref, created_by) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id, org_id) DO UPDATE SET "
            "base_url=excluded.base_url, vault_ref=excluded.vault_ref, "
            "created_by=excluded.created_by, updated_at=datetime('now')",
            (uuid7_str(), target_user_id, org_id or 0, (base_url or "").strip(), ref, admin_user_id or 0))
        db.commit()
    log.info(f"ov_credential_cell: stored user={target_user_id} org={org_id} by={admin_user_id}")
    return {"user_id": target_user_id, "org_id": org_id or 0, "vault_ref": ref}


def revoke_cell(target_user_id: int, org_id: int, revoked_by: int = 0) -> bool:
    """吊销一格：格子行 + vault 行同删 + 吊销台账落笔（同一事务，无副本缓存 → 不复活）。

    台账用途（QA (a)3）：吊销后 exec 端点的存量自配 user_credentials 兼容回退
    被 has_revoked_cell 阻断——吊销语义对全部解析序成立；重新配格子即恢复。
    返回是否命中（格子不存在返回 False，路由层转 404）。
    """
    with db_lock:
        c = db.cursor()
        c.execute("SELECT vault_ref FROM grape_t_ov_credential_cells WHERE user_id=? AND org_id=?",
                  (target_user_id, org_id or 0))
        row = c.fetchone()
        if not row:
            return False
        ref = row[0]
        c.execute("DELETE FROM grape_t_ov_credential_cells WHERE user_id=? AND org_id=?",
                  (target_user_id, org_id or 0))
        c.execute("DELETE FROM grape_t_plugin_credential_vault WHERE ref=?", (ref,))
        c.execute(
            "INSERT INTO grape_t_ov_credential_cell_revocations (id, user_id, org_id, revoked_by) "
            "VALUES (?,?,?,?) ON CONFLICT(user_id, org_id) DO UPDATE SET "
            "revoked_by=excluded.revoked_by, revoked_at=datetime('now')",
            (uuid7_str(), target_user_id, org_id or 0, revoked_by or 0))
        db.commit()
    log.info(f"ov_credential_cell: revoked user={target_user_id} org={org_id} by={revoked_by}")
    return True


def has_revoked_cell(user_id: int) -> bool:
    """该用户是否存在已吊销的格子记录（吊销阻断存量自配回退，吊销不复活）。

    user 维度判定（与 resolve_cell 的 org_id=None 兑换口径一致）：任一格子被
    吊销即视为管理员收回该用户 OV 访问——重配格子（put_cell）即恢复。
    """
    if not str(user_id or "").strip():  # arch11 D105：user_id 为 UUID 串（非 int）
        return False
    with db_lock:
        row = db.cursor().execute(
            "SELECT 1 FROM grape_t_ov_credential_cell_revocations WHERE user_id=? LIMIT 1",
            (user_id,)).fetchone()
    return bool(row)


def resolve_cell(user_id: int, org_id: int | None = None) -> dict | None:
    """执行期解析（exec 端点 tk_ 兑换用）：格子 → vault 明文。

    org_id=None：不限组织，取该用户最近更新的一格（多组织各配时以最新为准）。
    返回 {"secret": 明文, "base_url": str}；未配置返回 None（调用方按
    PLUGIN_CREDENTIAL_MISSING 处理）。
    """
    if not str(user_id or "").strip():  # arch11 D105：user_id 为 UUID 串（非 int）
        return None
    with db_lock:
        c = db.cursor()
        if org_id is not None:
            c.execute("SELECT vault_ref, base_url FROM grape_t_ov_credential_cells "
                      "WHERE user_id=? AND org_id=?", (user_id, org_id or 0))
        else:
            c.execute("SELECT vault_ref, base_url FROM grape_t_ov_credential_cells "
                      "WHERE user_id=? ORDER BY updated_at DESC, id DESC LIMIT 1", (user_id,))
        row = c.fetchone()
    if not row:
        return None
    ref, base_url = row[0], row[1] or ""
    from src.domains.mgmt.auth.vault import CredentialVault  # arch11 落位
    secret = CredentialVault().get_secret(f"vault://cred/{ref}")
    if not secret:
        return None
    return {"secret": secret, "base_url": base_url}


def list_cells(org_id: int) -> list[dict]:
    """管理端列表（掩码：永不回明文，只回存在性/元数据）。

    单查询 JOIN 直取 vault 存在性（不在循环内逐行查询，避免 N+1，QA Standards #7）。
    """
    with db_lock:
        rows = db.cursor().execute(
            "SELECT cells.user_id, cells.org_id, cells.base_url, cells.created_at, "
            "cells.updated_at, acc.username, vault.ref IS NOT NULL "
            "FROM grape_t_ov_credential_cells cells "
            "LEFT JOIN grape_accounts acc ON acc.id = cells.user_id "
            "LEFT JOIN grape_t_plugin_credential_vault vault ON vault.ref = cells.vault_ref "
            "WHERE cells.org_id=? ORDER BY cells.user_id",
            (org_id,)).fetchall()
    out = []
    for uid, oid, base_url, created, updated, username, has_secret in rows or []:
        out.append({
            "user_id": uid,
            "username": username or "",
            "org_id": oid,
            "base_url": base_url,
            "has_secret": bool(has_secret),
            "created_at": created,
            "updated_at": updated,
        })
    return out
