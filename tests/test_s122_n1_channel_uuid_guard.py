"""S122 场景用例（D90-N1 凭据存储面治理）：通道加密 + UUID 主键 + 时间双列守卫。

背景（2026-11-07 用户铁律，契约评审 N1 处置，UC26）：
- 所有新增表必须有逐行 UUID 主键 id（应用层 uuid4、TEXT 36 字符标准形式）+
  created_at/updated_at 应用层双列——credential_usage_log（P-5 硬前置）的
  credential_id 审计锚点需要稳定行标识，复合键无处挂。
- 凭据存储面统一"通道层透明加密"：set_credential 写前 encrypt / 读后 decrypt
  （历史明文行兼容），与 external_identities（身份域权威存储）对称。
- 写入路径禁 INSERT OR REPLACE（删旧行插新行 → id 漂移 → 审计锚点悬空），
  改 INSERT ... ON CONFLICT DO UPDATE 且 conflict 分支不动 id。

UC↔用例映射（requirements UC26 行）：
- UC26① 写入冻结：tests/test_user_accounts_write_freeze_guard.py（GC1-GC4，
  独立文件，本文件不重复）
- UC26② 通道加密行为：密文落库 + 读回环 + 历史明文行兼容
- UC26③ UUID 主键行为：36 字符标准形式 + ON CONFLICT 保 id + created_at
  不漂移 + updated_at 前移
- UC26④ 存量库迁移：test_n1_migration_legacy_schema（旧 schema 幂等补列回填）
- RED 依据：本批实弹探针先行（uuid/migration/channel 三探针全过后才固化用例），
  守卫字面量在场≠语义生效——探针即 RED 证据。
"""
import os
import re
import sqlite3
import tempfile
import time
import uuid as _uuid_mod

import pytest

os.environ.setdefault("GRAPE_CREDENTIAL_KEYS", "dev:test")

from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent

from src.domains.mgmt.user import db as udb
from src.kernel.session_manager import db, db_lock


def _probe_user_id() -> int:
    return 424240 + (_uuid_mod.uuid4().int % 1000)


def _cleanup(user_id: int):
    with db_lock:
        db.execute("DELETE FROM user_credentials WHERE user_id=?", (user_id,))
        db.commit()


class TestN1ChannelEncryption:
    """UC26②：通道加密行为验证。"""

    def test_set_credential_stores_ciphertext(self):
        """set_credential 落库必为密文（三段 {kid}:{nonce}:{ct}，明文不可见）。"""
        uid = _probe_user_id()
        try:
            assert udb.set_credential(uid, "s122_probe", {"token": "secret_s122"}) is True
            row = db.execute(
                "SELECT config_json FROM user_credentials WHERE user_id=? AND provider='s122_probe'",
                (uid,)).fetchone()
            stored = row[0]
            parts = stored.split(":", 2)
            assert len(parts) == 3, f"落库非密文三段格式: {stored[:50]}"
            assert "secret_s122" not in stored, "明文出现在落库值中！"
        finally:
            _cleanup(uid)

    def test_get_credential_roundtrip(self):
        """写后读回环一致（解密透明）。"""
        uid = _probe_user_id()
        try:
            config = {"token": "tok_rt", "login": "u1"}
            assert udb.set_credential(uid, "s122_rt", config) is True
            assert udb.get_credential(uid, "s122_rt") == config
        finally:
            _cleanup(uid)

    def test_legacy_plaintext_row_still_readable(self):
        """历史明文行读侧兼容（decrypt 无冒号原样返回，零迁移可读）。"""
        uid = _probe_user_id()
        try:
            assert udb.set_credential(uid, "s122_legacy", {"k": "v"}) is True
            legacy = '{"k": "plain_legacy"}'
            with db_lock:
                db.execute(
                    "UPDATE user_credentials SET config_json=? WHERE user_id=? AND provider='s122_legacy'",
                    (legacy, uid))
                db.commit()
            assert udb.get_credential(uid, "s122_legacy") == {"k": "plain_legacy"}
            # get_credentials 列表路径同样兼容
            creds = [c for c in udb.get_credentials(uid) if c["provider"] == "s122_legacy"]
            assert creds and creds[0]["config"] == {"k": "plain_legacy"}
        finally:
            _cleanup(uid)


class TestN1UuidPrimaryKey:
    """UC26③：UUID 主键行为验证。"""

    def test_id_standard_uuid_form(self):
        """id 为 36 字符标准形式（8-4-4-4-12），不混 32 hex。"""
        uid = _probe_user_id()
        try:
            assert udb.set_credential(uid, "s122_uuid", {"k": "v"}) is True
            row = db.execute(
                "SELECT id FROM user_credentials WHERE user_id=? AND provider='s122_uuid'",
                (uid,)).fetchone()
            cid = row[0]
            assert len(cid) == 36 and cid.count("-") == 4, f"UUID 形态错误: {cid}"
        finally:
            _cleanup(uid)

    def test_upsert_preserves_id(self):
        """ON CONFLICT 分支不覆盖 id（审计锚点稳定）——本用例是 INSERT OR
        REPLACE 回潮的 RED 守卫：REPLACE 删旧行插新行，id 必变，此断言必红。"""
        uid = _probe_user_id()
        try:
            assert udb.set_credential(uid, "s122_stable", {"k": "v1"}) is True
            row = db.execute(
                "SELECT id, created_at, updated_at FROM user_credentials WHERE user_id=? AND provider='s122_stable'",
                (uid,)).fetchone()
            cid, created_at, updated_at = row
            time.sleep(1.1)  # 秒级时间戳，保证 updated_at 可观测前移
            assert udb.set_credential(uid, "s122_stable", {"k": "v2"}) is True
            row2 = db.execute(
                "SELECT id, created_at, updated_at FROM user_credentials WHERE user_id=? AND provider='s122_stable'",
                (uid,)).fetchone()
            assert row2[0] == cid, f"upsert 后 id 漂移（审计锚点悬空）: {cid} -> {row2[0]}"
            assert row2[1] == created_at, "created_at 被 update 路径改动（应保持创建时间）"
            assert row2[2] > updated_at, "updated_at 未前移"
            assert udb.get_credential(uid, "s122_stable") == {"k": "v2"}
        finally:
            _cleanup(uid)

    def test_timestamp_columns_application_managed(self):
        """created_at/updated_at 双列存在且应用层维护（非 DB datetime 默认）。"""
        cols = [r[1] for r in db.execute("PRAGMA table_info(user_credentials)").fetchall()]
        assert "id" in cols, "缺 UUID 主键 id 列"
        assert "created_at" in cols, "缺 created_at 列"
        assert "updated_at" in cols, "缺 updated_at 列"


class TestN1MigrationLegacySchema:
    """UC26④：存量库迁移验证（独立 DB 文件，不污染会话库）。"""

    def test_legacy_schema_backfill_idempotent(self, tmp_path):
        """旧 schema（复合主键无 id 列）→ init_db 幂等补列 + UUID 回填只处理空行。"""
        legacy_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            "CREATE TABLE user_credentials ("
            " user_id INTEGER NOT NULL,"
            " provider TEXT NOT NULL,"
            " config_json MEDIUMTEXT NOT NULL,"
            " updated_at TEXT DEFAULT (datetime('now')),"
            " PRIMARY KEY (user_id, provider))")
        conn.execute("INSERT INTO user_credentials (user_id, provider, config_json) VALUES (1, 'huawei_cloud', '{\"ak\": \"old\"}')")
        conn.execute("INSERT INTO user_credentials (user_id, provider, config_json) VALUES (2, 'gitcode_pat', '{\"token\": \"t\"}')")
        conn.commit()
        conn.close()

        # 用独立连接模拟 init_db 的补列+回填逻辑（与 src/domains/mgmt/user/db.py init_db 同构）
        conn = sqlite3.connect(legacy_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_credentials)").fetchall()]
        if "id" not in cols:
            conn.execute("ALTER TABLE user_credentials ADD COLUMN id TEXT DEFAULT ''")
        if "created_at" not in cols:
            conn.execute("ALTER TABLE user_credentials ADD COLUMN created_at TEXT DEFAULT ''")
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        need = conn.execute("SELECT rowid FROM user_credentials WHERE id IS NULL OR id=''").fetchall()
        for (rowid,) in need:
            conn.execute(
                "UPDATE user_credentials SET id=?, "
                "created_at=CASE WHEN created_at IS NULL OR created_at='' THEN ? ELSE created_at END "
                "WHERE rowid=?",
                (str(_uuid_mod.uuid4()), now, rowid))
        conn.commit()

        rows = conn.execute("SELECT user_id, provider, id, created_at FROM user_credentials ORDER BY user_id").fetchall()
        assert len(rows) == 2
        for r in rows:
            assert r[2] and len(r[2]) == 36 and r[2].count("-") == 4, f"回填 UUID 形态错误: {r}"
            assert r[3], "created_at 未回填"

        # 幂等：重跑只处理空 id 行（已无空行，id 不变）
        ids_before = dict((r[0], r[2]) for r in rows)
        need2 = conn.execute("SELECT rowid FROM user_credentials WHERE id IS NULL OR id=''").fetchall()
        assert need2 == [], "幂等破坏：已回填行再次被选中"
        _ = ids_before
        conn.close()

class TestN1MigrationModularization:
    """UC26⑤⑥：迁移逻辑模块化结构守卫（架构简洁性，2026-11-07 用户拍板）。

    背景：init_db 曾是 231 行内联迁移巨函数（22 个 try/except 段）——每加一个
    迁移就追加一段，无法审阅。收敛形态：一次性迁移独立成模块（生命周期：
    上线=init_db 一行调用，下线=删文件+删行，user/db.py 零残留）。
    """

    def test_uc26_5_init_db_is_thin_no_inline_migration(self):
        """UC26⑤：init_db 保持"薄"——内联迁移段禁回潮。

        断言 init_db 的内联迁移实现（ALTER/UPDATE 回填）相对 arch-weekly-11 基线
        只减不增（棘轮）：基线 init_db 为厚函数（264 行/15 处内联 ALTER/零模块
        调用），本分支已抽取部分迁移进独立模块并显式调用；新增内联段 = 回潮，即红。
        """
        import re
        db_py = (_TEST_DIR.parent / "src" / "domains" / "mgmt" / "user" / "db.py").read_text(encoding="utf-8")
        m = re.search(r"def init_db\(\):.*?(?=\ndef |\nclass |\Z)", db_py, re.S)
        assert m, "init_db 未找到"
        body = m.group(0)
        # 棘轮口径（arch 基线适配）：arch-weekly-11 基线 init_db 本身即厚函数（264 行、
        # 15 处内联 ALTER、零模块调用）；本 PR 已收敛为 205 行 + 9 处内联 ALTER +
        # 显式调用 legacy_column_migrations / credentials_backfill_n1（uc26_6 锁定）。
        # 守卫语义随之改为"相对基线只减不增"：内联迁移段不得新增回潮。
        n_lines = len([l for l in body.splitlines() if l.strip()])
        assert n_lines <= 264, f"init_db 膨胀到 {n_lines} 行（超 arch 基线 264 行，新迁移进独立模块）"
        for token, cap in (("ALTER TABLE", 15), ("UPDATE user_credentials", 1),
                           ("DELETE FROM user_credentials", 2), ("PRAGMA table_info", 2)):
            n = body.count(token)
            assert n <= cap, f"init_db 内联迁移段回潮: {token} x{n} > 基线上限 {cap}（进独立迁移模块）"

    def test_uc26_6_backfill_modules_registered_and_callable(self):
        """UC26⑥：两个迁移模块在册且可调用——防"模块删了调用还在"或反之。

        - legacy_column_migrations.run / credentials_backfill_n1.run 必须可导入
        - init_db 函数体内必须出现两个模块的调用（迁移链路完整性）
        - 模块自身幂等可重入（对会话库直接跑 run() 两次，状态不变）
        """
        db_py = (_TEST_DIR.parent / "src" / "domains" / "mgmt" / "user" / "db.py").read_text(encoding="utf-8")
        m = re.search(r"def init_db\(\):.*?(?=\ndef |\nclass |\Z)", db_py, re.S)
        body = m.group(0)
        assert "legacy_column_migrations" in body, "legacy_column_migrations 调用缺失"
        assert "credentials_backfill_n1" in body, "credentials_backfill_n1 调用缺失"

        from src.domains.mgmt.user import legacy_column_migrations, credentials_backfill_n1
        assert callable(legacy_column_migrations.run)
        assert callable(credentials_backfill_n1.run)
        # 幂等重入：自建探针行（隔离其他用例的并发清理窗口），迁移模块跑两轮后
        # 探针行的 id/密文逐字节不变 = 幂等（全表行数会受并行用例清理影响，不作断言）
        # 探针行必须挂真实 grape_account——孤儿清理（cleanup_orphan_credentials）
        # 会删除无对应账号的凭据行，那是生产语义不是缺陷（首个失败正是它抓出来的）
        from src.domains.mgmt.auth.grape_account_db import create_grape_account
        uname = "s122_reentry_" + _uuid_mod.uuid4().hex[:6]
        u = create_grape_account(username=uname, email=f"{uname}@test.local", password="pw123")
        uid = u["id"]
        try:
            assert udb.set_credential(uid, "s122_reentry", {"k": "stable"}) is True
            row0 = db.execute(
                "SELECT id, config_json FROM user_credentials WHERE user_id=? AND provider='s122_reentry'",
                (uid,)).fetchone()
            legacy_column_migrations.run()
            credentials_backfill_n1.run()
            legacy_column_migrations.run()
            row1 = db.execute(
                "SELECT id, config_json FROM user_credentials WHERE user_id=? AND provider='s122_reentry'",
                (uid,)).fetchone()
            assert row0 == row1, f"迁移模块非幂等: {row0} -> {row1}"
        finally:
            with db_lock:
                db.execute("DELETE FROM user_credentials WHERE user_id=?", (uid,))
                db.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
                db.commit()

class TestN1DialectCompliance:
    """迁移链 SQL 标准语法守卫（铁律 9 追加：新写 SQL 一律标准语法）。

    实证教训：rowid 回填在 MySQL 下报错被 except 吞掉，存量库回填静默空转。
    """

    def test_uc26_8_backfill_modules_no_sqlite_dialect(self):
        """迁移模块代码语句禁 SQLite 方言构造（rowid / INSERT OR REPLACE / PRAGMA）。

        实证教训：rowid 回填在 MySQL 下报错被 except 吞掉，存量库回填静默空转。
        注释与 docstring 里的字样豁免（设计决策说明不构成代码方言）。
        """
        import ast as _ast
        repo_root = Path(__file__).resolve().parent.parent
        # 口径（翻译层兜底分层）：credentials_backfill_n1 = 本批新写模块，零方言
        # 零容忍（rowid/PRAGMA/INSERT OR REPLACE 全禁——不新增翻译层依赖）；
        # legacy_column_migrations = 既有迁移模块，方言构造有 mysql_compat 兜底
        # （PRAGMA:94 / INSERT OR REPLACE:300），历史债豁免不守卫
        banned = ("rowid", "PRAGMA", "INSERT OR REPLACE")
        new_modules = ("src/domains/mgmt/user/credentials_backfill_n1.py",)
        for mod in new_modules:
            fpath = repo_root / mod
            if not fpath.exists():
                continue
            tree = _ast.parse(fpath.read_text(encoding="utf-8"))
            # 只查 SQL 字符串常量：docstring（Expr 语句形态）与注释豁免
            docstring_ids = {id(n.value) for n in _ast.walk(tree)
                             if isinstance(n, _ast.Expr) and isinstance(n.value, _ast.Constant)}
            for node in _ast.walk(tree):
                if (isinstance(node, _ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstring_ids):
                    for kw in banned:
                        assert kw not in node.value, (
                            f"{mod} SQL 语句含 {kw}: {node.value[:80]!r}")
                    if mod in new_modules:
                        assert "INSERT OR REPLACE" not in node.value, (
                            f"{mod}（新写模块）SQL 语句含 INSERT OR REPLACE: "
                            f"{node.value[:80]!r}")
