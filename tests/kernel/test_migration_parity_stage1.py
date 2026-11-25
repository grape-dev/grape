"""迁移对齐（阶段一）守卫：缺表/缺列/类型收敛四条必须保持注册。

背景（docs/weekly-11/migration-parity-assessment.md）：
线上三库曾出现「迁移已盖章、效果不存在」的假绿——202611120004 在 MySQL 上因
`TEXT DEFAULT ''` 无法 INSTANT/INPLACE 而未建列，仍被盖章。阶段一改用
202611140001-0004 重新落地，并把 `_ensure_column` 补上 TEXT 默认值剥离分支。

本守卫是静态的（AST 解析 db_setup），不依赖 MySQL 环境：
1. 四条迁移必须注册，版本号与名称锁定，防止被后续 rebase 悄悄删掉；
2. 两条建表迁移引用的 DDL 常量必须仍在内存（D9 按域拆分后建表 DDL 住 owner 域
   `schema_ddl.py`，db_setup 只 import —— 守卫随常量归属一起看 owner 模块）；
3. 修复迁移必须带 allow_copy（否则 MySQL 上会重演假绿）。

⚠ 2026-11-25（Issue #4339 顺带校正，非本 Issue 语义）：本文件在 HEAD 上已红——
`_CUL_DDL`/`_SC_DDL` 随 D9 拆到 `src/domains/runtime/schema_ddl.py`（db_setup 仅
import），而守卫仍要求它们在 db_setup 里定义；同时 `_registered()` 对
`name=VerificationHashWidthMigration.NAME` 这类非字面量做 `ast.literal_eval` 会直接
抛 ValueError。两处都是拆分后的守卫漂移，本次按归属校正指向、非字面量按「不参与
阶段一断言」跳过，断言强度不变（仍要求常量含 CREATE TABLE IF NOT EXISTS）。
"""
import ast
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_DB_SETUP = os.path.join(ROOT, "src", "api", "db_setup.py")
#: D9 按域拆分后的建表 DDL 归属模块（常量实际定义处，db_setup 只 import）。
_OWNER_DDL_MODULES = tuple(
    os.path.join(ROOT, "src", "domains", _d, "schema_ddl.py")
    for _d in ("mgmt", "runtime", "insight"))

_EXPECTED = {
    "202611140001": ("create-credential-usage-log-table", "_fn_create_credential_usage_log_table"),
    "202611140002": ("create-system-credentials-table", "_fn_create_system_credentials_table"),
    "202611140003": ("repair-agent-tasks-trace-context", "_fn_repair_agent_tasks_trace_context"),
    "202611140004": ("issue-state-scene-user-id-text-normalize",
                     "_fn_issue_state_scene_user_id_text_normalize"),
}


def _tree():
    with open(_DB_SETUP, encoding="utf-8") as f:
        return ast.parse(f.read())


def _registered():
    out = {}
    for node in ast.walk(_tree()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "register_migration"):
            continue
        kw = {}
        for k in node.keywords:
            if k.arg in ("version", "name"):
                try:
                    kw[k.arg] = ast.literal_eval(k.value)
                except (ValueError, TypeError):
                    # 非字面量（如 name=Cls.NAME）静态解析不出 → 不记录，本守卫只断言
                    # _EXPECTED 里那四条字面量登记；绝不能因此让整条 AST 解析崩掉
                    # （HEAD 上 202611190102 的 Attribute 形态就是这样把守卫打成红的）。
                    continue
            elif k.arg == "fn" and isinstance(k.value, ast.Name):
                kw["fn"] = k.value.id
        if kw.get("version"):
            out[kw["version"]] = (kw.get("name"), kw.get("fn"))
    return out


def test_stage1_migrations_still_registered():
    reg = _registered()
    for version, (name, fn) in _EXPECTED.items():
        assert version in reg, f"阶段一迁移 {version} 丢失（{name}）"
        got_name, got_fn = reg[version]
        assert got_name == name, f"{version} 名称漂移：{got_name} != {name}"
        assert got_fn == fn, f"{version} fn 漂移：{got_fn} != {fn}"


def _const_source(const: str):
    """常量定义所在源码：(源码文本, 文件路径)；找不到返回 (None, None)。

    D9 拆分后 `_CUL_DDL`/`_SC_DDL` 定义在 owner 域 `schema_ddl.py`，db_setup 只
    `from ... import`（源码里是 `_CUL_DDL,` 而非 `_CUL_DDL = `），因此按定义处检索。
    匹配按**行首赋值**锚定（`^\\s*<const> = `），避免注释/字符串里出现同名子串时误判。
    """
    pattern = re.compile(rf"^\s*{re.escape(const)}\s*=", re.MULTILINE)
    for path in (_DB_SETUP,) + _OWNER_DDL_MODULES:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if pattern.search(src):
            return src, path
    return None, None


def test_stage1_ddl_constants_present():
    with open(_DB_SETUP, encoding="utf-8") as f:
        db_setup_src = f.read()
    for const in ("_CUL_DDL", "_SC_DDL"):
        src, path = _const_source(const)
        assert src is not None, f"{const} 缺失——建表迁移会 NameError"
        # 从行首赋值处取常量右值（而非 `f"{const} = "` 的子串首次命中）
        value = re.search(rf"^\s*{re.escape(const)}\s*=\s*(.*)", src,
                          re.MULTILINE | re.DOTALL).group(1)
        assert "CREATE TABLE IF NOT EXISTS" in value[:400], \
            f"{path} 的 {const} 不含 CREATE TABLE IF NOT EXISTS（非幂等）"
        assert const in db_setup_src, f"{const} 未被 db_setup 引用（建表迁移会 NameError）"


def test_trace_context_repair_uses_allow_copy():
    """修复迁移必须 allow_copy，否则 MySQL 上 INSTANT/INPLACE 失败后只记人工清单。"""
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == "_fn_repair_agent_tasks_trace_context":
            call = next(n for n in ast.walk(node)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == "_ensure_column")
            kwargs = {k.arg: ast.literal_eval(k.value) for k in call.keywords
                      if k.arg in ("allow_copy", "strict")}
            assert kwargs.get("allow_copy") is True, "缺少 allow_copy=True"
            assert kwargs.get("strict") is True, "缺少 strict=True（静默失败会盖章）"
            ddl = ast.literal_eval(call.args[3])
            assert "DEFAULT" not in ddl.upper(), f"DDL 仍带默认值：{ddl}"
            return
    raise AssertionError("未找到 _fn_repair_agent_tasks_trace_context")