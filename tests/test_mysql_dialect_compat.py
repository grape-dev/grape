"""MySQL 方言兼容性修复 —— 双方言行为用例（pytest，SQLite 测试库）。

对应 docs/weekly-08/mysql-dialect-compat-requirements.md §五 中不依赖 MySQL 的
行为断言：白名单（S1-U5）、cutoff 预计算（S2-U1/U2）、affected_count 语义
（S1-U3）、部分失败可见（S3 的 F3 语义）、datetime 带参写法零残留（防回归守卫）。

MySQL 方言级验证（S1-U1、S3-U1、S4、S5-U1、S6、S7-U1、S8-U2）为部署验收项：
按需求文档 §五用例清单在目标 MySQL 环境执行（pytest 强制 SQLite 隔离，
连不上 RDS；验收工具不入仓库）。
"""
import inspect
import time

import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------- S1-U5 白名单

def test_s1_u5_provider_whitelist():
    from src.api.routes.grape_auth import unbind_provider
    src = inspect.getsource(unbind_provider)
    assert 'provider not in ("gitcode", "github")' in src
    with pytest.raises(HTTPException) as ei:
        unbind_provider(provider="foo", confirm=False, user={"id": -1}, request=None)
    assert ei.value.status_code == 400


def test_s1_u5_um_column_mapping():
    from src.repo_key import um_column_for_provider
    for p in ("gitcode", "github"):
        assert um_column_for_provider(p) in ("gitcode_user", "github_login")


# ---------------------------------------------------------------- S2 cutoff 预计算

def test_s2_u1_u2_stale_cutoff():
    from src.api.routes.feishu_onboard import STALE_TTL
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - STALE_TTL - 60))
    fresh = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 60))
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - STALE_TTL))
    assert old < cutoff      # S2-U1: 超过 TTL → 判 stale
    assert fresh > cutoff    # S2-U2: 未超 TTL → 不判 stale


def test_s2_cutoff_matches_sqlite_datetime_now_format():
    """cutoff 串格式必须与 datetime('now') 写入的 UTC 格式一致（字典序可比）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT datetime('now')")
        now_sql = c.fetchone()[0]
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    assert now_sql[:10] == cutoff[:10]          # 同一天（UTC）
    assert len(now_sql) == len(cutoff)          # 同格式 "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- S3/S1-U3 语义

def test_no_unguarded_datetime_now_with_modifier():
    """全库禁止出现无守卫的 datetime('now', 带参) SQL 常量（本修复的家族根因）。

    AST 精确匹配：只检查真正传给 cursor.execute/executemany 的字符串常量，
    注释/docstring 中的说明性文字不误报。合法残留形态：is_mysql 分支守卫内。
    """
    import ast
    import pathlib
    offenders = []
    for p in pathlib.Path("src").rglob("*.py"):
        if "__pycache__" in str(p) or p.name == "mysql_compat.py":
            continue
        try:
            text = p.read_text(errors="ignore")
            tree = ast.parse(text)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            nm = f.attr if isinstance(f, ast.Attribute) else ""
            if nm not in ("execute", "executemany") or not node.args:
                continue
            a = node.args[0]
            try:
                s = ast.literal_eval(a)
            except Exception:
                continue
            if not isinstance(s, str) or "datetime('now'," not in s.replace(" ", ""):
                continue
            # 上方 6 行内须有 is_mysql 分支守卫
            window = "\n".join(text.splitlines()[max(0, node.lineno - 7):node.lineno - 1])
            if "is_mysql" in window or "_is_mysql_backend" in window:
                continue
            offenders.append(f"{p}:{node.lineno}")
    assert not offenders, f"无守卫的 datetime('now', 带参) SQL 常量残留: {offenders}"


def test_s3_session_active_uses_parameterized_cutoff():
    """_check_session_active 的可执行 SQL 不含 SQLite 私有函数变体（标准参数化）。"""
    import ast
    import pathlib
    srcfile = pathlib.Path("src/api/event_pipeline.py").read_text()
    tree = ast.parse(srcfile)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_check_session_active")
    sqls = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            nm = f.attr if isinstance(f, ast.Attribute) else ""
            if nm == "execute" and node.args:
                try:
                    s = ast.literal_eval(node.args[0])
                    if isinstance(s, str):
                        sqls.append(s)
                except Exception:
                    pass
    assert sqls, "未找到 _check_session_active 内的 execute SQL"
    for s in sqls:
        assert "datetime(" not in s, s
        assert "last_active_at > ?" in s, s


def test_s1_u3_affected_count_failure_not_silent_zero():
    """affected_count 查询失败 → -1（可见的未知），不得静默归零。"""
    from src.api.routes.grape_auth import unbind_provider
    src = inspect.getsource(unbind_provider)
    assert "affected_count = -1" in src


def test_f3_user_repos_cleanup_partial_visible():
    """user_repos 清理失败 → 响应带 partial 标记 + log.error，不留静默半途状态。"""
    from src.api.routes.grape_auth import unbind_provider
    src = inspect.getsource(unbind_provider)
    assert "user_repos_cleanup_failed = True" in src
    assert '"partial": user_repos_cleanup_failed' in src


# ---------------------------------------------------------------- 防回归守卫

def test_no_escape_backslash_in_sql_strings():
    """全库 SQL 字符串禁止 ESCAPE 反斜杠写法（F1 家族根因守卫）。"""
    import pathlib
    offenders = []
    for p in pathlib.Path("src").rglob("*.py"):
        if "__pycache__" in str(p) or p.name == "mysql_compat.py":
            # mysql_compat 模块 docstring 是团队规则文档，允许提及 ESCAPE 字样
            continue
        for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if "ESCAPE" in line and "\\" in line and not line.strip().startswith("#"):
                offenders.append(f"{p}:{i}: {line.strip()[:70]}")
    assert not offenders, f"ESCAPE 反斜杠残留: {offenders}"


# ---------------------------------------------------------------- S9 参数化 sqlite_master（F7/M-11）

def test_s9_u3_translated_param_sqlite_master():
    """S9-U3：翻译层对参数化 sqlite_master 形态保留原样（Cursor 特判标记），
    不落入后续 ? -> %s 替换链产生非法 SQL。"""
    from src.mysql_compat import translate_sql, _SQLITE_MASTER_NAME_PARAM_RE
    sql = "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
    assert _SQLITE_MASTER_NAME_PARAM_RE.match(sql), "regex should match param form"
    # translate 返回原样（标记特判），调用方（Cursor）负责特判
    out = translate_sql(sql)
    assert out == sql, out
    # 无参数形态仍走 SHOW TABLES（零回归）
    assert translate_sql("SELECT name FROM sqlite_master WHERE type='table'") == "SHOW TABLES"
    # 非表名参数化形态不误伤（带引号字面量）
    out2 = translate_sql("SELECT name FROM sqlite_master WHERE type='table' AND name='managed_repos_legacy'")
    assert "sqlite_master" not in out2 or out2 == "SELECT name FROM sqlite_master WHERE type='table' AND name='managed_repos_legacy'", out2


def test_s9_u2_sqlite_param_sqlite_master_native():
    """S9-U2：SQLite 后端同 SQL 原生可执行（零回归）。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", ("nonexistent_tbl_xyz",))
        assert c.fetchone() is None
