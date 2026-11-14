"""N1 凭据存储面一次性迁移（user_credentials UUID 主键 + 通道加密回填）。

生命周期设计（2026-11-07 用户拍板）：迁移是一次性代码，独立成模块——
上线 = init_db 一行调用；下线 = 删本文件 + 删那一行调用，user/db.py 零残留。

四个迁移步骤（各自幂等）：
1. user_credentials UUID 补列回填（mgmt 域）
2. user_credentials 明文加密回填（mgmt 域）
3. grape_t_repo_credentials 明文加密回填（**insight 域**：该表基表在 insight 库）
4. grape_t_repo_credentials 空 id 断言（insight 域）

三库独立修正：原先 3/4 走 `runtime_lib` 读，而该表归 insight 域，且把 `read_all()`
的返回值当连接用（`db = read_all(...)` + `db.execute`），独立拓扑下必然
1146 / AttributeError。现改为 `domain_db.cursor(域)`：配了自有 DSN 走 owner 连接，
单库/视图期回落进程库——两条拓扑下语义一致。
"""
import time

from src.kernel.config import log
from src.kernel.domain_db import cursor as _domain_cursor
from src.kernel.id_gen import uuid7_str  # D105 第二批：DB 主键 UUIDv7


def _commit(c) -> None:
    """提交当前游标所属连接（sqlite/pymysql 的 cursor 都有 .connection）。"""
    conn = getattr(c, "connection", None)
    if conn is not None:
        conn.commit()
        return
    from src.kernel.session_manager import db  # 兜底：极少数包装游标无 connection

    db.commit()


def _is_plaintext_credential(config_json: str) -> bool:
    """明文判定（与 credential_crypto.decrypt 兼容判定同构）。

    密文恒为 {kid}:{nonce_b64}:{ct_b64} 三段且以 key_id 开头；明文 JSON 恒以
    { 开头（不可能命中合法 kid:nonce:ct 形态）。非三段密文形态 = 明文
    （空串除外——空无需回填）。
    """
    if not config_json:
        return False
    if config_json.lstrip().startswith("{"):
        return True
    return len(config_json.split(":", 2)) != 3


def backfill_uuid() -> None:
    """步骤 1：存量库幂等补列 id/created_at + UUID 回填（只处理空 id 行）。

    补列形态：直接 ALTER + 吞 duplicate column 错（幂等标准做法，零方言——
    不用 PRAGMA table_info 探测；MySQL 1060/SQLite duplicate column 双兼容）。
    """
    with _domain_cursor("mgmt") as c:
        for _ddl in ("ALTER TABLE user_credentials ADD COLUMN id TEXT DEFAULT ''",
                     "ALTER TABLE user_credentials ADD COLUMN created_at TEXT DEFAULT ''"):
            try:
                c.execute(_ddl)
            except Exception:
                pass  # duplicate column = 已补，幂等语义
        try:
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            # 双方言键：业务复合键 (user_id, provider)——不用 SQLite 方言 rowid
            c.execute(
                "SELECT user_id, provider, COALESCE(kind,'') FROM user_credentials "
                "WHERE id IS NULL OR id=''")
            need = c.fetchall()
            for user_id, provider, kind in need:
                # A2b（2026-11-13）：唯一键扩为 (user_id, provider, kind)，同平台 PAT/OAuth
                # 各一行 —— WHERE 不带 kind 会把两行一起改写（QA M1）。
                c.execute(
                    "UPDATE user_credentials SET id=?, "
                    "created_at=CASE WHEN created_at IS NULL OR created_at='' THEN ? ELSE created_at END "
                    "WHERE user_id=? AND provider=? AND COALESCE(kind,'')=?",
                    (uuid7_str(), now, user_id, provider, kind))
            if need:
                _commit(c)
                log.info(f"user_credentials UUID backfill: {len(need)} rows")
        except Exception as e:
            log.warning(f"user_credentials UUID backfill failed: {e}")


def backfill_encryption() -> None:
    """步骤 2：历史明文行加密回填（只处理明文行，已加密行零写入）。"""
    try:
        from src.domains.mgmt.auth.credential_crypto import encrypt

        with _domain_cursor("mgmt") as c:
            # 双方言键：业务复合键（同 backfill_uuid，MySQL 兼容）
            c.execute(
                "SELECT user_id, provider, COALESCE(kind,''), config_json FROM user_credentials")
            rows = c.fetchall()
            plain = [(uid, prov, kind, cfg) for uid, prov, kind, cfg in rows
                     if _is_plaintext_credential(cfg)]
            for uid, prov, kind, cfg in plain:
                # A2b：同平台多 kind 各一行，WHERE 必须带 kind（QA M1）
                c.execute(
                    "UPDATE user_credentials SET config_json=? "
                    "WHERE user_id=? AND provider=? AND COALESCE(kind,'')=?",
                    (encrypt(cfg), uid, prov, kind))
            if plain:
                _commit(c)
                log.info(f"user_credentials plaintext encryption backfill: {len(plain)} rows")
    except Exception as e:
        log.warning(f"user_credentials encrypt backfill failed: {e}")


def backfill_repo_credentials_encrypt() -> int:
    """步骤 3（D90-N3）：grape_t_repo_credentials 明文行加密回填（幂等）。

    明文判定 = 无冒号（decrypt 明文兼容同构判据）；密文行含冒号跳过。
    复合键 (repo_key, cred_type) 定位——双方言安全（N1 rowid 教训复用）。
    表归 insight 域，故走 insight 连接。
    """
    try:
        from src.domains.mgmt.auth.credential_crypto import encrypt

        with _domain_cursor("insight") as c:
            c.execute(
                "SELECT repo_key, cred_type, value FROM grape_t_repo_credentials "
                "WHERE value != ''")
            rows = c.fetchall()
            plain = [(rk, ct, val) for rk, ct, val in rows
                     if ":" not in val]  # 明文判定：密文恒为 kid:nonce:ct 三段
            for rk, ct, val in plain:
                c.execute(
                    "UPDATE grape_t_repo_credentials SET value=? "
                    "WHERE repo_key=? AND cred_type=?",
                    (encrypt(val), rk, ct))
            if plain:
                _commit(c)
                log.info(f"grape_t_repo_credentials encryption backfill: {len(plain)} rows")
        return len(plain)
    except Exception as e:
        log.warning(f"grape_t_repo_credentials encrypt backfill failed: {e}")
        return 0


def backfill_repo_credentials_uuid() -> int:
    """步骤 4（D90-N4 → D99 退役）：id 已是主键（建表自带，不可空）——

    本函数保留为壳（调用链稳定），语义=断言校验（残留空 id 行=数据异常告警）。
    """
    try:
        with _domain_cursor("insight") as c:
            c.execute(
                "SELECT repo_key, cred_type FROM grape_t_repo_credentials "
                "WHERE id IS NULL OR id=''")
            need = c.fetchall()
        if need:
            # D99 主键化后不应出现——出现即数据异常（旧库未走新 DDL），响亮告警
            log.warning(f"grape_t_repo_credentials: {len(need)} rows with empty id "
                        f"(D99 主键化后不应出现，检查库版本): {need[:3]}")
        return len(need)
    except Exception as e:
        log.warning(f"grape_t_repo_credentials UUID backfill failed: {e}")
        return 0

def run() -> None:
    """迁移入口（init_db 调用）。各步幂等，整体一次性。"""
    backfill_uuid()
    backfill_encryption()
    backfill_repo_credentials_encrypt()
    backfill_repo_credentials_uuid()
