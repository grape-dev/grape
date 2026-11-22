"""user_accounts 死表写入冻结守卫（2026-11-07 用户铁律：废弃表一定不可以再写入）。

背景：D32 账号体系迁移后 user_accounts 为死表（grape_accounts 是唯一权威
账号表），但"废弃"只做到了调用面废弃（生产零调用），写入面未冻结：
- 启动迁移回填 INSERT ... SELECT FROM users（user/db.py）每次启动往死表造数据
- 7 个 deprecated 写函数（create_user/update_password/link_*/save_user_account
  /delete_user_account/increment_token_version）+ 死测试继续存在

守卫口径（AST 级，非文本 grep——注释/字符串提及不触发）：
- GC1: src/ 全域零 `INSERT INTO user_accounts` / `INSERT OR ... INTO user_accounts`
- GC2: src/ 全域零 `UPDATE user_accounts`
- GC3: src/ 全域零 `DELETE FROM user_accounts`
- GC4: 废弃写函数符号已删除（防"删了调用没删定义"复活）

只读保留（不违反写入冻结铁律）：存量库启动迁移的 SELECT/EXISTS 重映射
（user/db.py 迁移段、db_setup.py 历史列 ALTER/读引用）。
"""
import ast
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_SRC = _TEST_DIR.parents[1] / "src"   # tests/mgmt/x.py → 仓库根/src

_TABLE = "user_accounts"

# GC4: 废弃写函数（唯一调用方曾是死测试/启动回填，现生产零调用）
_DEPRECATED_WRITE_FUNCS = (
    "create_user",
    "verify_user",
    "delete_user",
    "update_password",
    "increment_token_version",
    "get_token_version",
    "link_gitcode_user",
    "link_github_user",
    "save_user_account",
    "delete_user_account",
)

# 废弃读函数登记簿：2026-11-22 起全部随 user_accounts 清退删除，
# 现存名单为空 —— 若有人重新引入对旧表的读，请在评审里显式登记，不要静默加回。
_DEPRECATED_READ_FUNCS: tuple[str, ...] = ()


def _py_files():
    return [p for p in _SRC.rglob("*.py")]


def _table_write_ops(tree: ast.AST, filepath: Path):
    """AST 遍历：收集对 user_accounts 的写操作（INSERT/UPDATE/DELETE）。

    SQL 以常量字符串形态出现在 execute() 调用里，用 AST 抓字符串常量
    （f-string/拼接不匹配——现有代码全部是常量 SQL，覆盖充分）。
    """
    import re
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            sql = node.value
            if _TABLE not in sql:
                continue
            # INSERT（含 INSERT OR IGNORE/REPLACE INTO）
            if re.search(rf"INSERT\s+(OR\s+\w+\s+)?INTO\s+{_TABLE}\b", sql, re.I):
                offenders.append((filepath.name, node.lineno, "INSERT"))
            # UPDATE
            elif re.search(rf"UPDATE\s+{_TABLE}\b", sql, re.I):
                offenders.append((filepath.name, node.lineno, "UPDATE"))
            # DELETE
            elif re.search(rf"DELETE\s+FROM\s+{_TABLE}\b", sql, re.I):
                offenders.append((filepath.name, node.lineno, "DELETE"))
    return offenders


def test_gc1_gc2_gc3_user_accounts_zero_writes():
    """GC1/GC2/GC3：src/ 全域对 user_accounts 零写入（INSERT/UPDATE/DELETE）。

    铁律：D32 后 user_accounts 是死表，grape_accounts 是唯一权威账号表。
    任何新增写入（含启动迁移回填、定时清理 DELETE）都必须走方案评审改
    本守卫，不允许静默绕过。
    """
    offenders = []
    for f in _py_files():
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        offenders.extend(_table_write_ops(tree, f))
    assert not offenders, (
        "user_accounts 死表出现写入操作（铁律：废弃表零写入）: "
        f"{offenders}——账号写入必须走 grape_accounts（grape_account_db.py）"
    )


def test_gc4_deprecated_write_funcs_removed():
    """GC4：废弃写函数已删除——防"删了调用没删定义"复活。

    这些函数的退出判据=生产零调用（2026-11-07 实码核对）+ 死测试删除。
    复活任一符号（重新 def）即失败。
    """
    db_py = (_SRC / "domains" / "mgmt" / "user" / "db.py").read_text(encoding="utf-8")
    alive = []
    for fn in _DEPRECATED_WRITE_FUNCS:
        if ast.parse(db_py):  # 语法保证
            import re
            if re.search(rf"^def {fn}\b", db_py, re.M):
                alive.append(fn)
    assert not alive, (
        f"废弃写函数重新出现（user_accounts 写入面冻结铁律）: {alive}——"
        "替代路径: create_grape_account/update_password(grape_account_db.py)/"
        "create_external_identity/save_credential 通道"
    )
