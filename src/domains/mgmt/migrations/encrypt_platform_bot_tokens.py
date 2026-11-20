"""把 grape_t_repo_platforms.bot_token_enc 里仍是明文的 token 就地加密（迁移 202611170039，幂等）。

为什么需要：202611170035 从 meta 兜底搬 token，而 meta 的 global_bot_*_token 是历史明文值
（列名 bot_token_enc 是承诺，不是事实）。密文形态判定见 credential_crypto.is_ciphertext；
已加密的行原样不动。

表归 mgmt，所以迁移连同 SQL 一起住在本域；注册仍由 api 层引导器做。
"""
from __future__ import annotations


def encrypt_platform_bot_tokens() -> None:
    from src.domains.mgmt.auth.credential_crypto import encrypt, is_ciphertext
    from src.kernel.config import log
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        try:
            rows = c.execute("SELECT provider, bot_token_enc FROM grape_t_repo_platforms "
                             "WHERE bot_token_enc IS NOT NULL AND bot_token_enc != ''").fetchall()
        except Exception:
            log.info("202611170039 平台表不存在，跳过")
            return
        encrypted_now = []
        for provider, token in rows:
            if is_ciphertext(token):
                continue
            c.execute("UPDATE grape_t_repo_platforms SET bot_token_enc=?, "
                      "updated_at=datetime('now') WHERE provider=?", (encrypt(token), provider))
            encrypted_now.append(provider)
        db.commit()
    log.info(f"202611170039 平台 bot token 就地加密完成（本轮 {encrypted_now or '无'}）")


MIGRATION = {
    "version": "202611170039",
    "name": "encrypt-platform-bot-tokens",
    "kind": "backfill",
    "fn": encrypt_platform_bot_tokens,
    "domains": ("mgmt",),
}
