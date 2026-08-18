#!/usr/bin/env python3
"""存量技能目录迁移到 OBS 固定 key（N17，见 obs-skill-storage-design.md §3 N17，v2.4）。

枚举本地 skills/custom/、skills/repo/ 目录，逐技能规范化打包（与上传链路同款：
根={skill_name}/、保留权限位、弃系统文件）→ 上传 OBS 固定 key（覆盖模型）→ DB 回写
（skill_path=固定对象键）。

用法：
  python scripts/migrate_skills_to_obs.py [--dry-run] [--delete-after]
  --dry-run      只列出将迁移的技能与对象键，不上传
  --delete-after sha256 校验通过后删除本地技能目录（默认保留为缓存）
"""
import argparse
import hashlib
import io
import os
import stat
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import log, SKILLS_DIR  # noqa: E402
from src.storage import get_storage, skill_key  # noqa: E402


def _norm_zip(skill_dir: str, skill_name: str) -> bytes:
    """与 src.api.routes.skills._normalize_skill_zip 同款规范化重打包。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(skill_dir):
            dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git", "__pycache__")]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, skill_dir).replace(os.sep, "/")
                arc = f"{skill_name}/{rel}"
                st = os.stat(full)
                info = zipfile.ZipInfo(arc)
                info.external_attr = (stat.S_IMODE(st.st_mode)) << 16
                with open(full, "rb") as fh:
                    zf.writestr(info, fh.read())
    return buf.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scan() -> list[tuple[str, str, str]]:
    """扫描本地技能目录，返回 [(scope, owner, skill_name)]。"""
    items: list[tuple[str, str, str]] = []
    custom_base = os.path.join(SKILLS_DIR, "custom")
    repo_base = os.path.join(SKILLS_DIR, "repo")
    if os.path.isdir(custom_base):
        for uid in sorted(os.listdir(custom_base)):
            udir = os.path.join(custom_base, uid)
            if not os.path.isdir(udir):
                continue
            for name in sorted(os.listdir(udir)):
                if os.path.isdir(os.path.join(udir, name)) and os.path.isfile(os.path.join(udir, name, "SKILL.md")):
                    items.append(("user", uid, name))
    if os.path.isdir(repo_base):
        for owner in sorted(os.listdir(repo_base)):
            odir = os.path.join(repo_base, owner)
            if not os.path.isdir(odir):
                continue
            for repo in sorted(os.listdir(odir)):
                rdir = os.path.join(odir, repo)
                if not os.path.isdir(rdir):
                    continue
                for name in sorted(os.listdir(rdir)):
                    if os.path.isdir(os.path.join(rdir, name)) and os.path.isfile(os.path.join(rdir, name, "SKILL.md")):
                        items.append(("repo", f"{owner}/{repo}", name))
    return items


def main():
    parser = argparse.ArgumentParser(description="存量技能目录迁移到 OBS")
    parser.add_argument("--dry-run", action="store_true", help="只列出清单不上传")
    parser.add_argument("--delete-after", action="store_true", help="校验通过后删除本地技能目录")
    args = parser.parse_args()

    storage = get_storage()
    if storage is None:
        print("OBS 未启用（OBS_ENABLED=true 且配置 AK/SK/BUCKET），无法迁移", file=sys.stderr)
        sys.exit(1)

    items = _scan()
    print(f"扫描到 {len(items)} 个技能待迁移")
    if not items:
        return

    from src.session_manager import db, db_lock

    ok_count = 0
    for scope, owner, skill_name in items:
        if scope == "user":
            skill_dir = os.path.join(SKILLS_DIR, "custom", owner, skill_name)
            db_scope, db_owner = "user", owner
        else:
            skill_dir = os.path.join(SKILLS_DIR, "repo", owner.replace("/", os.sep), skill_name)
            db_scope, db_owner = "repo", owner

        object_key = skill_key("custom" if db_scope == "user" else db_scope,
                               db_owner, skill_name)
        if args.dry_run:
            print(f"  [dry-run] {db_scope}/{db_owner}/{skill_name} -> {object_key}")
            continue

        zip_bytes = _norm_zip(skill_dir, skill_name)
        storage.save_bytes(object_key, zip_bytes)
        # DB 回写（存在则更新 key，不存在插入；v2.4 无 current_version）
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM skill WHERE skill_name=? AND scope=? AND owner_id=? AND is_active=1",
                      (skill_name, db_scope, db_owner))
            row = c.fetchone()
            if row:
                c.execute("UPDATE skill SET skill_path=? WHERE id=?", (object_key, row[0]))
            else:
                c.execute("INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, is_active) VALUES (?,?,?,'',?,?,?,1)",
                          (skill_name, skill_name, "", object_key, db_scope, db_owner))
            db.commit()
        print(f"  [ok] {db_scope}/{db_owner}/{skill_name} -> {object_key} ({len(zip_bytes)} bytes)")

        if args.delete_after:
            # sha256 校验（对象已上传，本地目录删除前校验 zip 一致性）
            remote = storage.read_bytes(object_key)
            if remote is not None and _sha256(remote) == _sha256(zip_bytes):
                import shutil
                shutil.rmtree(skill_dir, ignore_errors=True)
                print(f"  [deleted] {skill_dir}")
            else:
                print(f"  [warn] 校验失败，保留本地目录 {skill_dir}")
        ok_count += 1

    print(f"迁移完成：{ok_count}/{len(items)} 个技能已上传 OBS（--delete-after={args.delete_after}）")


if __name__ == "__main__":
    main()
