"""统一凭据库 —— 全系统唯一持明文处（Issue #1082 真本单存）。

设计约束（凭据明文零副本）：
- 明文只存在于本表 grape_t_plugin_credential_vault.secret 一列；
- 其余各处（overlay JSON / 有效配置视图 / 审计与日志）一律只出现
  引用句柄 `vault://cred/<ref>`，不落明文；
- ref 稳定：同 (plugin_id, name, owner_id) 重置密钥时 ref 不变，
  已分发的句柄引用持续有效（换 key 只改一处，无需同步）；
- 明文换出（get_secret）仅供 A2 #1080 Runner 凭据代理在执行时刻调用。

密钥命名判定复用 src/scene_custom_env 的命名约定（_KEY/_SK/_SECRET/
_TOKEN/_PASSWORD/_PWD/_CREDENTIALS 后缀），全库单一约定。
"""
from __future__ import annotations

import uuid

from src.kernel.config import log
from src.kernel.id_gen import uuid7_str
from src.kernel.scene_custom_env import is_secret_name
from src.kernel.session_manager import db, db_lock

HANDLE_PREFIX = "vault://cred/"


def is_credential_name(name: str) -> bool:
    """按全库统一命名约定判定凭据项（引擎落库置换与掩码共用）。"""
    return is_secret_name(name)


class CredentialVault:
    """统一凭据库：put_secret 存明文返句柄；get_secret 凭句柄换明文。"""

    def put_secret(self, plugin_id: str, name: str, secret: str,
                   owner_id: str = "") -> str:
        """存入明文，返回引用句柄（已存在则更新明文、ref 保持稳定）。"""
        if not name:
            raise ValueError("凭据名 name 不能为空")
        secret = "" if secret is None else str(secret)
        if not secret:
            raise ValueError("凭据明文不能为空（空值请直接从 overlay 删除该项）")
        with db_lock:
            cur = db.execute(
                "SELECT ref FROM grape_t_plugin_credential_vault"
                " WHERE plugin_id=? AND name=? AND owner_id=?",
                (plugin_id, name, owner_id))
            row = cur.fetchone()
            if row is not None:
                ref = row[0]
                db.execute(
                    "UPDATE grape_t_plugin_credential_vault SET secret=?"
                    " WHERE plugin_id=? AND name=? AND owner_id=?",
                    (secret, plugin_id, name, owner_id))
            else:
                ref = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO grape_t_plugin_credential_vault (id, ref, plugin_id, name,"
                    " secret, owner_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (uuid7_str(), ref, plugin_id, name, secret, owner_id))
            db.commit()
        log.info(f"credential_vault: secret stored plugin={plugin_id}"
                 f" name={name} owner={owner_id or '-'}")
        return HANDLE_PREFIX + ref

    def get_secret(self, handle: str) -> str | None:
        """凭引用句柄换取明文（仅供 A2 Runner 凭据代理执行时刻调用）。

        未知/非法句柄返回 None（不抛异常，调用方按未授权处理）。
        """
        ref = handle_to_ref(handle)
        if not ref:
            return None
        with db_lock:
            cur = db.execute(
                "SELECT secret FROM grape_t_plugin_credential_vault WHERE ref=?", (ref,))
            row = cur.fetchone()
        if row is None:
            return None
        return row[0]


def handle_to_ref(handle: str) -> str:
    """句柄 → ref；非法句柄返回空串。"""
    h = handle or ""
    if not h.startswith(HANDLE_PREFIX):
        return ""
    return h[len(HANDLE_PREFIX):].strip()


def is_handle(value: str) -> bool:
    """值是否为凭据库引用句柄。"""
    return bool(handle_to_ref(value))
