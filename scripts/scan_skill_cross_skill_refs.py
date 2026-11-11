#!/usr/bin/env python3
"""Issue #1070：全量扫描活跃技能 SKILL.md，输出带跨技能前缀引用的技能清单。

用法：
  python scripts/scan_skill_cross_skill_refs.py <skills_root> [more_roots...]
  # 例如：python scripts/scan_skill_cross_skill_refs.py data/skills /opt/grape/official-skills

对每个 <root>/<skill_name>/SKILL.md 调用 src.runtime.skill_materialize.
parse_skill_md_references（#1070 修复后口径），统计：
  - 带跨技能前缀引用的技能（物化此前必然误判失败的清单，验证修复覆盖）；
  - 引用了不存在自有 references 文件的技能（仍应判败，供物化巡检比对）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.runtime.skill_materialize import (  # noqa: E402
    parse_skill_md_references,
    validate_skill_dir,
)


def _cross_skill_refs(skill_md_path: str, skill_name: str) -> list:
    """返回该 SKILL.md 中带跨技能前缀的引用原文清单。"""
    import re
    refs = []
    try:
        with open(skill_md_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return refs
    for m in re.finditer(r"([\w.-]+)/(references/[\w./-]+\.[\w]+)", text):
        prefix, ref = m.group(1), m.group(2)
        if prefix != skill_name:
            refs.append(f"{prefix}/{ref}")
    return refs


def main():
    roots = sys.argv[1:]
    if not roots:
        print(__doc__)
        sys.exit(1)
    cross_skills, broken_own = [], []
    total = 0
    for root in roots:
        if not os.path.isdir(root):
            print(f"skip (not dir): {root}")
            continue
        for name in sorted(os.listdir(root)):
            skill_md = os.path.join(root, name, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            total += 1
            xs = _cross_skill_refs(skill_md, name)
            if xs:
                cross_skills.append((name, xs))
            ok, missing, _extra, _vd = validate_skill_dir(os.path.join(root, name))
            if not ok:
                broken_own.append((name, missing))
    print(f"\n扫描技能总数: {total}")
    print(f"\n== 带跨技能前缀引用的技能（#1070 修复覆盖，共 {len(cross_skills)} 个）==")
    for name, xs in cross_skills:
        print(f"  {name}: {', '.join(xs[:5])}{' ...' if len(xs) > 5 else ''}")
    print(f"\n== 自有内容缺失（物化仍应判败，共 {len(broken_own)} 个）==")
    for name, missing in broken_own:
        print(f"  {name}: missing {', '.join(missing[:5])}")
    print("\n修复验收口径：跨技能引用技能清单中的技能物化不再 missing；"
          "「自有内容缺失」清单中的技能仍判败。")


if __name__ == "__main__":
    main()
