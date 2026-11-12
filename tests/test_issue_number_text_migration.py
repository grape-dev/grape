"""issue-ident：issue.issue_number / parent_issue_number INTEGER 亲和行为锁（202611090004 修复回滚）。

背景：D105-5 曾把 issue_number / parent_issue_number 归一为 TEXT 亲和，
但用户已定口径为基线 #995 的 INTEGER 亲和——数字 ident 落盘即 int、
读侧恒 int；非数字 ident（gitee IK*）按 SQLite 动态类型原样 text，
D105 域不受影响。迁移 202611090004 现在承担“TEXT → INTEGER”回滚：
- 存量 INTEGER 亲和库：零改动（不重建、不 UPDATE，混型行为天然保留）；
- 存量 TEXT 亲和库（跑过旧版归一）：整表重建回 INTEGER，数据保值；
- str 参数等值比较经 INTEGER 亲和无损转换仍命中数字行，int 参数直接命中。
幂等：INTEGER 库重复执行恒 0；TEXT 库重建一次后二次执行恒 0。
"""
import os, sqlite3
import importlib
import pytest

from src.api.db_setup import _normalize_issue_number_text


@pytest.fixture(autouse=True)
def _restore_global_db_state():
    """测试体切换 DEVOPS_DB_PATH 并 reload sm/db_setup——teardown 恢复 env 并
    全家族重挂（同 test_d105_batch2 口径），防污染同进程后续文件。"""
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
                     "src.domains.mgmt.user.org_db", "src.persistence.repositories",
                     "src.persistence.credential_usage_log", "src.api.deps"):
        try:
            importlib.reload(importlib.import_module(mod_name))
        except Exception:
            pass


def _legacy_db(tmp_path):
    """模拟存量库：issue_number 声明 INTEGER（旧 DDL），混型存储。"""
    p = tmp_path / "legacy.db"
    db = sqlite3.connect(str(p))
    c = db.cursor()
    c.execute("CREATE TABLE issue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "repo_key TEXT NOT NULL, issue_number INTEGER NOT NULL)")
    c.execute("CREATE UNIQUE INDEX idx_tasks_repo_issue ON issue(repo_key, issue_number)")
    c.execute("CREATE INDEX idx_issue_dummy ON issue(repo_key)")
    c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('g/n', 96)")
    c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('g/n', 'IKTVZQ')")
    c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('g/n', 24)")
    db.commit()
    return db, c


def _decl(c):
    return next(r[2] for r in c.execute("PRAGMA table_info(issue)").fetchall()
                if r[1] == "issue_number")


class TestIssueNumberTextNormalize:
    def test_mixed_types_exist_before(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        types = dict(c.execute("SELECT typeof(issue_number), COUNT(*) FROM issue GROUP BY 1").fetchall())
        assert types == {"integer": 2, "text": 1}

    def test_normalize_rebuilds_affinity_and_preserves_value(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        changed = _normalize_issue_number_text(db)
        db.commit()
        assert changed == 0  # INTEGER 亲和为终态：零改动，不重建
        assert _decl(c).upper() == "INTEGER"
        # 保值断言：int 96 保持 int 存储，非数字 ident 保持 text——混型行为天然保留
        row = c.execute("SELECT issue_number, typeof(issue_number) FROM issue "
                        "WHERE repo_key='g/n' AND issue_number='96'").fetchone()
        assert row == (96, "integer")
        types = dict(c.execute("SELECT typeof(issue_number), COUNT(*) FROM issue GROUP BY 1").fetchall())
        assert types == {"integer": 2, "text": 1}

    def test_unique_index_survives_rebuild(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        _normalize_issue_number_text(db)
        db.commit()
        idx = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='issue'").fetchall()}
        assert {"idx_tasks_repo_issue", "idx_issue_dummy"} <= idx
        # UNIQUE 语义仍生效（同 repo_key+issue_number 第二行被拒）
        try:
            c.execute("INSERT INTO issue (repo_key, issue_number) VALUES ('g/n', '96')")
            db.commit()
            raise AssertionError("unique index lost after rebuild")
        except sqlite3.IntegrityError:
            pass

    def test_str_param_hits_after_normalize(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        _normalize_issue_number_text(db)
        db.commit()
        # INTEGER 亲和下 str 参数经亲和无损转换命中数字行；ident 行直接命中
        assert c.execute("SELECT COUNT(*) FROM issue WHERE repo_key='g/n' AND issue_number=?",
                         ("96",)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM issue WHERE repo_key='g/n' AND issue_number=?",
                         ("IKTVZQ",)).fetchone()[0] == 1

    def test_int_param_hits_via_text_affinity(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        _normalize_issue_number_text(db)
        db.commit()
        # INTEGER 亲和列：int 参数直接命中（github/gitcode int 传参路径不受影响）
        assert c.execute("SELECT COUNT(*) FROM issue WHERE repo_key='g/n' AND issue_number=?",
                         (96,)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM issue WHERE repo_key='g/n' AND issue_number=?",
                         (24,)).fetchone()[0] == 1

    def test_idempotent(self, tmp_path):
        db, c = _legacy_db(tmp_path)
        assert _normalize_issue_number_text(db) == 0
        db.commit()
        # 二次执行幂等：INTEGER 亲和已是终态，数据不动
        assert _normalize_issue_number_text(db) == 0
        assert c.execute("SELECT COUNT(*) FROM issue").fetchone()[0] == 3
        assert _decl(c).upper() == "INTEGER"

    def test_text_affinity_fresh_db_update_only(self, tmp_path):
        """新 DDL（TEXT 亲和）库：必须整表重建回 INTEGER 终态。"""
        p = tmp_path / "fresh.db"
        db = sqlite3.connect(str(p))
        c = db.cursor()
        c.execute("CREATE TABLE issue (id INTEGER PRIMARY KEY, repo_key TEXT, issue_number TEXT NOT NULL)")
        c.execute("INSERT INTO issue (repo_key, issue_number) "
                  "SELECT 'g/n', 7 WHERE 1")
        db.commit()
        # TEXT 亲和下 int 已被转 text；重建回 INTEGER 后恢复 int 存储
        assert _normalize_issue_number_text(db) == -1
        db.commit()
        assert _decl(c).upper() == "INTEGER"
        assert c.execute("SELECT issue_number, typeof(issue_number) FROM issue "
                         "WHERE repo_key='g/n'").fetchone() == (7, "integer")

    def test_missing_issue_table_noop(self, tmp_path):
        db = sqlite3.connect(str(tmp_path / "empty.db"))
        assert _normalize_issue_number_text(db) == 0


class TestParentIssueNumberTextNormalize:
    """P2：parent_issue_number 同根因纳入 INTEGER 亲和回滚（IK* 字符串写
    INTEGER 亲和列按动态类型原样保留，MySQL INT 列严格模式由 widen 分支处理）。"""

    def _legacy_db(self, tmp_path):
        """存量库：issue_number + parent_issue_number 均声明 INTEGER，混型存储。"""
        p = tmp_path / "legacy_parent.db"
        db = sqlite3.connect(str(p))
        c = db.cursor()
        c.execute("CREATE TABLE issue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "repo_key TEXT NOT NULL, issue_number INTEGER NOT NULL, "
                  "parent_issue_number INTEGER DEFAULT 0)")
        c.execute("CREATE UNIQUE INDEX idx_tasks_repo_issue ON issue(repo_key, issue_number)")
        c.execute("INSERT INTO issue (repo_key, issue_number, parent_issue_number) VALUES ('g/n', 96, 0)")
        c.execute("INSERT INTO issue (repo_key, issue_number, parent_issue_number) VALUES ('g/n', 97, 96)")
        c.execute("INSERT INTO issue (repo_key, issue_number, parent_issue_number) VALUES ('g/n', 'IKTVZQ', 'IKABCD')")
        db.commit()
        return db, c

    def _pdecl(self, c):
        return next(r[2] for r in c.execute("PRAGMA table_info(issue)").fetchall()
                    if r[1] == "parent_issue_number")

    def test_mixed_types_exist_before(self, tmp_path):
        db, c = self._legacy_db(tmp_path)
        types = dict(c.execute("SELECT typeof(parent_issue_number), COUNT(*) FROM issue GROUP BY 1").fetchall())
        assert types == {"integer": 2, "text": 1}

    def test_normalize_rebuilds_parent_affinity_all_text(self, tmp_path):
        db, c = self._legacy_db(tmp_path)
        changed = _normalize_issue_number_text(db)
        db.commit()
        assert changed == 0  # INTEGER 亲和为终态：零改动
        assert self._pdecl(c).upper() == "INTEGER"
        assert _decl(c).upper() == "INTEGER"  # issue_number 一并保持 INTEGER
        # 混型保值：数值行保持 int，ident 行保持 text
        types = dict(c.execute("SELECT typeof(parent_issue_number), COUNT(*) FROM issue GROUP BY 1").fetchall())
        assert types == {"integer": 2, "text": 1}
        assert c.execute("SELECT COUNT(*) FROM issue WHERE parent_issue_number='96'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM issue WHERE parent_issue_number='IKABCD'").fetchone()[0] == 1

    def test_str_param_hits_and_int_via_affinity(self, tmp_path):
        db, c = self._legacy_db(tmp_path)
        _normalize_issue_number_text(db)
        db.commit()
        assert c.execute("SELECT COUNT(*) FROM issue WHERE parent_issue_number=?",
                         ("96",)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM issue WHERE parent_issue_number=?",
                         (0,)).fetchone()[0] == 1  # int 参数经 INTEGER 亲和直接命中

    def test_idempotent(self, tmp_path):
        db, c = self._legacy_db(tmp_path)
        assert _normalize_issue_number_text(db) == 0
        db.commit()
        assert _normalize_issue_number_text(db) == 0  # 二次执行零改动
        assert self._pdecl(c).upper() == "INTEGER"
        assert c.execute("SELECT COUNT(*) FROM issue").fetchone()[0] == 3

    def test_parent_only_rebuild_when_issue_number_already_text(self, tmp_path):
        """issue_number 已 INTEGER、parent_issue_number 为 TEXT（旧版归一残留）
        的库：parent 列重建回 INTEGER 终态。"""
        p = tmp_path / "partial.db"
        db = sqlite3.connect(str(p))
        c = db.cursor()
        c.execute("CREATE TABLE issue (id INTEGER PRIMARY KEY, repo_key TEXT, "
                  "issue_number INTEGER NOT NULL, parent_issue_number TEXT DEFAULT '')")
        c.execute("INSERT INTO issue (repo_key, issue_number, parent_issue_number) VALUES ('g/n', 5, '5')")
        c.execute("INSERT INTO issue (repo_key, issue_number, parent_issue_number) VALUES ('g/n', 'IKX', 'IKY')")
        db.commit()
        assert _normalize_issue_number_text(db) == -1
        db.commit()
        assert self._pdecl(c).upper() == "INTEGER"
        assert _decl(c).upper() == "INTEGER"
        assert c.execute("SELECT COUNT(*) FROM issue WHERE parent_issue_number='5'").fetchone()[0] == 1
        # 幂等
        assert _normalize_issue_number_text(db) == 0


def test_dual_key_sort_numeric_order_restored(tmp_path):
    """LENGTH+number 双键行为锁：数字 idents 恢复数值序（9<24<96），
    ident 行确定性排在数字之后（字典序）——D105-4 场景 2 回归锁。"""
    import sys, importlib
    sys.path.insert(0, ".")
    os.environ["DEVOPS_DB_PATH"] = str(tmp_path / "sort.db")
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for num in ("96", "9", "24", "IKTVZQ", "IKTAB"):
            c.execute("INSERT INTO issue (user_id, repo_key, issue_number, assignee, title) "
                      "VALUES (1, 'gitee:o/r', ?, '', ?)", (num, f"t-{num}"))
        db.commit()

    # 直接验证业务使用的排序键（session_store.get_subtask_issues 同款 SQL）：
    # 数字行恢复数值序（读侧 int），ident 行确定性排后
    from src.kernel.session_manager import db as _db
    with db_lock:
        ordered = [r[0] for r in _db.cursor().execute(
            "SELECT issue_number FROM issue WHERE repo_key='gitee:o/r' "
            "ORDER BY LENGTH(issue_number), issue_number").fetchall()]
    assert ordered == [9, 24, 96, "IKTAB", "IKTVZQ"], f"双键排序语义: {ordered}"
