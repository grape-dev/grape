#!/usr/bin/env python3
"""生成/刷新迁移基线快照（tests/arch/migration_versions_baseline.json）。

用法：
    python3 scripts/gen-migration-baseline.py            # 打印当前注册表
    python3 scripts/gen-migration-baseline.py --write    # 覆盖基线（⚠ 仅发布时由维护者执行）

背景（2026-11-12 事故）：两条并行开发线各自占用了 202611120001~120005，
合并后同一版本号出现两个不同名字。数据库里只认"号"，已盖章就跳过 →
另一条线的 DDL 永不落地（UUID 主键 / trace_context 列 / autopilot 列全都没建），
而且日志只有一条 ERROR，不阻断启动。

基线就是"已发布迁移"的快照：已进基线的版本号不允许改名/改号，新迁移号必须更大。
"""
import argparse
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
BASELINE = REPO / "tests" / "arch" / "migration_versions_baseline.json"


def load_registry():
    """从代码注册表取权威 (version, name) 列表（含位置参数与关键字参数两种写法）。"""
    sys.path.insert(0, str(REPO))
    import src.api.db_setup as db_setup  # noqa: E402

    return [(m.version, m.name) for m in db_setup._MIGRATIONS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="覆盖基线文件（仅发布时执行）")
    args = ap.parse_args()

    pairs = load_registry()
    versions = {v: n for v, n in sorted(pairs)}
    dup = [v for v in versions if sum(1 for vv, _ in pairs if vv == v) > 1]

    print(f"注册表: {len(pairs)} 条，唯一版本号: {len(versions)}，重复: {sorted(set(dup)) or '无'}")
    if not args.write:
        if BASELINE.exists():
            base = json.loads(BASELINE.read_text(encoding="utf-8"))
            bv = base.get("versions") or {}
            renamed = [v for v in bv if v in versions and versions[v] != bv[v]]
            unknown = sorted(v for v in versions if v not in bv)
            print(f"基线: {len(bv)} 条，max={base.get('max_version')}")
            print(f"  改名/改号（应为 0）: {renamed or '无'}")
            print(f"  基线之后新增: {unknown}")
        else:
            print("基线不存在")
        return 0

    payload = {
        "generated_at": __import__("datetime").date.today().isoformat(),
        "source": "src/api/db_setup.py",
        "note": ("已发布迁移的 版本号→名字 快照。规则：已在此快照中的版本号不得改名/改号；"
                 "新迁移号必须大于 max_version。刷新需在发布评审时执行并单独提交。"),
        "max_version": max(versions),
        "versions": versions,
    }
    BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已写入 {BASELINE}（{len(versions)} 条，max={max(versions)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())