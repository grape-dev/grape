"""内部端点契约快照守卫（方案 §6.3）。

内部端点不进对外 `/openapi.json`（include_in_schema=False），因此单独用
`docs/weekly-11/internal-contract.json` 做契约快照：
  · 快照必须与当前 router 一致（漂移即红）；
  · 每个端点必须有 `operation_id`，且满足 `<domain>_<resource>_<action>`。
"""
from __future__ import annotations

import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

SCRIPT = os.path.join(ROOT, "scripts", "gen_internal_contract.py")
SNAPSHOT = os.path.join(ROOT, "docs", "weekly-11", "internal-contract.json")


def _mod():
    spec = importlib.util.spec_from_file_location("gen_internal_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def test_snapshot_is_current():
    mod = _mod()
    data = mod.collect()
    assert os.path.isfile(SNAPSHOT), f"内部契约快照缺失：{SNAPSHOT}"
    assert open(SNAPSHOT, encoding="utf-8").read() == mod.render(data), (
        "内部契约快照过期；运行 python3 scripts/gen_internal_contract.py")


def test_every_internal_route_has_domain_operation_id():
    mod = _mod()
    errs = mod.validate(mod.collect())
    assert not errs, "内部端点 operation_id 不合规：\n" + "\n".join(errs)


def test_p2_endpoints_present_in_snapshot():
    import json

    routes = {(r["method"], r["path"]) for r in
              json.load(open(SNAPSHOT, encoding="utf-8"))["routes"]}
    expected = {
        ("GET", "/internal/skills"),
        ("GET", "/internal/skills/{skill_id}"),
        ("GET", "/internal/engine-capabilities"),
        ("GET", "/internal/runtime-credentials/{runtime_id}"),
        ("GET", "/internal/meta/{namespace}"),
        ("POST", "/internal/tasks/batch"),
        ("POST", "/internal/stats/tasks"),
        ("POST", "/internal/xwrite/apply"),
        ("GET", "/internal/repos/{repo_key}"),
        ("GET", "/internal/accounts/{account_id}"),
        ("GET", "/internal/knowledge/{knowledge_id}"),
        ("POST", "/internal/user-mapping"),
    }
    missing = expected - routes
    assert not missing, f"N1–N13 端点未进契约快照：{sorted(missing)}"