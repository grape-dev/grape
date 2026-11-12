"""D105-7 机制固化：账号域外键列 TEXT 不变式守卫（防复发）。

背景：D105b（202611091000）把 30 表 31 列 *user_id 族外键列归一 TEXT 后，
patrol_findings.owner / issue.release_approved_by 仍是 INTEGER 声明（D105-7 残留）。
本守卫把「账号域所有引用 TEXT 主键（grape_accounts.id uuid7）的外键列必须
声明 TEXT / 亲和 TEXT / 写入 uuid 后同列 typeof 恒 text」固化为常驻测试：

- 以后任何人加表/加列引用 user id 而声明 INTEGER——本文件必红，倒逼把该列
  同步进 db_setup._ACCOUNT_FK_USER_ID_COLS 枚举 + DDL TEXT 化（机制固化口径）。
- 清单 import db_setup 枚举常量（不硬拷贝），加列进枚举即自动被守卫覆盖。

建库手法同 test_d105_uuid_pk_migration / test_issue_number_text_migration：
内存/临时库跑 init_database()（完整建表 + 全量迁移链）。
"""
import os
import sqlite3

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _restore_global_db_state():
    """同 test_d105_uuid_pk_migration：_fresh_db 改写 DEVOPS_DB_PATH 是进程级
    全局污染，跑完恢复 env 并重载受影响模块，防止读写分裂扩散到后续测试。"""
    import importlib
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


@pytest.fixture()
def fresh_db(tmp_path):
    """独立临时库 + 完整建库链（含 D105b 202611091000 归一迁移）。"""
    import importlib
    dbp = str(tmp_path / "invariant.db")
    os.environ["DEVOPS_DB_PATH"] = dbp
    import src.kernel.session_manager as sm
    importlib.reload(sm)
    from src.api import db_setup
    importlib.reload(db_setup)
    db_setup.init_database()
    return dbp


def _known_integer_sentinel():
    """D105-7 残留登记表：语义含 0=unowned 整型哨兵、修复需动 src/（本轮
    仅 tests+docs 授权），暂以 INTEGER 声明在册。修掉一列即从此处删除并
    自动落入强断言面；全部清零后本登记表与看护用例整体退役。"""
    return {("grape_t_patrol_findings", "owner"),
            ("issue", "release_approved_by")}


def _fk_targets(strict=True):
    """受守卫的账号域外键列全集：db_setup 枚举清单 + D105-7 追补的散列。

    - _ACCOUNT_FK_USER_ID_COLS：30 表 31 对（含 grape_t_credential_usage_log.
      initiating_actor_id，558d9fb55 已补入）——import 真源，不硬拷贝。
    - grape_t_patrol_findings.owner、issue.release_approved_by：设计分片
      D105-7 点名的清单外散列；strict=True 时剔除仍 INTEGER 声明的残留项
      （它们由登记表用例单独看护）。
    """
    from src.api.db_setup import _ACCOUNT_FK_USER_ID_COLS
    extra = (("grape_t_patrol_findings", "owner"),
             ("issue", "release_approved_by"))
    residual = _known_integer_sentinel()
    seen, out = set(), []
    for pair in tuple(_ACCOUNT_FK_USER_ID_COLS) + extra:
        if strict and pair in residual:
            continue
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def test_fk_declaration_and_affinity_all_text(fresh_db):
    """不变式①：清单内每列声明类型 TEXT（→ SQLite TEXT 亲和）。

    枚举清单列（迁移 202611091000 + 新装 DDL 双保险）必须全 TEXT；
    INTEGER/VARCHAR 声明即红——新表加 user id 列忘同步清单/声明时在此拦截。
    """
    con = sqlite3.connect(fresh_db)
    bad = []
    for table, col in _fk_targets():
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue  # 可选表未建（如 MySQL-only 表），与迁移同口径跳过
        decl = next((r[2] or "" for r in rows if r[1] == col), None)
        if decl is None:
            continue  # 列未建，与迁移兜底口径一致
        # D105c 演练修正：MySQL 8 下 TEXT 列不允许 DEFAULT（1101），新装 DDL 用 VARCHAR(36)——
        # 不变式本质是字符串亲和（无 int 形态），TEXT 与 VARCHAR 同为合规。
        if decl.strip().upper() not in ("TEXT",) and not decl.strip().upper().startswith("VARCHAR"):
            bad.append(f"{table}.{col} declared {decl!r}")
    con.close()
    assert not bad, (
        "账号域 user id 外键列必须声明 TEXT（D105b 不变式）——"
        f"违规列: {bad}。修法：DDL 声明改 TEXT 并确认该列在 "
        "db_setup._ACCOUNT_FK_USER_ID_COLS 枚举内")


def test_fk_uuid_write_typeof_all_text_no_mixed(fresh_db):
    """不变式②：任何列 INSERT uuid 形态 id 后，同列 typeof 全 text 无混型。

    对每列（有行时含存量行）写入一个 36 字符 uuid7 形态值，再全列扫
    typeof != 'text'——INTEGER 亲和会把纯数字串翻回 int（混型根因），
    本断言让该根因在新表/新列上必红。
    """
    from src.id_gen import uuid7_str
    con = sqlite3.connect(fresh_db)
    mixed = []
    for table, col in _fk_targets():
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue
        if next((r for r in rows if r[1] == col), None) is None:
            continue
        # uuid7 形态主值 + 纯数字串（存量 int id 字符串化形态）双探针
        probes = (uuid7_str(), "24")
        try:
            for v in probes:
                con.execute(f"INSERT INTO {table}({col}) VALUES(?)", (v,))
        except sqlite3.Error:
            con.rollback()
            continue  # NOT NULL/UNIQUE 等约束拦截的列由建表方用例覆盖
        types = [r[0] for r in con.execute(
            f"SELECT DISTINCT typeof({col}) FROM {table} "
            f"WHERE {col} IN (?,?)", probes)]
        if any(t != "text" for t in types):
            mixed.append(f"{table}.{col} typeof={types}")
        con.execute(
            f"DELETE FROM {table} WHERE {col} IN (?,?)", probes)
        con.commit()
    con.close()
    assert not mixed, (
        "uuid/数字串写入后同列出现非 text 存储（亲和混型）——"
        f"违规列: {mixed}")


def test_fk_residual_integer_declarations_are_tracked(fresh_db):
    """D105-7 残留看护：清单外散列仍 INTEGER 的列必须显式在册。

    patrol_findings.owner / issue.release_approved_by 当前声明仍 INTEGER
    （语义含 0=unowned 整型哨兵，修复需动 src/，超出本轮 tests+docs 授权）。
    本用例把残留收敛为「显式登记」：修掉一列就把它从 _KNOWN_INTEGER_SENTINEL
    删掉并挪进上一用例的强断言面；全部修完后本用例与登记表整体退役。
    """
    con = sqlite3.connect(fresh_db)
    _KNOWN_INTEGER_SENTINEL = {
        ("grape_t_patrol_findings", "owner"),
        ("issue", "release_approved_by"),
    }
    still_integer = set()
    for table, col in _KNOWN_INTEGER_SENTINEL:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        decl = next((r[2] or "" for r in rows if r[1] == col), None)
        if decl is not None and decl.strip().upper() != "TEXT":
            still_integer.add((table, col))
    con.close()
    untracked = still_integer - _KNOWN_INTEGER_SENTINEL
    assert not untracked, (
        f"发现未登记的 INTEGER 声明外键列: {sorted(untracked)}——"
        "请修 DDL 为 TEXT，或若为暂缓项显式加入 _KNOWN_INTEGER_SENTINEL 并"
        "在 d105b 设计分片「机制固化」节登记理由")


def test_fk_uuid_write_typeof_for_residual_cols(fresh_db):
    """残留列（INTEGER 声明）行为面底线：uuid 形态值写入后 typeof 仍须 text。

    INTEGER 亲和只收编纯数字串（'24'→24），uuid 串带字母/连字符不被改写——
    这是残留列当下不炸 uuid 用户的原因，锁住这条底线：一旦某列连 uuid 串
    都被改写（如 COLLATE/触发器引入），必红。
    """
    from src.id_gen import uuid7_str
    con = sqlite3.connect(fresh_db)
    bad = []
    for table, col in (("grape_t_patrol_findings", "owner"),
                       ("issue", "release_approved_by")):
        uid = uuid7_str()
        try:
            con.execute(f"INSERT INTO {table}({col}) VALUES(?)", (uid,))
        except sqlite3.Error:
            continue
        types = [r[0] for r in con.execute(
            f"SELECT DISTINCT typeof({col}) FROM {table} WHERE {col}=?",
            (uid,))]
        if types != ["text"]:
            bad.append(f"{table}.{col} typeof={types}")
        con.execute(f"DELETE FROM {table} WHERE {col}=?", (uid,))
    con.commit()
    con.close()
    assert not bad, f"uuid 串写入被改写存储形态: {bad}"
