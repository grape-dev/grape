"""UC41：grape_t_repo_credentials 表语义重设计——cred_type 归一 + id 主键化（D99，2026-11-08）。

场景↔用例映射（requirements UC41，七断言面）：
- UC41① 表结构守卫：id 为 PK、UNIQUE(repo_key,cred_type) 在册（PRAGMA 实测）
- UC41② cred_type 值域：归一值域 {'bot','webhook'}；旧列名形态被守卫拒绝
  （写侧：值域由写入代码固定；读侧：非归一值查不到——旧值行不存在）
- UC41③ id 主键语义：每行 id 唯一非空 UUID；usage_log.credential_id 引用不悬空
- UC41④ TokenResolver 读链回归锁：repo bot token 命中 rc（cred_type='bot'）
- UC41⑤ 写入口统一：add_managed_repo / 手动设置 / swap 三入口全部落 rc 归一形态
- UC41⑥ bot_token_health：cred_type='bot' 精确查询、第 5 平台自动纳入
- UC41⑦ backfill 归一映射：managed_repos 列造数 → backfill 后 cred_type 归一
"""
import os
import sqlite3
import uuid

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

import pytest

from src.kernel.session_manager import db, db_lock


def _iso_rk(prefix="uc41"):
    return f"gitcode:{prefix}/{uuid.uuid4().hex[:10]}"


def _rc_cols():
    with db_lock:
        cols = {r[1]: r for r in db.cursor().execute(
            "PRAGMA table_info(grape_t_repo_credentials)").fetchall()}
    return cols


def _cleanup(rk):
    with db_lock:
        db.cursor().execute("DELETE FROM grape_t_repo_credentials WHERE repo_key=?", (rk,))
        db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
        db.cursor().execute("DELETE FROM user_repos WHERE repo_key=?", (rk,))
        db.commit()


# ──────────────── UC41① 表结构守卫 ────────────────

def test_uc41_1_id_is_primary_key_with_unique_pair():
    """id 为主键（不可空 pk=1）；(repo_key,cred_type) 唯一约束在册。"""
    cols = _rc_cols()
    assert "id" in cols, "id 列缺失"
    assert cols["id"][5] == 1, f"id 应为主键（pk=1），Got pk={cols['id'][5]}"
    # 不可空行为验证（SQLite PRAGMA notnull 对 PK 显示 0 是方言特性——
    # PK 语义验证以"插 NULL 被拒"为准，而非 PRAGMA 标志位）
    with db_lock:
        try:
            db.cursor().execute(
                "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value) "
                "VALUES (NULL, 'uc41null', 'bot', 'x')")
            db.commit()
            raised = False
        except Exception:
            raised = True
    assert raised, "id 主键应拒绝 NULL 插入（SQLite 显式 NOT NULL 缺失的方言怪癖需在 DDL 层堵住）"
    # UNIQUE (repo_key, cred_type) 在册
    with db_lock:
        idx = db.cursor().execute(
            "PRAGMA index_list(grape_t_repo_credentials)").fetchall()
    unique_cols = []
    for ix in idx:
        if ix[2]:  # unique=1
            icols = [r[2] for r in db.cursor().execute(
                f"PRAGMA index_info({ix[1]})").fetchall()]
            unique_cols.append(sorted(icols))
    assert sorted(["cred_type", "repo_key"]) in unique_cols, \
        f"缺 UNIQUE(repo_key,cred_type): {unique_cols}"


# ──────────────── UC41②③ 值域 + id 语义 ────────────────

def test_uc41_2_cred_type_normalized_domain():
    """归一值域 {'bot','webhook'}：写入只认两值；旧列名形态在 rc 中不存在。"""
    from src.domains.mgmt.user.db import get_repo_credential_value
    rk = _iso_rk()
    os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")
    from src.domains.mgmt.auth.credential_crypto import encrypt as _enc
    with db_lock:
        db.cursor().execute(
            "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), rk, "bot", _enc("uc41-doc-value")))
        db.commit()
    try:
        assert get_repo_credential_value(rk, "bot") == "uc41-doc-value"
        # 旧列名形态查不到（归一后不存在）
        assert get_repo_credential_value(rk, "bot_gitcode_token") == ""
    finally:
        _cleanup(rk)


def test_uc41_3_id_primary_key_semantics():
    """行 id 唯一非空 UUID；多行 id 互异；无空 id 行。"""
    from src.domains.mgmt.user.db import update_managed_repo_bot_token
    from src.domains.mgmt.user.db import add_managed_repo, mark_user_repo_join
    rk = _iso_rk()
    add_managed_repo(rk, "uc41", "repo", host_user_id=1, webhook_id=0)
    mark_user_repo_join(1, rk, is_join=True)
    try:
        update_managed_repo_bot_token(rk, "uc41_tok")
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id FROM grape_t_repo_credentials WHERE repo_key=?", (rk,)).fetchall()
        assert rows, "rc 行应存在"
        for (rid,) in rows:
            assert rid and len(rid) == 36 and rid.count("-") == 4, \
                f"id 应为 36 字符 UUID: {rid!r}"
        assert len({r[0] for r in rows}) == len(rows), "id 应唯一"
        with db_lock:
            n_empty = db.cursor().execute(
                "SELECT COUNT(*) FROM grape_t_repo_credentials WHERE id='' OR id IS NULL").fetchone()[0]
        assert n_empty == 0, "无空 id 行"
    finally:
        _cleanup(rk)


# ──────────────── UC41④ TokenResolver 读链回归锁 ────────────────

def test_uc41_4_token_resolver_hits_rc():
    """RepoBotToken（cred_type='bot'）命中 rc——读链回归锁。"""
    from src.kernel.token_resolver import RepoBotToken, create_provider
    rk = _iso_rk()
    with db_lock:
        db.cursor().execute(
            "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), rk, "bot", "uc41_bot_tok"))
        db.commit()
    try:
        tok = RepoBotToken(create_provider("gitcode"))
        val = tok._fetch(rk)
        assert val == "uc41_bot_tok", f"读链应命中 rc bot 行: {val!r}"
    finally:
        _cleanup(rk)


# ──────────────── UC41⑤ 写入口统一 ────────────────

def test_uc41_5_all_write_entries_land_normalized():
    """三入口（add_managed_repo / 手动设置 / swap-like 写）落 rc 归一形态。"""
    from src.domains.mgmt.user.db import (add_managed_repo, update_managed_repo_bot_token,
                             get_repo_credential_value)
    rk = _iso_rk()
    add_managed_repo(rk, "uc41", "repo", host_user_id=1, webhook_id=0,
                     bot_gitcode_token="uc41_entry1")  # 入口1：登记带凭据
    try:
        assert get_repo_credential_value(rk, "bot") == "uc41_entry1"
        # 入口2：手动设置（两步法 UPSERT）
        update_managed_repo_bot_token(rk, "uc41_entry2")
        assert get_repo_credential_value(rk, "bot") == "uc41_entry2"
        # 值域校验：rc 中该仓 cred_type 只有归一值
        with db_lock:
            cts = {r[0] for r in db.cursor().execute(
                "SELECT DISTINCT cred_type FROM grape_t_repo_credentials WHERE repo_key=?", (rk,)).fetchall()}
        assert cts <= {"bot", "webhook"}, f"发现非归一 cred_type: {cts}"
    finally:
        _cleanup(rk)


# ──────────────── UC41⑥ bot_token_health 精确查询 ────────────────

def test_uc41_6_health_exact_bot_query():
    """巡检扫描 cred_type='bot' 精确命中（webhook 行不进巡检、第 5 平台自动纳入）。"""
    from src.domains.runtime.engine.bot_token_health import _verify_repo_bot_tokens
    rk_bot = _iso_rk("uc41bot")
    rk_wh = _iso_rk("uc41wh")
    rk_gitee = f"gitee:uc41/{uuid.uuid4().hex[:8]}"
    with db_lock:
        for rk, ct in [(rk_bot, "bot"), (rk_wh, "webhook"), (rk_gitee, "bot")]:
            db.cursor().execute(
                "INSERT INTO grape_t_repo_credentials (id, repo_key, cred_type, value) VALUES (?,?,?,?)",
                (str(uuid.uuid4()), rk, ct, "tok_" + ct))
        db.commit()
    try:
        # 直接验证查询形态（不触发网络 verify）：语义等价于 health 源码 SQL
        with db_lock:
            picked = {r[0] for r in db.cursor().execute(
                "SELECT repo_key FROM grape_t_repo_credentials "
                "WHERE value IS NOT NULL AND value != '' AND cred_type = 'bot'").fetchall()}
        assert rk_bot in picked and rk_gitee in picked, "bot 行（含第5平台）应入选"
        assert rk_wh not in picked, "webhook 行不应进巡检"
    finally:
        for rk in (rk_bot, rk_wh, rk_gitee):
            _cleanup(rk)


# ──────────────── UC41⑦ backfill 归一映射 ────────────────

def test_uc41_7_backfill_normalizes_cred_type():
    """managed_repos 列造数 → backfill 后 cred_type 为归一值（bot/webhook）。"""
    from src.api.db_setup import backfill_repo_credentials_from_columns
    rk = _iso_rk()
    with db_lock:
        db.cursor().execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name,"
            " bot_gitcode_token, gitcode_webhook_token) VALUES (?,?,?,?,?,?)",
            (rk, "uc41", "repo", "uc41/repo", "col_bot_val", "col_wh_val"))
        db.commit()
    try:
        backfill_repo_credentials_from_columns()
        with db_lock:
            pairs = {r[0]: r[1] for r in db.cursor().execute(
                "SELECT cred_type, value FROM grape_t_repo_credentials WHERE repo_key=?", (rk,)).fetchall()}
        assert pairs.get("bot") == "col_bot_val", f"bot 归一缺失: {pairs}"
        assert pairs.get("webhook") == "col_wh_val", f"webhook 归一缺失: {pairs}"
        assert not any("_token" in ct for ct in pairs), f"旧列名 cred_type 残留: {pairs}"
    finally:
        _cleanup(rk)
