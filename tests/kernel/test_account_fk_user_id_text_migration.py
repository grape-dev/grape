"""账号域 user_id 族外键列 TEXT 归一迁移行为锁（202611091000，D105 根修 A）。

背景：grape_accounts.id 自 D105 第二批起用 uuid7_str()（36 字符），但全库
30 张表的 *user_id 族外键列旧 DDL 仍是 INTEGER——SQLite 混型同列互查不中
（int 24 != str '24'，实锤 binding_tickets 本人转移票秒 410），MySQL INT 列
严格模式直接写崩。迁移归一为 TEXT 亲和 + 全列 text 存储：
- 存量 int 值保值（24 → '24' 字符串形态，不丢数据）；
- str 参数直接命中 text 存储（uuid 与存量 int 字符串同列可查）；
- 幂等：二次执行亲和判定命中 TEXT 分支且无混型行 → 零改动。
"""
import os, sqlite3
import importlib
import pytest

from src.api.db_setup import (
    _normalize_account_fk_user_id_text,
    _ACCOUNT_FK_USER_ID_COLS,
)


@pytest.fixture(autouse=True)
def _restore_global_db_state():
    """测试体切换 DEVOPS_DB_PATH 并 reload sm/db_setup——teardown 恢复 env 并
    全家族重挂（同 test_issue_number_text_migration 口径），防污染同进程后续文件。"""
    saved = {k: os.environ.get(k) for k in ("DEVOPS_DB_PATH",)}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    for mod_name in ("src.domains.mgmt.user.db", "src.domains.mgmt.auth.grape_account_db", "src.domains.mgmt.admin.db",
                     "src.domains.mgmt.user.org_db", "src.kernel.persistence.repositories",
                     "src.kernel.persistence.credential_usage_log", "src.api.deps"):
        try:
            importlib.reload(importlib.import_module(mod_name))
        except Exception:
            pass


_LEGACY_DDL = [
    # (table, ddl)——模拟存量库：user_id 族列声明 INTEGER（旧 DDL），含 UNIQUE 索引/表达式默认值
    ("binding_tickets", """
        CREATE TABLE binding_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_no TEXT NOT NULL,
            initiating_user_id INTEGER NOT NULL,
            original_user_id INTEGER NOT NULL DEFAULT 0,
            status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE (ticket_no)
        )"""),
    ("chat_agent_session", """
        CREATE TABLE chat_agent_session (
            id TEXT PRIMARY KEY,
            user_id INTEGER DEFAULT 0,
            repo_key TEXT DEFAULT '',
            created_at TEXT
        )"""),
    ("issue", """
        CREATE TABLE issue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            repo_key TEXT NOT NULL,
            issue_number TEXT NOT NULL,
            scene_user_id INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )"""),
    ("external_identities", """
        CREATE TABLE external_identities (
            id TEXT PRIMARY KEY,
            grape_user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            external_id TEXT NOT NULL,
            UNIQUE (provider, external_id)
        )"""),
    ("user_repos", """
        CREATE TABLE user_repos (
            user_id INTEGER NOT NULL,
            repo_key TEXT NOT NULL,
            PRIMARY KEY (user_id, repo_key)
        )"""),
    ("obs_llm_usage", """
        CREATE TABLE obs_llm_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 0,
            tokens INTEGER DEFAULT 0
        )"""),
]

# 各表一个 int 存量行 + 一个 uuid 字符串行（复现线上混型形态）
_LEGACY_ROWS = [
    ("INSERT INTO binding_tickets (ticket_no, initiating_user_id, original_user_id) VALUES ('T1', 24, 0)"),
    ("INSERT INTO binding_tickets (ticket_no, initiating_user_id, original_user_id) VALUES ('T2', '01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67', 24)"),
    ("INSERT INTO chat_agent_session (id, user_id, repo_key) VALUES ('s1', 24, 'g:o/r')"),
    ("INSERT INTO chat_agent_session (id, user_id, repo_key) VALUES ('s2', '01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67', 'g:o/r')"),
    ("INSERT INTO issue (user_id, repo_key, issue_number) VALUES (24, 'g:o/r', '5')"),
    ("INSERT INTO issue (user_id, repo_key, issue_number) VALUES ('01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67', 'g:o/r', '6')"),
    ("INSERT INTO external_identities (id, grape_user_id, provider, external_id) VALUES ('e1', 24, 'gitcode', 'x1')"),
    ("INSERT INTO external_identities (id, grape_user_id, provider, external_id) VALUES ('e2', '01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67', 'gitcode', 'x2')"),
    ("INSERT INTO user_repos (user_id, repo_key) VALUES (24, 'g:o/r')"),
    ("INSERT INTO user_repos (user_id, repo_key) VALUES ('01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67', 'g:o/r')"),
    ("INSERT INTO obs_llm_usage (user_id, tokens) VALUES (24, 100)"),
    ("INSERT INTO obs_llm_usage (user_id, tokens) VALUES ('01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67', 200)"),
]

_UUID = "01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b67"


def _legacy_db(tmp_path):
    """模拟存量库：user_id 族列声明 INTEGER，int 行 + uuid 行混存。"""
    p = tmp_path / "legacy_account_fk.db"
    db = sqlite3.connect(str(p))
    c = db.cursor()
    for _t, ddl in _LEGACY_DDL:
        c.execute(ddl)
    c.execute("CREATE INDEX idx_bt_initiator ON binding_tickets(initiating_user_id)")
    for sql in _LEGACY_ROWS:
        c.execute(sql)
    db.commit()
    return db, c


def _decl(c, table, col):
    return next(r[2] for r in c.execute(f"PRAGMA table_info({table})").fetchall()
                if r[1] == col)


class TestAccountFkUserIdTextNormalize:
    def test_mixed_types_exist_before(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        for table, col in (("binding_tickets", "initiating_user_id"),
                           ("chat_agent_session", "user_id"),
                           ("issue", "user_id"),
                           ("external_identities", "grape_user_id"),
                           ("user_repos", "user_id")):
            types = dict(c.execute(
                f"SELECT typeof({col}), COUNT(*) FROM {table} GROUP BY 1").fetchall())
            assert types == {"integer": 1, "text": 1}, f"{table}.{col}: {types}"

    def test_normalize_all_text_values_preserved(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        changed = _normalize_account_fk_user_id_text(db)
        db.commit()
        assert changed == -1  # 存量库 INTEGER 亲和 → 重建路径
        for table, col in _ACCOUNT_FK_USER_ID_COLS:
            _cols = c.execute(f"PRAGMA table_info({table})").fetchall()
            if not _cols:
                continue
            # 子集 schema（legacy 模拟库只建部分列）：列不存在跳过
            if not any(r[1] == col for r in _cols):
                continue
            if _decl(c, table, col).upper() != "TEXT":
                raise AssertionError(f"{table}.{col} affinity not TEXT after normalize")
        # 全列 typeof=text
        for table, col in (("binding_tickets", "initiating_user_id"),
                           ("binding_tickets", "original_user_id"),
                           ("chat_agent_session", "user_id"),
                           ("issue", "user_id"),
                           ("external_identities", "grape_user_id"),
                           ("user_repos", "user_id"),
                           ("obs_llm_usage", "user_id")):
            types = dict(c.execute(
                f"SELECT typeof({col}), COUNT(*) FROM {table} GROUP BY 1").fetchall())
            assert types == {"text": 2}, f"{table}.{col}: {types}"

    def test_values_preserved_string_form(self, tmp_path):
        """存量 int 值保值：24 → '24' 字符串形态，不重编号不丢行。"""
        db, c = _legacy_db(tmp_path)
        _normalize_account_fk_user_id_text(db)
        db.commit()
        assert c.execute("SELECT COUNT(*) FROM binding_tickets "
                         "WHERE initiating_user_id='24'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM binding_tickets "
                         "WHERE initiating_user_id=?", (_UUID,)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM chat_agent_session "
                         "WHERE user_id='24'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM issue WHERE user_id='24'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM user_repos WHERE user_id='24'").fetchone()[0] == 1

    def test_str_param_hits_int_stored_rows(self, tmp_path):
        """fff452153 实锤场景回归锁：str 参数（'24'）必须命中 int 存量行。"""
        db, c = _legacy_db(tmp_path)
        _normalize_account_fk_user_id_text(db)
        db.commit()
        assert c.execute("SELECT ticket_no FROM binding_tickets "
                         "WHERE initiating_user_id=? AND status!='closed'",
                         ("24",)).fetchone()[0] == "T1"
        # int 参数经 TEXT 亲和自动转换仍命中（老调用方 int 传参不受影响）
        assert c.execute("SELECT COUNT(*) FROM binding_tickets "
                         "WHERE initiating_user_id=?", (24,)).fetchone()[0] == 1

    def test_unique_and_expr_defaults_survive_rebuild(self, tmp_path):
        """UNIQUE 索引与表达式默认值（datetime('now')）跨整表重建不丢。"""
        db, c = _legacy_db(tmp_path)
        _normalize_account_fk_user_id_text(db)
        db.commit()
        idx = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='binding_tickets'"
        ).fetchall()}
        assert "sqlite_autoindex_binding_tickets_1" in idx  # UNIQUE(ticket_no)
        assert "idx_bt_initiator" in idx
        # UNIQUE 语义仍生效
        try:
            c.execute("INSERT INTO binding_tickets (ticket_no, initiating_user_id, "
                      "original_user_id) VALUES ('T1', 24, 0)")
            db.commit()
            raise AssertionError("unique index lost after rebuild")
        except sqlite3.IntegrityError:
            pass
        # 表达式默认值仍生效（重建后的 DDL 里保留）
        ddl = c.execute("SELECT sql FROM sqlite_master WHERE name='binding_tickets'"
                        ).fetchone()[0]
        assert "datetime('now')" in ddl

    def test_idempotent_second_run_zero_change(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        assert _normalize_account_fk_user_id_text(db) == -1
        db.commit()
        # 二次执行：亲和全 TEXT 且无混型行 → 零改动
        assert _normalize_account_fk_user_id_text(db) == 0
        db.commit()
        total = sum(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t, _ in _LEGACY_DDL)
        assert total == len(_LEGACY_ROWS)
        assert _decl(c, "issue", "user_id").upper() == "TEXT"

    def test_missing_tables_and_cols_skipped(self, tmp_path):
        """空库（表不存在）与缺列场景：逐项跳过，不报错。"""
        db = sqlite3.connect(str(tmp_path / "empty.db"))
        assert _normalize_account_fk_user_id_text(db) == 0
        # 表在但缺 scene_user_id 列（_ensure_column 兜底前）
        p = tmp_path / "partial.db"
        db2 = sqlite3.connect(str(p))
        c2 = db2.cursor()
        c2.execute("CREATE TABLE issue (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                   "repo_key TEXT, issue_number TEXT)")
        c2.execute("INSERT INTO issue (user_id, repo_key, issue_number) VALUES (24, 'g:o/r', '5')")
        db2.commit()
        assert _normalize_account_fk_user_id_text(db2) == -1
        db2.commit()
        assert _decl(c2, "issue", "user_id").upper() == "TEXT"
        assert c2.execute("SELECT COUNT(*) FROM issue WHERE user_id='24'").fetchone()[0] == 1

    def test_enum_covers_required_pairs(self):
        """清单完整性：任务枚举的 33 对表列必须全量在列（202611091100 补录 2 对）。"""
        required = {
            ("chat_agent_session", "user_id"), ("user_credentials", "user_id"),
            ("managed_repos", "host_user_id"), ("user_repos", "user_id"),
            ("agent_tasks", "user_id"), ("chat_conversations", "user_id"),
            ("feishu_onboard_task", "user_id"), ("scene_market", "source_user_id"),
            ("scene_likes", "user_id"), ("skill_likes", "user_id"),
            ("session_api_tokens", "user_id"), ("api_tokens", "user_id"),
            ("user_feedback", "user_id"), ("autopilot_subscriptions", "user_id"),
            ("org_member", "user_id"), ("org_invitation", "invitee_user_id"),
            ("audit_logs", "user_id"), ("exchange_codes", "user_id"),
            ("binding_tickets", "initiating_user_id"),
            ("binding_tickets", "original_user_id"),
            ("community_posts", "user_id"), ("community_comments", "user_id"),
            ("community_post_likes", "user_id"), ("obs_llm_usage", "user_id"),
            ("obs_llm_usage_archive", "user_id"),
            ("grape_t_credential_reveal_log", "user_id"),
            ("grape_t_credential_usage_log", "user_id"),
            ("grape_t_credential_usage_log", "initiating_actor_id"),
            ("external_identities", "grape_user_id"),
            ("issue", "user_id"), ("issue", "scene_user_id"),
            ("grape_t_patrol_findings", "owner"),
            ("issue", "release_approved_by"),
        }
        assert set(_ACCOUNT_FK_USER_ID_COLS) == required
        assert len(_ACCOUNT_FK_USER_ID_COLS) == 33


class Test202611091100OwnerReleaseApprovedBy:
    """202611091100 补录回归：patrol_findings.owner / issue.release_approved_by
    混型保值（int 存量行字符串化）+ 幂等（二次执行零改动）。"""

    def test_mixed_rows_preserved_and_idempotent(self, tmp_path):
        p = tmp_path / "owner_release.db"
        db = sqlite3.connect(str(p))
        c = db.cursor()
        c.execute("""CREATE TABLE grape_t_patrol_findings (
            id TEXT PRIMARY KEY, owner INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT 'broken_chain', status TEXT DEFAULT 'open')""")
        c.execute("""CREATE TABLE issue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_approved_by INTEGER DEFAULT 0,
            repo_key TEXT NOT NULL DEFAULT '', issue_number TEXT NOT NULL DEFAULT '')""")
        c.execute("INSERT INTO grape_t_patrol_findings (id, owner) VALUES ('f1', 24)")
        c.execute(f"INSERT INTO grape_t_patrol_findings (id, owner) VALUES ('f2', '{_UUID}')")
        c.execute("INSERT INTO issue (release_approved_by, repo_key, issue_number) VALUES (24, 'g:o/r', '5')")
        c.execute(f"INSERT INTO issue (release_approved_by, repo_key, issue_number) VALUES ('{_UUID}', 'g:o/r', '6')")
        db.commit()

        # 首跑：INTEGER 亲和 → 整表重建（返回 -1），int 存量值字符串化保值
        assert _normalize_account_fk_user_id_text(db) == -1
        db.commit()
        assert _decl(c, "grape_t_patrol_findings", "owner").upper() == "TEXT"
        assert _decl(c, "issue", "release_approved_by").upper() == "TEXT"
        # int 行保值（'24'）且 str/uuid 参数可直接命中（混型互查根因消除）
        assert c.execute("SELECT owner FROM grape_t_patrol_findings WHERE id='f1'").fetchone()[0] == "24"
        assert c.execute("SELECT owner FROM grape_t_patrol_findings WHERE owner=?", (_UUID,)).fetchone()[0] == _UUID
        assert c.execute("SELECT release_approved_by FROM issue WHERE release_approved_by=?", (_UUID,)).fetchone()[0] == _UUID
        assert c.execute("SELECT COUNT(*) FROM issue WHERE release_approved_by='24'").fetchone()[0] == 1

        # 二跑：亲和已 TEXT、无混型行 → 零改动（幂等）
        assert _normalize_account_fk_user_id_text(db) == 0
        db.commit()
        assert _decl(c, "grape_t_patrol_findings", "owner").upper() == "TEXT"


def test_new_ddl_declares_text_for_all_enum_cols(tmp_path, monkeypatch):
    """根修 A 第 2 步行为锁：db_setup 新 DDL 对清单内全部表列声明 TEXT 亲和——
    init_database 后每列 PRAGMA type 即为 TEXT（在其它文件声明 DDL 的 4 张表除外）。"""
    import sys
    sys.path.insert(0, ".")
    monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "fresh.db"))
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        bad = []
        for table, col in _ACCOUNT_FK_USER_ID_COLS:
            cols = c.execute(f"PRAGMA table_info({table})").fetchall()
            if not cols:
                continue  # 未在本库建的可选表（chat_agent_session 等其它模块建）由运行时迁移兜底
            decl = next((r[2] or "" for r in cols if r[1] == col), None)
            if decl is None:
                bad.append(f"{table}.{col}: column missing")
            # D105c 演练修正：MySQL 8 TEXT 列不允许 DEFAULT（1101），新装 DDL 用 VARCHAR(36)——
            # 不变式本质是字符串亲和（无 int 形态），TEXT 与 VARCHAR 均合规。
            elif decl.strip().upper() != "TEXT" and not decl.strip().upper().startswith("VARCHAR"):
                bad.append(f"{table}.{col}: {decl}")
    assert not bad, f"新 DDL 未字符串亲和化: {bad}"
