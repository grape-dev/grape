"""N1 凭据存储面一次性迁移（user_credentials UUID 主键 + 通道加密回填）。

生命周期设计（2026-11-07 用户拍板）：迁移是一次性代码，独立成模块——
上线 = init_db 一行调用；下线 = 删本文件 + 删那一行调用，user/db.py 零残留。

两个迁移步骤（各自幂等）：
1. UUID 补列回填：存量库复合主键（无 id 列）→ 补 id/created_at 列 + 空行回填
   36 字符标准 UUID（credential_usage_log 审计锚点，P-5 硬前置）。
2. 明文加密回填：通道加密（60123cf76）上线前的历史明文 JSON 行 → encrypt
   覆写（只处理明文行，已加密行零写入；一次性语义=回填后每次启动零命中）。

破坏性说明：步骤 2 是破坏性写操作，回滚锚点 = 通道加密上线前的既有 DB
备份链路；读侧 _load_credential_json 明文兼容保证回滚后功能不受影响。

下线判据（对齐 D-2A/D90 前置 11 的量化风格）：生产日志连续观察期内零
"backfill: N rows"（N>0）输出 → 迁移完成 → 可删本模块。
"""
import time
import uuid

from src.kernel.session_manager import db
from src.kernel.config import log
from src.kernel.id_gen import uuid7_str  # D105 第二批：DB 主键 UUIDv7


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
    try:
        db.execute("ALTER TABLE user_credentials ADD COLUMN id TEXT DEFAULT ''")
    except Exception:
        pass  # duplicate column = 已补，幂等语义
    try:
        db.execute("ALTER TABLE user_credentials ADD COLUMN created_at TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        # 双方言键：业务复合键 (user_id, provider)——不用 SQLite 方言 rowid
        #（MySQL 无 rowid 列，此前 except 吞错导致存量 MySQL 库回填静默空转）
        need = db.execute(
            "SELECT user_id, provider, COALESCE(kind,'') FROM user_credentials "
            "WHERE id IS NULL OR id=''").fetchall()
        for user_id, provider, kind in need:
            # A2b（2026-11-13）：唯一键扩为 (user_id, provider, kind)，同平台 PAT/OAuth
            # 各一行 —— WHERE 不带 kind 会把两行一起改写（QA M1）。
            db.execute(
                "UPDATE user_credentials SET id=?, "
                "created_at=CASE WHEN created_at IS NULL OR created_at='' THEN ? ELSE created_at END "
                "WHERE user_id=? AND provider=? AND COALESCE(kind,'')=?",
                (uuid7_str(), now, user_id, provider, kind))
        if need:
            db.commit()
            log.info(f"user_credentials UUID backfill: {len(need)} rows")
    except Exception as e:
        log.warning(f"user_credentials UUID backfill failed: {e}")


def backfill_encryption() -> None:
    """步骤 2：历史明文行加密回填（只处理明文行，已加密行零写入）。"""
    try:
        from src.domains.mgmt.auth.credential_crypto import encrypt
        # 双方言键：业务复合键（同 backfill_uuid，MySQL 兼容）
        rows = db.execute(
            "SELECT user_id, provider, COALESCE(kind,''), config_json FROM user_credentials").fetchall()
        plain = [(uid, prov, kind, cfg) for uid, prov, kind, cfg in rows
                 if _is_plaintext_credential(cfg)]
        for uid, prov, kind, cfg in plain:
            # A2b：同平台多 kind 各一行，WHERE 必须带 kind（QA M1）
            db.execute(
                "UPDATE user_credentials SET config_json=? "
                "WHERE user_id=? AND provider=? AND COALESCE(kind,'')=?",
                (encrypt(cfg), uid, prov, kind))
        if plain:
            db.commit()
            log.info(f"user_credentials plaintext encryption backfill: {len(plain)} rows")
    except Exception as e:
        log.warning(f"user_credentials encrypt backfill failed: {e}")


def backfill_repo_credentials_encrypt() -> int:
    """步骤 3（D90-N3）：grape_t_repo_credentials 明文行加密回填（幂等）。

    明文判定 = 无冒号（decrypt 明文兼容同构判据）；密文行含冒号跳过。
    复合键 (repo_key, cred_type) 定位——双方言安全（N1 rowid 教训复用）。
    返回加密行数。
    """
    try:
        from src.domains.mgmt.auth.credential_crypto import encrypt
        rows = db.execute(
            "SELECT repo_key, cred_type, value FROM grape_t_repo_credentials "
            "WHERE value != ''").fetchall()
        plain = [(rk, ct, val) for rk, ct, val in rows
                 if ":" not in val]  # 明文判定：密文恒为 kid:nonce:ct 三段
        for rk, ct, val in plain:
            db.execute(
                "UPDATE grape_t_repo_credentials SET value=? WHERE repo_key=? AND cred_type=?",
                (encrypt(val), rk, ct))
        if plain:
            db.commit()
            log.info(f"grape_t_repo_credentials encryption backfill: {len(plain)} rows")
        return len(plain)
    except Exception as e:
        log.warning(f"grape_t_repo_credentials encrypt backfill failed: {e}")
        return 0


def backfill_repo_credentials_uuid() -> int:
    """步骤 4（D90-N4 → D99 退役）：id 已是主键（建表自带，不可空）——
    本函数保留为壳（调用链稳定），语义=断言校验（残留空 id 行=数据异常告警）。"""
    try:
        need = db.execute(
            "SELECT repo_key, cred_type FROM grape_t_repo_credentials "
            "WHERE id IS NULL OR id=''").fetchall()
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
