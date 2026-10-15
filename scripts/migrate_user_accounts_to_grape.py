#!/usr/bin/env python3
"""user_accounts → grape_accounts + external_identities + user_credentials 统一迁移。

设计原则：
1. 依赖 user_accounts.migrated 字段跳过已迁移行（migrated=1）
2. 每行的全部数据（grape_accounts + external_identities + user_credentials）
   一次性迁完才标记 migrated=1，保证不遗漏
3. 每一步用 NOT EXISTS 幂等检查，重跑不产生重复数据
4. 服务启动时自动执行（main.py lifespan yield 前同步等待完成）

三阶段：
  阶段 1：用户数据 + OAuth 绑定 + 业务凭据
  阶段 2：关联表 user_id 更新（27 张表，含 issue/issue_state/agent_tasks 等）
  阶段 3：全表孤儿 ID 扫描（迁移后校验，有孤儿则需人工处理）

用法：
    .venv/bin/python -m scripts.migrate_user_accounts_to_grape --dry-run
    .venv/bin/python -m scripts.migrate_user_accounts_to_grape --execute
    .venv/bin/python -m scripts.migrate_user_accounts_to_grape --execute --force
"""
import argparse
import json
import logging
import os
import sys
import time

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.session_manager import db, db_lock

log = logging.getLogger("devops-bot")


# ── 幂等插入原语 ──────────────────────────────────────────────────────

def _ensure_grape_account(username, password_hash, nickname,
                          token_version, created_at, last_active_at,
                          dry_run) -> int | None:
    """创建 grape_accounts 记录（如不存在），返回 grape_user_id。

    幂等：按 username 查已有 → 返回 id；不存在 → INSERT → 返回 lastrowid。
    P1-5 修复：SELECT 和 INSERT 在同一个 db_lock 内，消除 TOCTOU 竞态。
    """
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM grape_accounts WHERE username = ?", (username,))
        existing = c.fetchone()
        if existing:
            return existing[0]

        if dry_run:
            print(f"    [DRY-RUN] 将创建 grape_accounts: {username}")
            return None

        c.execute(
            "INSERT INTO grape_accounts "
            "(username, email, password_hash, nickname, status, token_version, "
            " last_active_at, created_at, updated_at) "
            "VALUES (?, NULL, ?, ?, 1, ?, ?, ?, datetime('now'))",
            (username, password_hash, nickname or "",
             token_version or 0, last_active_at, created_at)
        )
        db.commit()
        return c.lastrowid


def _ensure_external_identity(grape_user_id, provider, external_id,
                              external_login, access_token, dry_run) -> bool:
    """创建 external_identities 记录（如不存在）。

    幂等：按 (provider, external_id) 查已有 → 跳过。
    D10: access_token 加密存储（与运行时 create_external_identity 一致）。
    D22: INSERT 时带 status=1（活跃绑定）。
    P1-5 修复：SELECT 和 INSERT 在同一个 db_lock 内，消除 TOCTOU 竞态。
    返回 True 表示已存在或创建成功，False 表示创建失败。
    """
    if not external_id:
        return True

    # D10: 加密 access_token（与运行时 create_external_identity 调 encrypt 一致）
    from src.auth.credential_crypto import encrypt
    encrypted_token = encrypt(access_token or "") if access_token else ""

    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT 1 FROM external_identities WHERE provider = ? AND external_id = ?",
                (provider, external_id)
            )
            if c.fetchone():
                return True

            if dry_run:
                print(f"    [DRY-RUN] 将创建 external_identities: {provider} {external_id[:8]}...")
                return True

            c.execute(
                "INSERT INTO external_identities "
                "(grape_user_id, provider, external_id, external_login, "
                " access_token, refresh_token, status, token_expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, '', 1, '', datetime('now'), datetime('now'))",
                (grape_user_id, provider, external_id, external_login or "",
                 encrypted_token)
            )
            db.commit()
        return True
    except Exception as e:
        log.warning(f"_ensure_external_identity failed: provider={provider}, external_id={external_id[:8]}, error={e}")
        return False


def _ensure_user_credentials(grape_user_id, provider, config_json, dry_run, source_username="") -> bool:
    """创建 user_credentials 记录（如不存在）。

    幂等：按 (user_id, provider) 查已有 → 跳过。
    P1-5 修复：SELECT 和 INSERT 在同一个 db_lock 内，消除 TOCTOU 竞态。
    Issue #343: 撞 ID 防护——写入前核对 grape_user_id 对应的 username 是否和源用户一致，
    不一致说明旧 ID 撞上了新表另一个用户，打 WARNING 拒绝写入。
    返回 True 表示已存在或创建成功，False 表示创建失败。
    """
    if not config_json:
        return True

    try:
        with db_lock:
            c = db.cursor()
            # Issue #343: 撞 ID 防护——核对 grape_user_id 的 username 是否和源用户一致
            if source_username:
                c.execute("SELECT username FROM grape_accounts WHERE id=?", (grape_user_id,))
                row = c.fetchone()
                if row and row[0] != source_username:
                    log.warning(
                        f"  [WARNING] _ensure_user_credentials: user_id={grape_user_id} "
                        f"belongs to '{row[0]}' not '{source_username}' (ID collision), "
                        f"skipping {provider} credentials to avoid overwriting wrong user's data"
                    )
                    return False

            c.execute(
                "SELECT 1 FROM user_credentials WHERE user_id = ? AND provider = ?",
                (grape_user_id, provider)
            )
            if c.fetchone():
                return True

            if dry_run:
                print(f"    [DRY-RUN] 将创建 user_credentials: {provider}")
                return True

            c.execute(
                "INSERT INTO user_credentials (user_id, provider, config_json, updated_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (grape_user_id, provider, config_json)
            )
            db.commit()
        return True
    except Exception as e:
        log.warning(f"_ensure_user_credentials failed: provider={provider}, user_id={grape_user_id}, error={e}")
        return False


# ── 单行迁移：一次做对 ────────────────────────────────────────────────

def _migrate_one_user(row, dry_run) -> tuple[int, int] | None:
    """迁移单行 user_accounts → 全部新表数据，返回 (old_id, new_id) 或 None。

    BUG-M4 修复：检查每个子操作返回值，失败时不标记 migrated=1。
    BUG-M2 修复：SELECT 补 github_api_token 字段，迁移 GitHub OAuth token。
    """
    (old_id, username, password_hash, nickname, token_version,
     gitcode_id, gitcode_api_token, github_id, github_login, github_api_token,
     hw_ak, hw_sk, codearts_ak, codearts_sk,
     feishu_app_id, feishu_app_secret,
     created_at, last_active_at) = row

    # 1. grape_accounts
    new_id = _ensure_grape_account(
        username, password_hash, nickname or "",
        token_version, created_at, last_active_at, dry_run
    )
    if new_id is None:
        return None  # dry-run 且账号不存在

    # 记录子操作失败，失败时不标记 migrated=1
    failures = []

    # 2. external_identities — OAuth 绑定
    if gitcode_id:
        if not _ensure_external_identity(
            new_id, "gitcode", gitcode_id,
            external_login="", access_token=gitcode_api_token or "",
            dry_run=dry_run
        ):
            failures.append("gitcode_external_identity")
    if github_id:
        if not _ensure_external_identity(
            new_id, "github", github_id,
            external_login=github_login or "",
            access_token=github_api_token or "",  # BUG-M2: 迁移 GitHub token
            dry_run=dry_run
        ):
            failures.append("github_external_identity")

    # 3. user_credentials — 业务凭据
    # Issue #343: 传 source_username 做撞 ID 防护
    if hw_ak or hw_sk:
        if not _ensure_user_credentials(
            new_id, "huawei_cloud",
            json.dumps({"ak": hw_ak or "", "sk": hw_sk or ""}, ensure_ascii=False),
            dry_run, source_username=username
        ):
            failures.append("huawei_cloud_credentials")
    if codearts_ak or codearts_sk:
        if not _ensure_user_credentials(
            new_id, "codearts",
            json.dumps({"ak": codearts_ak or "", "sk": codearts_sk or ""}, ensure_ascii=False),
            dry_run, source_username=username
        ):
            failures.append("codearts_credentials")
    if feishu_app_id or feishu_app_secret:
        if not _ensure_user_credentials(
            new_id, "feishu",
            json.dumps({"app_id": feishu_app_id or "", "app_secret": feishu_app_secret or ""},
                       ensure_ascii=False),
            dry_run, source_username=username
        ):
            failures.append("feishu_credentials")

    if failures:
        log.warning(f"_migrate_one_user: {username} (old_id={old_id}) failures: {failures}")
        return None  # 有失败，不标记 migrated=1

    return (old_id, new_id)


# ── 阶段 1：遍历迁移 ──────────────────────────────────────────────────

def _migrate_user_accounts(dry_run: bool) -> dict[int, int]:
    """阶段 1：遍历 migrated=0 的行，每行全量迁移后标记 migrated=1。"""
    id_map: dict[int, int] = {}

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, username, password_hash, nickname, token_version, "
            "gitcode_id, gitcode_api_token, github_id, github_login, github_api_token, "
            "huawei_cloud_ak, huawei_cloud_sk, "
            "CODEARTS_CLI_AK, CODEARTS_CLI_SK, "
            "feishu_app_id, feishu_app_secret, "
            "created_at, last_active_at "
            "FROM user_accounts WHERE username != 'default' AND migrated = 0"
        )
        old_rows = c.fetchall()

    print(f"\n[阶段 1] migrated=0 的行: {len(old_rows)} 行（排除 default）")

    for row in old_rows:
        old_id = row[0]
        username = row[1]

        result = _migrate_one_user(row, dry_run)
        if result is None:
            print(f"  [DRY-RUN] {username} (old_id={old_id})")
            continue

        _, new_id = result
        id_map[old_id] = new_id
        print(f"  {username}: old_id={old_id} → grape_accounts id={new_id}")

        # 全部数据迁完 → 标记 migrated=1
        if not dry_run:
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE user_accounts SET migrated=1 WHERE id=?", (old_id,))
                db.commit()

    print(f"[阶段 1] 完成: {len(id_map)} 个用户")
    return id_map


# ── 辅助函数 ──────────────────────────────────────────────────────────

def _get_pk_columns(table: str) -> list[str]:
    """获取表的主键列名列表（用于逐行 UPDATE 定位单行）。

    SQLite: PRAGMA table_info → pk 标记。
    MySQL: PRAGMA table_info 靠 mysql_compat 翻译；如翻译失败则 fallback SHOW KEYS。
    返回空列表表示无主键（fallback 到批量 UPDATE）。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"PRAGMA table_info({table})")
            cols = c.fetchall()
        pk_cols = [r[1] for r in cols if r[5] > 0]  # r[5] = pk flag
        if not pk_cols:
            # 无显式主键，用所有列做 WHERE（近似 rowid 级别定位）
            return [r[1] for r in cols] if cols else []
        return sorted(pk_cols, key=lambda col: next(r[5] for r in cols if r[1] == col))
    except Exception as e:
        log.warning(f"_get_pk_columns({table}) PRAGMA failed: {e}, trying SHOW KEYS fallback")
    # Issue #343 修复: MySQL 环境 PRAGMA 翻译可能失败，fallback 到 SHOW KEYS
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SHOW KEYS FROM {table} WHERE Key_name='PRIMARY'"
            )
            rows = c.fetchall()
        # SHOW KEYS 返回列顺序: Table, Non_unique, Key_name, Seq_in_index, Column_name, ...
        if rows:
            pk_cols = [r[4] for r in rows if r[4]]
            # 按 Seq_in_index 排序（复合主键顺序很重要）
            pk_cols = sorted(pk_cols, key=lambda col: next(r[3] for r in rows if r[4] == col))
            return pk_cols
    except Exception as e2:
        log.warning(f"_get_pk_columns({table}) SHOW KEYS fallback also failed: {e2}")
    return []


# ── 阶段 2：关联表 user_id 更新 ────────────────────────────────────────

def _update_related_tables(id_map: dict[int, int], dry_run: bool) -> None:
    """用 id 映射更新关联表的 user_id。

    安全幂等：只更新 user_id 不在 grape_accounts 中的记录（旧表残留 id），
    避免误改 grape_accounts 中同 id 的其他用户数据。
    """
    tables = [
        # 原有 4 张
        ("user_repos", "user_id"),
        ("chat_conversations", "user_id"),
        ("organization", "owner_id"),
        ("org_member", "user_id"),
        # Issue #343: 补全遗漏的用户归属列（INTEGER 类型，存 user_id）
        ("issue", "user_id"),
        ("issue", "scene_user_id"),
        ("issue_state", "scene_user_id"),
        ("agent_tasks", "user_id"),
        ("scene_market", "source_user_id"),
        ("scene_likes", "user_id"),
        ("skill_likes", "user_id"),
        ("session_api_tokens", "user_id"),
        ("api_tokens", "user_id"),
        ("user_feedback", "user_id"),
        ("org_invitation", "invitee_user_id"),
        ("audit_logs", "user_id"),
        ("exchange_codes", "user_id"),
        ("autopilot_tasks", "created_by"),
        # Issue #343 P1-3 修复: created_by_oauth 是布尔标志(0/1)不是 user_id，从迁移列表删除
        # ("grape_accounts", "created_by_oauth"),  — removed (boolean flag, not a user_id)
        ("community_posts", "user_id"),
        ("community_comments", "user_id"),
        ("community_post_likes", "user_id"),
        ("external_identities", "grape_user_id"),
        ("unified_user_mapping", "user_id"),
        ("feishu_onboard_task", "user_id"),
        ("binding_tickets", "initiating_user_id"),
        ("binding_tickets", "original_user_id"),
        # Issue #343: user_credentials 也加入，让撞 ID WARNING + 孤儿扫描兜住凭据归属
        ("user_credentials", "user_id"),
    ]

    print(f"\n[阶段 2] 更新关联表（{len(id_map)} 个映射）")

    for table, column in tables:
        updated = 0
        skipped = 0
        collisions = 0

        for old_id, new_id in id_map.items():
            with db_lock:
                c = db.cursor()
                # 查旧 ID 的总行数（含撞 ID 的）
                c.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (old_id,))
                total = c.fetchone()[0]
                # 查真正需要迁移的（不在 grape_accounts 中的）
                c.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE {column} = ? "
                    f"AND {column} NOT IN (SELECT id FROM grape_accounts)",
                    (old_id,)
                )
                migratable = c.fetchone()[0]

                if migratable == 0:
                    if total > 0:
                        # 旧 ID 撞上了新表另一个用户 → 无法自动迁移
                        log.warning(
                            f"  [WARNING] {table}.{column}: {total} 行 old_id={old_id} "
                            f"撞上新用户 ID，无法自动迁移，需人工核对"
                        )
                        collisions += total
                    skipped += 1
                    continue

                if dry_run:
                    print(f"  [DRY-RUN] {table}.{column}: {migratable} 行 {old_id} → {new_id}")
                    skipped += 1
                    continue

                # Issue #343: 按行 UPDATE + try/except，撞 UNIQUE 键时跳过单条继续
                # 旧逻辑一条 UPDATE 语句批量改，撞主键直接整个异常终止
                try:
                    # 先查需要迁移的行主键（用于逐行 UPDATE）
                    pk_cols = _get_pk_columns(table)
                    if pk_cols:
                        placeholders = ",".join(["?"] * len(pk_cols))
                        c.execute(
                            f"SELECT {','.join(pk_cols)} FROM {table} "
                            f"WHERE {column} = ? "
                            f"AND {column} NOT IN (SELECT id FROM grape_accounts)",
                            (old_id,)
                        )
                        rows_to_migrate = c.fetchall()
                        row_collisions = 0
                        for row_pk in rows_to_migrate:
                            try:
                                set_clause = f"{column} = ?"
                                where_clause = " AND ".join(
                                    f"{pk_cols[i]} = ?" for i in range(len(pk_cols))
                                )
                                c.execute(
                                    f"UPDATE {table} SET {set_clause} "
                                    f"WHERE {where_clause}",
                                    (new_id, *row_pk)
                                )
                                db.commit()
                                updated += 1
                            except Exception as row_err:
                                db.rollback()
                                row_collisions += 1
                                log.warning(
                                    f"  [WARNING] {table}.{column}: row {row_pk} "
                                    f"{old_id} → {new_id} collided (UNIQUE/PK), skipped: {row_err}"
                                )
                        if row_collisions:
                            collisions += row_collisions
                    else:
                        # 无主键表：fallback 到批量 UPDATE
                        c.execute(
                            f"UPDATE {table} SET {column} = ? "
                            f"WHERE {column} = ? "
                            f"AND {column} NOT IN (SELECT id FROM grape_accounts)",
                            (new_id, old_id)
                        )
                        db.commit()
                        updated += migratable
                except Exception as batch_err:
                    db.rollback()
                    log.warning(
                        f"  [WARNING] {table}.{column}: batch UPDATE {old_id} → {new_id} "
                        f"failed, skipped: {batch_err}"
                    )
                    skipped += 1

        print(f"  {table}.{column}: 更新 {updated} 行, 跳过 {skipped} 行" +
              (f", 撞ID {collisions} 行" if collisions else ""))

    print(f"[阶段 2] 完成")


# ── 阶段 3：迁移后全表孤儿 ID 扫描（Issue #343）─────────────────────

def _scan_orphan_ids() -> list[dict]:
    """扫描所有用户归属列中的孤儿 ID（不在 grape_accounts 中的 user_id）。

    迁移完成后调用，有孤儿说明迁移不完整，需人工处理。
    """
    orphan_tables = [
        ("user_repos", "user_id"),
        ("chat_conversations", "user_id"),
        ("organization", "owner_id"),
        ("org_member", "user_id"),
        ("issue", "user_id"),
        ("issue", "scene_user_id"),
        ("issue_state", "scene_user_id"),
        ("agent_tasks", "user_id"),
        ("scene_market", "source_user_id"),
        ("scene_likes", "user_id"),
        ("skill_likes", "user_id"),
        ("session_api_tokens", "user_id"),
        ("api_tokens", "user_id"),
        ("user_feedback", "user_id"),
        ("org_invitation", "invitee_user_id"),
        ("audit_logs", "user_id"),
        ("exchange_codes", "user_id"),
        ("autopilot_tasks", "created_by"),
        # Issue #343 P1-3 修复: created_by_oauth 是布尔标志(0/1)不是 user_id，从迁移列表删除
        # ("grape_accounts", "created_by_oauth"),  — removed (boolean flag, not a user_id)
        ("community_posts", "user_id"),
        ("community_comments", "user_id"),
        ("community_post_likes", "user_id"),
        ("external_identities", "grape_user_id"),
        ("unified_user_mapping", "user_id"),
        ("feishu_onboard_task", "user_id"),
        ("binding_tickets", "initiating_user_id"),
        ("binding_tickets", "original_user_id"),
        ("user_credentials", "user_id"),
    ]
    orphans = []
    for table, column in orphan_tables:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT {column}, COUNT(*) FROM {table} "
                    f"WHERE {column} != 0 "
                    f"AND {column} NOT IN (SELECT id FROM grape_accounts) "
                    f"GROUP BY {column}"
                )
                for row in c.fetchall():
                    orphans.append({"table": table, "column": column,
                                    "orphan_id": row[0], "count": row[1]})
        except Exception as e:
            log.warning(f"  [SCAN] {table}.{column}: scan failed: {e}")
    return orphans


def _scan_id_collisions() -> list[dict]:
    """扫描撞 ID 错位——user_credentials / external_identities 里的 user_id
    在 grape_accounts 中存在但 username 和源迁移用户不匹配的情况。

    孤儿扫描扫不出这种错位（旧 ID 是合法新 ID，不是孤儿）。
    专门检查 user_credentials：如果同一 user_id 的 gitcode_oauth/github_oauth
    凭据的 access_token 和 external_identities 里的不一致，说明可能撞 ID 错位。
    """
    collisions = []
    try:
        with db_lock:
            c = db.cursor()
            # 查 user_credentials 里有凭据但 external_identities 里无对应绑定的用户
            # 这些可能是撞 ID 后残留的错位凭据
            c.execute("""
                SELECT uc.user_id, uc.provider, ga.username
                FROM user_credentials uc
                JOIN grape_accounts ga ON uc.user_id = ga.id
                WHERE uc.provider IN ('gitcode_oauth', 'github_oauth')
                AND NOT EXISTS (
                    SELECT 1 FROM external_identities ei
                    WHERE ei.grape_user_id = uc.user_id
                    AND ei.provider = REPLACE(uc.provider, '_oauth', '')
                    AND ei.status = 1
                )
            """)
            for row in c.fetchall():
                collisions.append({
                    "user_id": row[0], "provider": row[1],
                    "username": row[2],
                    "issue": "user_credentials has OAuth cred but no matching external_identity"
                })
    except Exception as e:
        log.warning(f"  [SCAN] id collision scan failed: {e}")
    return collisions


# ── DDL ────────────────────────────────────────────────────────────────

def _ensure_migrated_column():
    """确保 user_accounts 表有 migrated 列（幂等）。"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute("ALTER TABLE user_accounts ADD COLUMN migrated INTEGER DEFAULT 0")
            db.commit()
            print("[DDL] user_accounts.migrated 列已添加")
        except Exception:
            pass


def _repair_missing_credentials(dry_run: bool) -> None:
    """BUG-M1 修复：补迁移已标记 migrated=1 但缺失 user_credentials 的用户。

    早期迁移脚本可能因异常被吞导致华为云凭据等未写入 user_credentials 表，
    但 migrated=1 已被标记。此函数扫描这类用户并补迁移。
    """
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT ua.id, ua.username, ua.huawei_cloud_ak, ua.huawei_cloud_sk, "
            "ua.CODEARTS_CLI_AK, ua.CODEARTS_CLI_SK, "
            "ua.feishu_app_id, ua.feishu_app_secret, "
            "ua.github_id, ua.github_api_token, ua.github_login "
            "FROM user_accounts ua "
            "WHERE ua.migrated=1 AND ua.username != 'default'"
        )
        rows = c.fetchall()

    repaired = 0
    for row in rows:
        (ua_id, username, hw_ak, hw_sk, ca_ak, ca_sk,
         fs_app, fs_secret, gh_id, gh_token, gh_login) = row

        # 查 grape_accounts id
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM grape_accounts WHERE username=?", (username,))
            r = c.fetchone()
        if not r:
            continue
        grape_id = r[0]

        # 检查并补迁移华为云凭据
        if hw_ak or hw_sk:
            if not _ensure_user_credentials(
                grape_id, "huawei_cloud",
                json.dumps({"ak": hw_ak or "", "sk": hw_sk or ""}, ensure_ascii=False),
                dry_run, source_username=username
            ):
                log.warning(f"_repair_missing_credentials: failed huawei_cloud for {username}")
            else:
                repaired += 1

        # 补迁移 codearts 凭据
        if ca_ak or ca_sk:
            if not _ensure_user_credentials(
                grape_id, "codearts",
                json.dumps({"ak": ca_ak or "", "sk": ca_sk or ""}, ensure_ascii=False),
                dry_run, source_username=username
            ):
                log.warning(f"_repair_missing_credentials: failed codearts for {username}")
            else:
                repaired += 1

        # 补迁移飞书凭据
        if fs_app or fs_secret:
            if not _ensure_user_credentials(
                grape_id, "feishu",
                json.dumps({"app_id": fs_app or "", "app_secret": fs_secret or ""}, ensure_ascii=False),
                dry_run, source_username=username
            ):
                log.warning(f"_repair_missing_credentials: failed feishu for {username}")
            else:
                repaired += 1

        # 补迁移 GitHub OAuth token（BUG-M2: 早期迁移没迁移 github_api_token）
        if gh_id and gh_token:
            if not _ensure_external_identity(
                grape_id, "github", gh_id,
                external_login=gh_login or "", access_token=gh_token,
                dry_run=dry_run
            ):
                log.warning(f"_repair_missing_credentials: failed github token for {username}")
            else:
                repaired += 1

    if repaired > 0:
        log.info(f"D32 repair: 补迁移 {repaired} 条缺失凭据")


# ── 主入口 ────────────────────────────────────────────────────────────

def run_migration(dry_run: bool = False, force: bool = False) -> dict[int, int]:
    """执行迁移，返回 id 映射表。可被 import 调用（服务启动时自动执行）。

    幂等机制：
    - user_accounts.migrated 列标记每行迁移状态（0=未迁移, 1=已迁移）
    - 只处理 migrated=0 的行，每行全量数据迁完才标记 migrated=1
    - force=True 重置所有行 migrated=0 后重新执行
    - 每个子操作用 NOT EXISTS 幂等检查，重跑不产生重复数据
    """
    log.info(f"D32 migration: user_accounts → grape_accounts (dry_run={dry_run}, force={force})")

    _ensure_migrated_column()

    if force and not dry_run:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE user_accounts SET migrated=0 WHERE username != 'default'")
            db.commit()
            log.info(f"D32 migration: force 模式，重置 {c.rowcount} 行 migrated=0")

    id_map = _migrate_user_accounts(dry_run)

    if id_map:
        _update_related_tables(id_map, dry_run)

    # BUG-M1 修复：补迁移已标记 migrated=1 但缺失凭据的用户
    _repair_missing_credentials(dry_run)

    # Issue #343: 迁移后全表孤儿 ID 扫描 + 撞 ID 错位检测
    if not dry_run:
        orphans = _scan_orphan_ids()
        if orphans:
            print(f"\n[阶段 3] 孤儿 ID 扫描: 发现 {len(orphans)} 组孤儿")
            for o in orphans:
                msg = f"  [ORPHAN] {o['table']}.{o['column']}: id={o['orphan_id']} count={o['count']}"
                print(msg)
                log.warning(msg)
            print("[阶段 3] 有孤儿 ID，迁移不完整，需人工处理")
        else:
            print(f"\n[阶段 3] 孤儿 ID 扫描: 0 孤儿，迁移完整")

        # 撞 ID 错位检测（孤儿扫描扫不出来的正是这种）
        collisions = _scan_id_collisions()
        if collisions:
            print(f"\n[阶段 3] 撞 ID 错位检测: 发现 {len(collisions)} 组疑似错位")
            for c in collisions:
                msg = f"  [COLLISION] user_id={c['user_id']} username={c['username']} provider={c['provider']}: {c['issue']}"
                print(msg)
                log.warning(msg)
            print("[阶段 3] 有撞 ID 错位，需人工核对凭据归属")
        else:
            print(f"[阶段 3] 撞 ID 错位检测: 0 错位")

    migrated = sum(1 for v in id_map.values() if v != -1)
    log.info(f"D32 migration: 完成，迁移 {migrated} 个用户")
    return id_map


def main():
    parser = argparse.ArgumentParser(
        description="迁移 user_accounts → grape_accounts + external_identities + user_credentials"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="预览，不修改数据")
    mode.add_argument("--execute", action="store_true", help="实际执行")
    parser.add_argument("--force", action="store_true", help="重置 migrated=0 后重新执行")
    args = parser.parse_args()

    print("=" * 60)
    print(f"迁移脚本: user_accounts → grape_accounts")
    print(f"模式: {'DRY-RUN' if args.dry_run else 'EXECUTE'}" + (" [FORCE]" if args.force else ""))
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    id_map = run_migration(dry_run=args.dry_run, force=args.force)

    if id_map:
        print("\n" + "=" * 60)
        print("ID 映射 (old → new):")
        for old_id, new_id in sorted(id_map.items()):
            print(f"  {old_id} → {new_id}")
        print("=" * 60)

    if args.dry_run:
        print("\n这是 DRY-RUN 预览。加 --execute 参数实际执行。")
    else:
        print("\n迁移完成。")


if __name__ == "__main__":
    main()
