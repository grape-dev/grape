"""把平台 bot 种子文件播进平台行（迁移 202611170037 / D110）。

文件格式（两种并存，见 configs/platform-bots.example.json）：
  1) 旧扁平键：gitcode_api_token / github_bot_token / github_bot_username / <p>_bot_token …
  2) 新数组：platform_bot: [{provider, bot_name, bot_token}]（别名 platform_type / bit_token 兼容）

配置文件只作初始化种子，运行时不读；凭据落库前加密；平台行对应列非空则不覆盖。

表归 mgmt，迁移连同 SQL 住本域；注册仍由 api 层引导器做。
"""
from __future__ import annotations


def seed_platform_bot_from_file() -> None:
    from src.domains.mgmt.auth.credential_crypto import encrypt
    from src.kernel.config import log, read_bot_seed_file
    from src.kernel.mysql_compat import get_db_backend
    from src.kernel.session_manager import db, db_lock

    seeds = read_bot_seed_file()
    if not seeds:
        log.info("202611170037 未发现平台 bot 种子（或文件为空），跳过")
        return

    backend = get_db_backend()
    filled = []
    with db_lock:
        c = db.cursor()
        if backend == "mysql":
            exists = bool(c.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = DATABASE() AND table_name = 'grape_t_repo_platforms'"
            ).fetchone())
        else:
            exists = bool(c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='grape_t_repo_platforms'"
            ).fetchone())
        if not exists:
            log.info("202611170037 平台表不存在，跳过种子")
            return
        for provider, fields in sorted(seeds.items()):
            row = c.execute("SELECT bot_username, bot_token_enc FROM grape_t_repo_platforms"
                            " WHERE provider=?", (provider,)).fetchone()
            if row is None:
                log.info(f"202611170037 平台行 {provider} 未导入，跳过该平台种子")
                continue
            cur_username, cur_token = (row[0] or ""), (row[1] or "")
            new_username = cur_username or fields.get("username", "")
            new_token = cur_token or (encrypt(fields["token"]) if fields.get("token") else "")
            if (new_username, new_token) != (cur_username, cur_token):
                c.execute("UPDATE grape_t_repo_platforms SET bot_username=?, bot_token_enc=?,"
                          " updated_at=datetime('now') WHERE provider=?",
                          (new_username, new_token, provider))
                filled.append(provider)
        db.commit()
    log.info(f"202611170037 平台 bot 种子完成（本轮写入 {filled or '无'}）")


MIGRATION = {
    "version": "202611170037",
    "name": "seed-platform-bot-from-config-file",
    "kind": "backfill",
    "fn": seed_platform_bot_from_file,
    "domains": ("mgmt",),
}
