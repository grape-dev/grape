"""F-5 regression guard: LLM cred scope routing must not use in-SQL json_extract joins.

Background (2026-11-17, split-DB E2E): the task_env scope route joined
grape_t_sandbox_provider_config on json_extract(r.runtime_info, '$.provider_id').
MariaDB returns the quoted JSON string ("2") where SQLite returned the bare
value, so after the split-DB migration the join silently missed and the
surrounding except:pass degraded the route to mode=user — injecting a stale
user-level LLM key into sandboxes (opencode Unauthorized -> exit=1).

Fix: Python-side json.loads of runtime_info + equality lookup on spc.id.

This guard bans the old pattern so it cannot be reintroduced during future
migrations (same family as G-04: split-DB afterglow), and anchors the new
implementation shape.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "domains" / "runtime" / "routes" / "runtime.py"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_no_in_sql_json_extract_join():
    """Old pattern ban: json_extract must never feed a JOIN/compare target in SQL strings."""
    src = _src()
    # 只看非注释代码行（修复注释里合理提及旧形态，裸名字 ban 会误伤）
    code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    offenders = [ln for ln in code_lines if "json_extract(" in ln]
    # F-5 修复后代码里 json_extract 应零命中（此前也仅 task_env 一处）；
    # 若未来确需在 SQL 用 JSON 函数，必须走跨库兼容封装并同步更新本守卫。
    assert not offenders, (
        "json_extract 出现在 runtime.py 代码行——F-5 缺陷形态回潮风险"
        "(MariaDB 返回带引号 JSON 串，拆库后 JOIN 恒 miss 静默回落 user 级凭据)：\n"
        + "\n".join(offenders[:5])
    )


def test_scope_route_uses_python_side_parse_and_public_lookup():
    """New-shape anchor: scope route parses runtime_info in Python, then resolves
    the provider's credential through the mgmt domain instead of joining the
    foreign table on the process cursor."""
    src = _src()
    # provider_id 取值走 Python json.loads（非 SQL json_extract）
    assert re.search(r'json\.loads\([^)\n]*\)[^\n]*provider_id', src), \
        "scope 路由未走 Python 侧 json.loads 取 provider_id"
    # 表归 mgmt：本域代码行不得直查该表（拆库下必然 1146；注释提及不算）
    code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    assert not any("grape_t_sandbox_provider_config" in ln for ln in code_lines), (
        "runtime.py 又直查了 mgmt 域的 grape_t_sandbox_provider_config——"
        "应走 mgmt.public.sandbox_provider_credential_id()")
    assert "sandbox_provider_credential_id" in src, \
        "scope 路由未通过 mgmt public 解析 provider 的 credential_id"
    # scope 行仍在本域取（runtime_credentials 归 runtime）
    assert "scope, repo_key FROM runtime_credentials" in src, \
        "scope/repo_key 应在本域 runtime_credentials 上取"
    # miss 不静默：静默 except 是原缺陷的放大器——失败必须折算成显式 marker
    assert '"llm_cred_missing": True' in src and '"llm_cred_source": "env_error"' in src, \
        "env 组装失败仍可能静默返回空 env（缺 llm_cred_missing 失败标记）"


def test_no_int_coercion_on_provider_id():
    """First-attempt regression ban: provider_id 不可 int() 强转（UUID 形态 spc.id 会炸→except 回落）。"""
    src = _src()
    # 锚点必须指向真实存在的取值行：旧锚点 `src.find("F-5 修复")` 在上游清理删掉该
    # 注释后返回 -1、分段退化为文件末尾，断言恒真（变异测试不会报红）。
    idx = src.find('.get("provider_id")')
    assert idx != -1, "provider_id 取值行缺失——锚点失效，需按当前实现重锚"
    line_start = src.rfind("\n", 0, idx) + 1
    line_end = src.find("\n", idx)
    value_line = src[line_start:line_end]
    assert "provider_id" in value_line, "锚点未落在 provider_id 取值行上"
    assert "int(" not in value_line, \
        "provider_id 被 int() 强转——UUID provider 行会异常回落 user 级"
