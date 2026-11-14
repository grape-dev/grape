"""P0-2 棘轮守卫：外域表引用只减不增（三库独立化）。

判据（方案 §9 P0、§10 A3）：
  - 任何**新增**的 (表, 消费域) 跨域引用 → 红；
  - 任何已有 pair 的引用数**上升** → 红；
  - 引用数下降但基线未收紧 → 红（提示运行 --update-baseline，保持棘轮咬合）。

归属口径：`docs/weekly-11/table-owner.tsv`（P0 冻结，102 张基表，来自三库实况）。
扫描口径见 `scripts/foreign_table_refs.py`（只算四进程运行代码 api/kernel/domains）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "foreign_table_refs.py"
OWNER_TSV = REPO_ROOT / "docs" / "weekly-11" / "table-owner.tsv"
BASELINE = REPO_ROOT / "docs" / "weekly-11" / "foreign-refs-allowlist.csv"


def _module():
    spec = importlib.util.spec_from_file_location("foreign_table_refs", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _scan():
    mod = _module()
    assert OWNER_TSV.is_file(), f"归属清单缺失：{OWNER_TSV}"
    assert BASELINE.is_file(), f"棘轮基线缺失：{BASELINE}"
    owner = mod.load_owner(OWNER_TSV)
    assert len(owner) >= 100, f"归属清单异常：只有 {len(owner)} 张表"
    rows = mod.scan(REPO_ROOT / "src", owner)
    return mod, rows


def test_no_new_cross_domain_table_refs():
    mod, rows = _scan()
    base = mod.load_baseline(BASELINE)
    violations, improvements = mod.check(rows, base)
    assert not violations, "新增/上升的跨域表引用（禁止）：\n" + "\n".join(violations[:30])


def test_baseline_kept_tight():
    """引用下降后必须同步收紧基线，否则棘轮会"忘记"已取得的进展。"""
    mod, rows = _scan()
    base = mod.load_baseline(BASELINE)
    _, improvements = mod.check(rows, base)
    assert not improvements, (
        "跨域引用已下降，但基线未收紧；请运行：\n"
        "  python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv "
        "--baseline docs/weekly-11/foreign-refs-allowlist.csv --update-baseline\n"
        + "\n".join(improvements[:30])
    )


def test_baseline_has_no_duplicates():
    import csv
    import collections

    pairs = [(r["table"], r["consumer"]) for r in csv.DictReader(BASELINE.open(encoding="utf-8"))]
    dupes = [k for k, n in collections.Counter(pairs).items() if n > 1]
    assert not dupes, f"基线存在重复行：{dupes[:10]}"