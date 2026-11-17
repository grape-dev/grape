"""F-5 regression guard: LLM cred scope routing must not use in-SQL json_extract joins.

Background (2026-11-17, aw11-3db E2E): the task_env scope route joined
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


def test_scope_route_uses_python_side_parse_and_lookup():
    """New-shape anchor: scope route parses runtime_info via json.loads then queries spc.id=?."""
    src = _src()
    # provider_id 取值走 Python json.loads（非 SQL json_extract）
    assert re.search(
        r'_json\.loads\([^)]*\).*provider_id|provider_id.*_json\.loads',
        src), "scope 路由未走 Python 侧 json.loads 取 provider_id"
    # spc 查询是等值参数化查询（spc.id=?），且在修复段附近
    assert "FROM grape_t_sandbox_provider_config spc" in src
    seg = src[src.find("F-5 修复"):]
    assert seg and "WHERE spc.id=?" in seg, "scope 路由缺参数化等值查询 WHERE spc.id=?"
    # miss 不静默：必须有 warning 日志（except: pass 静默吞是原缺陷的放大器）
    assert "scope 路由未命中 provider 行" in src, "scope 路由 miss 无显式日志"


def test_no_int_coercion_on_provider_id():
    """First-attempt regression ban: provider_id 不可 int() 强转（UUID 形态 spc.id 会炸→except 回落）。"""
    src = _src()
    seg = src[src.find("F-5 修复"):]
    assert seg
    assert not re.search(r"int\(_pid\)", seg), "provider_id 被 int() 强转——UUID provider 行会异常回落 user 级"
