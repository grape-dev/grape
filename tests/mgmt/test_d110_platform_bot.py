"""D110：bot 归平台行（一平台一个）——模型/仓储/读链口径锁。

覆盖：
- DDL 含 bot_token_enc / bot_token_status / bot_checked_at 三列
- 配置导入播种规则：bot_username 只在空值时填一次，之后导入不覆盖
- enabled 等其它写保护列维持硬保护（0 不被配置复位）
- set_bot / mark_bot_status / 清除语义
- 取 token 读序：平台行 → meta
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL
from src.kernel.persistence.repositories_platform_object import (
    RepoPlatformRepo,
    _apply_fields,
)

_RC_DDL = (
    "CREATE TABLE IF NOT EXISTS grape_t_repo_credentials ("
    " id VARCHAR(256) PRIMARY KEY, repo_key TEXT NOT NULL, cred_type TEXT NOT NULL,"
    " value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
    " created_at TEXT NOT NULL DEFAULT '', derived_from TEXT NOT NULL DEFAULT '',"
    " UNIQUE (repo_key, cred_type))"
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    raw = engine.raw_connection()
    raw.executescript(PLATFORM_OBJECT_DDL)
    raw.execute(_RC_DDL)
    raw.commit()
    s = Session(engine)
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def test_platform_ddl_has_bot_columns(session):
    from sqlite3 import connect

    # 用 ORM 建表后的内存库无法直接 PRAGMA，改为按模型列断言 + DDL 文本断言
    from src.kernel.persistence.models_platform_object import RepoPlatformModel

    cols = {c.key for c in RepoPlatformModel.__mapper__.columns}
    assert {"bot_username", "bot_token_enc", "bot_token_status", "bot_checked_at"} <= cols
    assert "bot_token_enc       TEXT NOT NULL DEFAULT ''" in PLATFORM_OBJECT_DDL
    assert "bot_token_status    TEXT NOT NULL DEFAULT 'unknown'" in PLATFORM_OBJECT_DDL


def test_config_import_seeds_bot_username_only_when_empty(session):
    assert RepoPlatformRepo.upsert_from_config(
        session, "gitcode", {"name": "GitCode", "bot_username": "card_bot"}, "h1"
    ) == "inserted"
    session.commit()
    row = RepoPlatformRepo.get(session, "gitcode")
    assert row.bot_username == "card_bot"          # 插入时播种

    RepoPlatformRepo.set_bot(session, "gitcode", username="admin_bot")  # 管理面改名
    session.commit()
    RepoPlatformRepo.upsert_from_config(
        session, "gitcode", {"name": "GitCode v2", "bot_username": "card_bot"}, "h2"
    )
    session.commit()
    row = RepoPlatformRepo.get(session, "gitcode")
    assert row.name == "GitCode v2"                # 普通字段照常更新
    assert row.bot_username == "admin_bot"         # 写保护列不被导入覆盖


def test_enabled_is_not_revived_by_config(session):
    RepoPlatformRepo.upsert_from_config(session, "gitee", {"name": "Gitee"}, "h1")
    session.commit()
    RepoPlatformRepo.set_enabled(session, "gitee", False)
    session.commit()
    row = RepoPlatformRepo.get(session, "gitee")
    _apply_fields(row, {"enabled": 1})
    assert row.enabled == 0                        # 0 是有效状态，硬保护列不播种


def test_set_mark_clear_bot(session):
    RepoPlatformRepo.upsert_from_config(session, "gitcode", {"name": "GitCode"}, "h1")
    session.commit()
    RepoPlatformRepo.set_bot(session, "gitcode", username="bot_a", token_enc="CIPHER")
    session.commit()
    row = RepoPlatformRepo.get(session, "gitcode")
    assert row.bot_token_enc == "CIPHER" and row.bot_token_status == "unknown"

    RepoPlatformRepo.mark_bot_status(session, "gitcode", "expired")
    session.commit()
    row = RepoPlatformRepo.get(session, "gitcode")
    assert row.bot_token_status == "expired" and row.bot_checked_at

    RepoPlatformRepo.set_bot(session, "gitcode", token_enc="")   # 清除 token
    session.commit()
    row = RepoPlatformRepo.get(session, "gitcode")
    assert row.bot_token_enc == "" and row.bot_token_status == ""
    assert row.bot_username == "bot_a"             # 名字保留


def test_token_read_priority_platform_then_meta(monkeypatch):
    from src.domains.mgmt.user import db as user_db

    monkeypatch.setattr(user_db, "_platform_bot_token", lambda p: "PLATFORM_TOKEN")
    assert user_db.get_global_bot_token_for("gitcode") == "PLATFORM_TOKEN"

    monkeypatch.setattr(user_db, "_platform_bot_token", lambda p: "")
    monkeypatch.setattr(user_db, "_cached_get_meta", lambda key, file_fallback_fn=None: "META_TOKEN")
    assert user_db.get_global_bot_token_for("gitcode") == "META_TOKEN"


# ---------- 退役面守卫 + 轮换传播 ----------

def test_retired_catalog_has_no_runtime_readers():
    """grape_t_bot_accounts 退役后，除迁移里的"若表存在则兜底"外不得再被读写（UC-D110-08）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent / "src"
    import re

    allow = {"src/api/db_setup.py"}  # 202611170014 的"存在才取默认行"兼容分支
    sql_ref = re.compile(r"(?:SELECT|INSERT|UPDATE|DELETE)\b[\s\S]{0,120}?grape_t_bot_accounts", re.I)
    offenders = []
    for f in root.rglob("*.py"):
        rel = f.relative_to(root.parent).as_posix()
        if "__pycache__" in f.parts or rel in allow:
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for needle in ("BotAccountRepo", "BotAccountModel"):
            if needle in text:
                offenders.append(f"{rel}: {needle}")
        for m in sql_ref.finditer(text):
            offenders.append(f"{rel}: SQL 引用退役表 ({m.group(0)[:40]}…)")
    assert not offenders, "退役表的代码引用回潮：\n" + "\n".join(offenders)


def test_rotation_triggers_swap_only_when_token_changes(monkeypatch):
    """D110 R1：换 token 必须触发 webhook 传播；同值不触发（UC-D110-04）。"""
    from src.domains.mgmt.admin import config_store

    calls = []
    monkeypatch.setattr(config_store, "_global_bot_token_swap_wrapper",
                        lambda p, o, n: calls.append((p, o, n)))
    config_store.trigger_platform_bot_swap("gitcode", "old", "new")
    assert calls == [("gitcode", "old", "new")]

    config_store.trigger_platform_bot_swap("gitcode", "same", "same")
    config_store.trigger_platform_bot_swap("gitcode", "", "new")
    assert calls == [("gitcode", "old", "new")]  # 空旧值（首次配置）不传播，也没东西可刷


def test_gitee_bot_identity_prefers_platform_row(monkeypatch):
    """gitee 的历史来源链只认 meta 单值键/卡；D110 后平台行必须优先（UC-D110-05）。"""
    from src.domains.mgmt.user import db as udb
    from src.domains.mgmt.user import repo_manager

    monkeypatch.setattr(udb, "_platform_bot_row", lambda p: {"username": "plat_gitee_bot"})
    assert repo_manager.resolve_bot_username("gitee") == "plat_gitee_bot"

    monkeypatch.setattr(udb, "_platform_bot_row", lambda p: {})
    monkeypatch.setattr(udb, "GLOBAL_BOT_USERNAME_SOURCES",
                        {"gitee": lambda: ["meta_gitee_bot"]}, raising=False)
    assert repo_manager.resolve_bot_username("gitee") == "meta_gitee_bot"


def test_gitee_token_chain_is_platform_then_meta(monkeypatch):
    """gitee 无文件 fallback（_GLOBAL_BOT_FILE_FIELDS 只有 gitcode/github）：平台行 → meta。"""
    from src.domains.mgmt.user import db as udb

    monkeypatch.setattr(udb, "_platform_bot_token", lambda p: "plat_token")
    assert udb.get_global_bot_token_for("gitee") == "plat_token"

    monkeypatch.setattr(udb, "_platform_bot_token", lambda p: "")
    monkeypatch.setattr(udb, "_cached_get_meta",
                        lambda key, file_fallback_fn=None: "meta_token" if key == "global_bot_gitee_token" else "")
    assert udb.get_global_bot_token_for("gitee") == "meta_token"


def test_bot_seed_parser_accepts_legacy_and_platform_bot_array(monkeypatch, tmp_path):
    """种子文件两种写法并存：旧扁平键 + 新 platform_bot 数组（含平台别名/笔误）归一化。"""
    import json

    import src.kernel.config as cfg

    (tmp_path / "global-bot-token.json").write_text(json.dumps({
        "gitcode_api_token": "gc",
        "github_bot_username": "gh",
        "gitee_bot_token": "flat-gitee",
        "platform_bot": [
            {"platform_type": "gitee", "bot_name": "gitee-bot", "bit_token": "arr-gitee"},
            {"provider": "gitlab", "username": "gitlab-bot", "token": "gitlab-token"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(tmp_path))

    seeds = cfg.read_bot_seed_file()
    assert seeds["gitcode"] == {"token": "gc"}
    assert seeds["github"] == {"username": "gh"}
    # 同字段数组优先（bit_token 是 bot_token 的兼容写法）
    assert seeds["gitee"] == {"username": "gitee-bot", "token": "arr-gitee"}
    assert seeds["gitlab"] == {"username": "gitlab-bot", "token": "gitlab-token"}


def test_bot_seed_parser_prefers_original_file_name(monkeypatch, tmp_path):
    """原名 global-bot-token.json 优先；platform-bots.json 只作兼容兜底。"""
    import json

    import src.kernel.config as cfg

    (tmp_path / "global-bot-token.json").write_text(json.dumps({"gitcode_api_token": "original"}),
                                                    encoding="utf-8")
    (tmp_path / "platform-bots.json").write_text(json.dumps({"gitcode_api_token": "alias"}),
                                                 encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_DIR", str(tmp_path))
    assert cfg.read_bot_seed_file()["gitcode"]["token"] == "original"

    (tmp_path / "global-bot-token.json").unlink()
    assert cfg.read_bot_seed_file()["gitcode"]["token"] == "alias"


def test_platform_bot_endpoint_writes_encrypted_and_clears(session, monkeypatch):
    """新端点：写 token 加密、空 token 不改、清除只清 token（UC-D110-03/04）。"""
    import src.kernel.persistence.engine as engine_mod
    from src.domains.mgmt.admin import repo_platform_routes as R
    from src.domains.mgmt.auth.credential_crypto import decrypt

    RepoPlatformRepo.upsert_from_config(session, "gitee", {"name": "Gitee"}, "h")
    session.commit()
    # 用同一个 engine，并模拟 uow 的"提交点收敛在出口"：
    # 生产 uow_write 会 commit；测试共享内存连接若不提交，端点内部再开的 Session 关闭时会回滚。
    monkeypatch.setattr(engine_mod, "get_engine", lambda: session.get_bind())
    monkeypatch.setattr(R, "uow_write", lambda fn: (fn(session), session.commit()))

    # 本分支起：存令牌会用平台 /user 回答「我是谁」（28c9d6f6c）。测试不出网，
    # 所以在这里打桩校验结果；断言不变。
    import src.domains.mgmt.admin.config_store as CS

    monkeypatch.setattr(CS, "verify_token", lambda p, t: {"login": "gitee-bot"})

    out = R.set_platform_bot("gitee", R.BotBody(username="gitee-bot", token="tok-1"),
                             admin={"username": "t"})
    assert out["bot"]["token_present"] is True and "token" not in out["bot"]
    session.expire_all()
    row = RepoPlatformRepo.get(session, "gitee")
    assert row.bot_username == "gitee-bot" and decrypt(row.bot_token_enc) == "tok-1"

    # 只改名、token 留空 → 不改 token
    R.set_platform_bot("gitee", R.BotBody(username="renamed"), admin={"username": "t"})
    session.expire_all()
    row = RepoPlatformRepo.get(session, "gitee")
    assert row.bot_username == "renamed" and decrypt(row.bot_token_enc) == "tok-1"

    R.clear_platform_bot("gitee", admin={"username": "t"})
    session.expire_all()
    row = RepoPlatformRepo.get(session, "gitee")
    assert row.bot_token_enc == "" and row.bot_username == "renamed"


def _fake_account_catalog(monkeypatch, tmp_path, exportable=True):
    """造一张账号目录表 + 把迁移用的 db/db_lock 指过去（不碰真实 data/）。"""
    import sqlite3
    import src.kernel.session_manager as sm

    class _NoopLock:
        """迁移里 `with db_lock:` 的替身：测试不并发，直接放行。"""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE grape_t_bot_accounts "
                 "(id TEXT, provider TEXT, username TEXT, token_enc TEXT)")
    conn.execute("INSERT INTO grape_t_bot_accounts VALUES ('1','gitcode','bot-a','derived:x:y')")
    conn.execute("INSERT INTO grape_t_bot_accounts VALUES ('2','github','bot-b','derived:x:z')")
    conn.commit()

    monkeypatch.setattr(sm, "db", conn, raising=False)
    monkeypatch.setattr(sm, "db_lock", _NoopLock(), raising=False)
    return conn


def test_drop_bot_accounts_exports_before_dropping(monkeypatch, tmp_path):
    """先导出含密文的整表快照，导出成功才 DROP（UC-D110-08）。"""
    import json
    from src.api import db_setup

    conn = _fake_account_catalog(monkeypatch, tmp_path)
    fake_dir = tmp_path / "data"
    monkeypatch.setattr(db_setup, "__file__", str(tmp_path / "src" / "api" / "db_setup.py"))

    db_setup._fn_drop_bot_accounts_table()

    backups = list(fake_dir.glob("bot_accounts-backup-*.json"))
    assert len(backups) == 1
    rows = json.loads(backups[0].read_text(encoding="utf-8"))["rows"]
    assert {r["username"] for r in rows} == {"bot-a", "bot-b"}
    # 表已删：再查一次必须报错（幂等路径）
    with pytest.raises(sqlite3_error()):
        conn.execute("SELECT * FROM grape_t_bot_accounts")
    db_setup._fn_drop_bot_accounts_table()  # 幂等：表不在，安静跳过


def sqlite3_error():
    import sqlite3
    return sqlite3.OperationalError


def test_drop_bot_accounts_keeps_table_when_export_fails(monkeypatch, tmp_path):
    """导出写盘失败 → 拒绝 DROP，死表留着也不丢数据。"""
    from src.api import db_setup

    conn = _fake_account_catalog(monkeypatch, tmp_path)
    monkeypatch.setattr(db_setup, "__file__", str(tmp_path / "src" / "api" / "db_setup.py"))
    import builtins
    real_open = builtins.open

    def _boom(path, *a, **kw):
        if "bot_accounts-backup" in str(path):
            raise OSError("disk full")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", _boom)
    db_setup._fn_drop_bot_accounts_table()
    # 表还在、数据还在
    assert conn.execute("SELECT count(*) FROM grape_t_bot_accounts").fetchone()[0] == 2
    assert not list((tmp_path / "data").glob("bot_accounts-backup-*.json"))


def test_is_ciphertext_distinguishes_plaintext_from_ciphertext():
    """平台行落的是明文还是密文，必须能自证（014 灌进的是历史明文 PAT）。"""
    from src.domains.mgmt.auth.credential_crypto import encrypt, is_ciphertext

    assert not is_ciphertext("")
    assert not is_ciphertext("ghp_abcdefghijklmnopqrstuvwxyz0123456789")  # secrets-check: allow
    assert not is_ciphertext("gitcode-token:with-colon:inside")
    assert is_ciphertext(encrypt("ghp_real"))


def test_migration_encrypts_plaintext_platform_bot_token(monkeypatch):
    """019：明文就地加密、密文原样不动、二次执行无改动。"""
    import sqlite3
    import src.kernel.session_manager as sm
    from src.api import db_setup
    from src.domains.mgmt.auth.credential_crypto import decrypt, encrypt, is_ciphertext

    class _NoopLock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE grape_t_repo_platforms "
                 "(provider TEXT, bot_token_enc TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO grape_t_repo_platforms VALUES ('gitcode','plain-token-1','')")
    conn.execute("INSERT INTO grape_t_repo_platforms VALUES ('github',?,'')",
                 (encrypt("already-cipher"),))
    conn.commit()
    monkeypatch.setattr(sm, "db", conn, raising=False)
    monkeypatch.setattr(sm, "db_lock", _NoopLock(), raising=False)

    db_setup._fn_encrypt_platform_bot_tokens()

    rows = dict(conn.execute("SELECT provider, bot_token_enc FROM grape_t_repo_platforms"))
    assert is_ciphertext(rows["gitcode"]) and decrypt(rows["gitcode"]) == "plain-token-1"
    assert decrypt(rows["github"]) == "already-cipher"
    before = dict(rows)

    db_setup._fn_encrypt_platform_bot_tokens()  # 幂等：全部已是密文
    assert dict(conn.execute("SELECT provider, bot_token_enc FROM grape_t_repo_platforms")) == before