#!/usr/bin/env python3
"""v2.3 版本化存量收敛脚本（N17，见 obs-skill-storage-design.md §3 N17，v2.4）。

背景：v2.3 版本化模型下对象键为 .../v{n}/{name}-{rand8}.zip；v2.4 改固定 key
覆盖模型（.../{name}/{name}.zip）。本脚本把每个技能的当前版本内容复制到固定
key（保留最新内容），删除所有 v{n} 历史对象，DB skill_path 回写固定 key。

用法：
  python scripts/converge_skill_versions.py [--dry-run] [--delete-after]
  --dry-run      只列出将收敛的技能与对象键，不执行
  --delete-after 收敛校验通过后删除历史版本对象（默认保留，便于审计回退）
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage import get_storage, skill_key, parse_skill_key  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="v2.3 版本化存量收敛为固定 key")
    parser.add_argument("--dry-run", action="store_true", help="只列出清单不执行")
    parser.add_argument("--delete-after", action="store_true", help="收敛校验通过后删除历史版本对象")
    args = parser.parse_args()

    storage = get_storage()
    if storage is None:
        print("OBS 未启用（OBS_ENABLED=true 且配置 AK/SK/BUCKET），无法收敛", file=sys.stderr)
        sys.exit(1)

    # 1. 扫描 DB 中 skill_path 为版本化 key 的技能（含 /v{n}/ 段）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, skill_name, scope, owner_id, skill_path FROM skill WHERE is_active=1 AND skill_path LIKE '%/skills/%' AND skill_path LIKE '%/v%/%'")
        rows = c.fetchall()
    print(f"扫描到 {len(rows)} 条版本化技能记录待收敛")
    if not rows:
        return

    ok_count = 0
    for sid, skill_name, scope, owner_id, old_key in rows:
        scope_seg = "custom" if scope == "user" else scope
        fixed_key = skill_key(scope_seg, owner_id or "", skill_name)
        if args.dry_run:
            print(f"  [dry-run] {scope}/{owner_id}/{skill_name}\n    {old_key}\n    -> {fixed_key}")
            continue

        # 2. 当前版本对象内容 → 固定 key（保留最新内容）
        data = storage.read_bytes(old_key)
        if data is None:
            print(f"  [warn] 当前对象读取失败，跳过: {old_key}")
            continue
        storage.save_bytes(fixed_key, data)

        # 3. DB 回写（v2.5：file_path=固定 key；skill_path 回写技能名——字段职责分离）
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE skill SET file_path=?, skill_path=? WHERE id=?",
                      (fixed_key, skill_name, sid))
            db.commit()

        # 4. 删除该技能所有 v{n} 历史对象（含当前版本旧 key）
        info = parse_skill_key(old_key)
        prefix = f"skills/{scope_seg}"
        if info.get("owner"):
            prefix += f"/{info['owner']}"
        prefix += f"/{skill_name}/"
        hist_keys = [k for k in storage.list_keys(prefix) if k != fixed_key]
        if args.delete_after:
            for k in hist_keys:
                storage.delete_object(k)
            print(f"  [ok] {scope}/{owner_id}/{skill_name} -> {fixed_key}（删除 {len(hist_keys)} 个历史对象）")
        else:
            print(f"  [ok] {scope}/{owner_id}/{skill_name} -> {fixed_key}（历史对象保留，--delete-after 后清理）")
        ok_count += 1

    print(f"收敛完成：{ok_count}/{len(rows)}（--delete-after={args.delete_after}）")


if __name__ == "__main__":
    main()
