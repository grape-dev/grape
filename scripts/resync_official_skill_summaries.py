#!/usr/bin/env python3
"""Issue #3202 C7：批量重梳官方技能摘要（≤50 字，description 零改动）。

用法：
    python3 scripts/resync_official_skill_summaries.py            # dry-run（默认）
    python3 scripts/resync_official_skill_summaries.py --apply    # 落库

规则：
- 仅处理 scope='official' 且 is_active=1 的技能；
- 摘要取 description 首句，经 `_clean_summary` 压空白、去流程链、截断 ≤50 字；
- 只 UPDATE skill.summary（及 updated_at），**绝不改 description**；
- 无 description / 摘要已一致的行跳过。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_summary(description: str) -> str:
    """由概述生成 ≤50 字用途摘要（首句优先，禁流程链）。"""
    from src.domains.mgmt.routes.skills import _clean_summary

    text = (description or "").strip()
    if not text:
        return ""
    head = re.split(r"[。！？!?\n]", text, maxsplit=1)[0].strip()
    return _clean_summary(head or text)


def collect(rows):
    """返回需要更新的 (skill_id, skill_name, new_summary)。"""
    todo = []
    for row in rows:
        sid, name, description, summary = row[0], row[1], row[2] or "", row[3] or ""
        new_summary = build_summary(description)
        if not new_summary:
            continue
        if (summary or "").strip() == new_summary:
            continue
        todo.append((int(sid), name or "", new_summary))
    return todo


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="重梳官方技能摘要（≤50 字，概述零改动）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = parser.parse_args(argv)

    from src.domains.runtime import public as rt

    rows = rt.read_all_any("skill_official_summary_rows")
    todo = collect(rows)
    print(f"official skills={len(rows)} to_update={len(todo)} mode={'apply' if args.apply else 'dry-run'}")
    for sid, name, summary in todo:
        print(f"  - {sid} {name}: {summary}")
    if args.apply:
        for sid, _name, summary in todo:
            rt.exec_any("skill_summary_update", (summary, sid))
        print(f"updated={len(todo)} (description untouched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
