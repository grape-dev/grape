#!/usr/bin/env python3
"""场景分类 slug 校验门禁（Issue #3796）。

背景：分类字典已收敛（#3202/#3648），grape-skills 下发的 scenes/*/scene.json
仍携带旧 slug（skill-dev/iac/general/content/ai/swarm-dev），场景同步每轮对
category_dict 恒未命中（生产 696 条 WARNING/小时）且这批场景分类停更。
kernel 侧 LEGACY_CATEGORY_ALIASES 过渡别名已让解析链命中收敛后 slug；本脚本
把「字典无此 slug 且无别名」的配置错误前移到 CI（linux-backend 门禁）拦截，
防止别名表退役/新增坏 slug 时静默回归。

scenes/ 目录由 grape-skills 仓库随 ZIP 下发，本仓基线可能没有该目录：
缺目录时输出 **WARNING**（不静默通过）并以 0 退出（不阻断门禁）；CI/本地已
在别处校验该目录时可用 `--allow-missing` 显式声明降级（降为 INFO）。
scene.json 的 category 迁移由 grape-skills 单列跨仓跟进（Issue #3796 Notes）。

用法：
    python3 scripts/check_category_slugs.py                    # 校验仓库根 scenes/
    python3 scripts/check_category_slugs.py --dir PATH         # 指定 scenes 目录
    python3 scripts/check_category_slugs.py --allow-missing    # 缺目录时降级为 INFO
退出码：0 = 通过/降级跳过；1 = 存在「直查与别名均未命中」的 slug。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.kernel.category import (  # noqa: E402
    category_seed_dict_map,
    iter_scenes_category_slugs,
    validate_category_slugs,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="scenes/*/scene.json 分类 slug 门禁（Issue #3796）")
    parser.add_argument("--dir", default=str(REPO_ROOT / "scenes"),
                        help="scenes 目录（默认 <repo>/scenes）")
    parser.add_argument("--allow-missing", action="store_true", dest="allow_missing",
                        help="scenes 目录缺失时显式降级（默认输出 WARNING，二者都退出 0）")
    args = parser.parse_args(argv)

    scenes_dir = str(Path(args.dir).resolve())
    if not Path(scenes_dir).is_dir():
        detail = (f"[check_category_slugs] scenes dir not found: {scenes_dir} "
                  f"(grape-skills 下发目录未随仓携带，Issue #3796)")
        if args.allow_missing:
            print(f"{detail} — skipped via --allow-missing (degraded, gate has no scenes input)")
        else:
            # PR #1982 审查 Optional：缺目录不再静默通过——门禁空转必须在 CI 日志可见
            print(f"{detail} — WARNING: gate skipped without --allow-missing; pass "
                  f"--dir <grape-skills>/scenes (or a checkout of it) to actually gate",
                  file=sys.stderr)
        return 0

    dict_map = category_seed_dict_map()
    slugs_by_scene = iter_scenes_category_slugs(scenes_dir)
    if not slugs_by_scene:
        print(f"[check_category_slugs] no scene.json with category found under {scenes_dir} — pass")
        return 0

    failed = False
    for scene_name, slugs in slugs_by_scene.items():
        unknown = validate_category_slugs(slugs, dict_map)
        if not unknown:
            continue
        failed = True
        print(f"[check_category_slugs] FAIL {scene_name}/scene.json: "
              f"unknown category slug(s) {unknown} "
              f"(不在 CATEGORY_DICT_SEED 且无 LEGACY_CATEGORY_ALIASES 别名)")
    if failed:
        print("[check_category_slugs] 未知分类 slug 拦截：请迁移 scene.json 的 category 到 "
              "收敛后 slug（it-rd-* / operations / other-*），或在 kernel "
              "LEGACY_CATEGORY_ALIASES 增补过渡别名（Issue #3796）")
        return 1
    print(f"[check_category_slugs] {len(slugs_by_scene)} scene(s) category slugs all resolvable — pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
