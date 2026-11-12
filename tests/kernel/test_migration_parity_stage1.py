"""迁移对齐（阶段一）守卫：缺表/缺列/类型收敛四条必须保持注册。

背景（docs/weekly-11/migration-parity-assessment.md）：
线上三库曾出现「迁移已盖章、效果不存在」的假绿——202611120004 在 MySQL 上因
`TEXT DEFAULT ''` 无法 INSTANT/INPLACE 而未建列，仍被盖章。阶段一改用
202611140001-0004 重新落地，并把 `_ensure_column` 补上 TEXT 默认值剥离分支。

本守卫是静态的（AST 解析 db_setup），不依赖 MySQL 环境：
1. 四条迁移必须注册，版本号与名称锁定，防止被后续 rebase 悄悄删掉；
2. 两条建表迁移引用的 DDL 常量必须仍在模块内；
3. 修复迁移必须带 allow_copy（否则 MySQL 上会重演假绿）。
"""
import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_DB_SETUP = os.path.join(ROOT, "src", "api", "db_setup.py")

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
                kw[k.arg] = ast.literal_eval(k.value)
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


def test_stage1_ddl_constants_present():
    src = open(_DB_SETUP, encoding="utf-8").read()
    for const in ("_CUL_DDL", "_SC_DDL"):
        assert f"{const} = " in src, f"{const} 缺失——建表迁移会 NameError"
        assert "CREATE TABLE IF NOT EXISTS" in src.split(f"{const} = ", 1)[1][:400], \
            f"{const} 不含 CREATE TABLE IF NOT EXISTS（非幂等）"


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